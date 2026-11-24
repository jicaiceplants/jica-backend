import os
import time
import hashlib
import json
import random
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any
from dotenv import load_dotenv

import bcrypt
import jwt
from fastapi import FastAPI, HTTPException, Request, Response, Query, status
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from pydantic import BaseModel, Field, field_validator
from supabase import create_client, Client
import httpx

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
load_dotenv()

# --- CONFIGURATION & SUPABASE INITIALIZATION ---
raw_url = os.getenv("SUPABASE_URL", "")
# Sanitize URL: strip /rest/v1 or trailing slashes if passed by user
SUPABASE_URL = raw_url.replace("/rest/v1/", "").replace("/rest/v1", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_KEY")
GOOGLE_ROUTES_KEY = os.getenv("GOOGLE_ROUTES_API_KEY") or os.getenv("GOOGLE_ROUTES_SERVER_KEY")

# Free Gmail SMTP Configuration for Password Reset OTP
SMTP_EMAIL = os.getenv("SMTP_EMAIL", "")
SMTP_APP_PASSWORD = os.getenv("SMTP_APP_PASSWORD", "")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))

# In-Memory OTP Store: { identifier_hash: { "otp_hash": str, "expires_at": float, "user_id": str, "email": str, "raw_otp_for_dev": str } }
_otp_store: Dict[str, Dict[str, Any]] = {}

# In-Memory Global SOS Alert Store: { alert_id: dict }
_active_sos_alerts: Dict[str, Dict[str, Any]] = {}

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY / SUPABASE_ANON_KEY in .env file.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# CORS Origins
raw_origins = os.getenv("CORS_ORIGINS", "http://localhost:3000,http://localhost:8081,http://192.168.1.24:3000,http://192.168.1.24:8081")
allowed_origins = [o.strip() for o in raw_origins.split(",") if o.strip()]
if "*" not in allowed_origins and "http://localhost:3000" not in allowed_origins:
    allowed_origins.append("http://localhost:3000")

# Rate Limiter
limiter = Limiter(key_func=get_remote_address, default_limits=["300/minute"])

app = FastAPI(
    title="JICA Ice Logistics & Delivery API",
    description="High-performance FastAPI backend for JICA Ice Logistics (Cagayan de Oro City). Optimized for Railway Hobby Plan.",
    version="2.1.0"
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Middlewares
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins if allowed_origins else ["*"],
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- IN-MEMORY CACHE FOR GOOGLE ROUTES / DIRECTIONS API ($0 OVERAGE) ---
ROUTE_CACHE_TTL_SECONDS = int(os.getenv("ROUTE_CACHE_TTL_MINUTES", "10")) * 60
_route_cache: Dict[str, tuple[float, Dict[str, Any]]] = {}

def get_route_cache_key(origin_str: str, dest_str: str) -> str:
    serialized = f"{origin_str.strip().lower()}|{dest_str.strip().lower()}"
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

# --- PYDANTIC V2 SCHEMAS ---

class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)
    app: Optional[str] = "web"

class PasswordChangeRequest(BaseModel):
    user_id: str = Field(..., min_length=1)
    old_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=4)

class AdminPasswordResetRequest(BaseModel):
    admin_user_id: str = Field(..., min_length=1)
    target_user_id: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=4)

class ForgotPasswordRequest(BaseModel):
    identifier: str = Field(..., min_length=1, description="Username or Gmail address")

class VerifyOtpResetRequest(BaseModel):
    identifier: str = Field(..., min_length=1)
    otp: str = Field(..., min_length=4, max_length=8)
    new_password: str = Field(..., min_length=4)

class UpdateEmailRequest(BaseModel):
    user_id: str = Field(..., min_length=1)
    email: str = Field(..., min_length=3)

class SosAcknowledgeRequest(BaseModel):
    alert_id: str = Field(..., min_length=1)
    dispatcher_reply: Optional[str] = "Help is on the Way: Mechanic dispatch en route to your coordinates."
    acknowledged_by: Optional[str] = "Dispatcher"

class UserAccountCreate(BaseModel):
    full_name: str = Field(..., min_length=1, max_length=120)
    role: str = Field(default="Staff")
    username: Optional[str] = None
    password: Optional[str] = "1234"
    phone_number: Optional[str] = None
    email: Optional[str] = None
    mobile_pin: Optional[str] = "1234"
    assigned_truck_id: Optional[str] = None
    photo_url: Optional[str] = None

class UserAccountUpdate(BaseModel):
    full_name: Optional[str] = None
    role: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    phone_number: Optional[str] = None
    email: Optional[str] = None
    mobile_pin: Optional[str] = None
    assigned_truck_id: Optional[str] = None
    photo_url: Optional[str] = None
    is_active: Optional[bool] = None

class SoftDeleteRequest(BaseModel):
    entity_type: str = Field(..., description="Table name, e.g. load_slips, invoices, products, trucks, delivery_hubs, customers, user_accounts")
    entity_id: str = Field(..., min_length=1)
    deleted_by: str = Field(default="Staff")

class RestoreRequest(BaseModel):
    entity_type: str = Field(...)
    entity_id: str = Field(..., min_length=1)
    restored_by: str = Field(default="Staff")

class PermanentDeleteRequest(BaseModel):
    entity_type: str = Field(...)
    entity_id: str = Field(..., min_length=1)
    admin_user_id: str = Field(..., min_length=1)

class TelemetryPing(BaseModel):
    trip_id: str = Field(..., min_length=1, description="Load slip or active shift ID")
    driver_id: Optional[str] = None
    truck_id: Optional[str] = None
    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)
    speed_kmh: float = Field(default=0.0, ge=0.0, le=200.0)
    heading: float = Field(default=0.0, ge=0.0, le=360.0)
    battery_level: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    is_charging: bool = False
    is_sos: bool = False

class EmergencySOSAlert(BaseModel):
    driver_id: str = Field(..., min_length=1)
    truck_id: str = Field(..., min_length=1)
    load_slip_id: Optional[str] = None
    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)
    reason: str = Field(..., min_length=1)
    idempotency_key: str = Field(..., min_length=1)

class LatLngModel(BaseModel):
    latitude: float
    longitude: float

class RouteOptimizeRequest(BaseModel):
    origin: LatLngModel
    destination: LatLngModel
    destination_id: Optional[str] = None
    routing_preference: Optional[str] = "TRAFFIC_AWARE_OPTIMAL"

class SyncTransactionItem(BaseModel):
    idempotency_key: str = Field(..., min_length=1)
    stop_id: str = Field(..., min_length=1)
    customer_name: str = Field(..., min_length=1)
    planned_qty: int = Field(..., ge=0)
    delivered_qty: int = Field(..., ge=0)
    returned_qty: int = Field(default=0, ge=0)
    unit_price: float = Field(..., ge=0.0)
    total_amount: float = Field(..., ge=0.0)
    payment_method: str = Field(default="cash")
    partial_reason: Optional[str] = None

class BatchSyncPayload(BaseModel):
    transactions: List[SyncTransactionItem] = Field(default_factory=list)
    gps_pings: List[TelemetryPing] = Field(default_factory=list)

# --- SECURITY & AUTHENTICATION HELPERS ---
JWT_SECRET = os.getenv("JWT_SECRET", "jica_secret_jwt_key_cdo_production_994821a8f9024bce")
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_DAYS = 7

def hash_password(plain_password: str) -> str:
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(plain_password.encode("utf-8"), salt).decode("utf-8")

def verify_password(plain_password: str, hashed_or_plain: Optional[str]) -> bool:
    if not hashed_or_plain or not plain_password:
        return False
    # Bcrypt signature check ($2b$, $2a$, $2y$)
    if hashed_or_plain.startswith("$2b$") or hashed_or_plain.startswith("$2a$") or hashed_or_plain.startswith("$2y$"):
        try:
            return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_or_plain.encode("utf-8"))
        except Exception:
            return False
    # Fallback to plain string comparison for bootstrap initial records
    return plain_password == hashed_or_plain

def create_access_token(user_id: str, username: str, role: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=JWT_EXPIRATION_DAYS)).timestamp())
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def log_audit(entity_type: str, entity_id: str, action: str, changed_by: str, old_data: Optional[Dict] = None, new_data: Optional[Dict] = None):
    try:
        supabase.table("audit_logs").insert({
            "entity_type": entity_type,
            "entity_id": str(entity_id),
            "action": action,
            "changed_by": changed_by,
            "old_data": old_data,
            "new_data": new_data
        }).execute()
    except Exception as e:
        print(f"[AUDIT LOG NOTICE] {e}")

VALID_TRASH_TABLES = {
    "load_slips", "invoices", "products", "trucks", 
    "delivery_hubs", "customers", "user_accounts"
}

# --- SYSTEM & HEALTH ENDPOINTS ---

@app.get("/")
def root():
    return {
        "service": "JICA Ice Logistics & Delivery Backend",
        "status": "online",
        "region": "Cagayan de Oro City (Asia/Manila)",
        "version": "2.1.0"
    }

@app.get("/api/v1/health")
@app.get("/api/health")
def health_check():
    return {
        "status": "ok",
        "timestamp": time.time(),
        "database": "connected"
    }

@app.get("/api/v1/keep-alive")
def keep_alive():
    """Called periodically to prevent Supabase Free Tier inactivity pause."""
    try:
        res = supabase.table("user_accounts").select("id").limit(1).execute()
        return {"status": "active", "timestamp": time.time(), "ping": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database keep-alive failed: {str(e)}")

# --- AUTHENTICATION & PASSWORD MANAGEMENT ---

@app.post("/api/v1/auth/login")
@app.post("/auth/login")
def login_user(req: LoginRequest):
    """Bcrypt password authentication and signed JWT session creation for all roles (Owner, Staff, Security, Driver)."""
    try:
        clean_user = req.username.strip().lower()
        # Query by username (case-insensitive)
        res = supabase.table("user_accounts").select("*").ilike("username", clean_user).eq("is_active", True).execute()
        
        # If not found by username, try email (case-insensitive)
        if not res.data or len(res.data) == 0:
            res = supabase.table("user_accounts").select("*").ilike("email", clean_user).eq("is_active", True).execute()

        # If not found by email, try full_name (case-insensitive)
        if not res.data or len(res.data) == 0:
            res = supabase.table("user_accounts").select("*").ilike("full_name", clean_user).eq("is_active", True).execute()

        if not res.data or len(res.data) == 0:
            raise HTTPException(status_code=401, detail="Invalid username or password.")
        
        account = res.data[0]
        if account.get("deleted_at") is not None:
            raise HTTPException(status_code=401, detail="This account has been deactivated or soft-deleted.")

        stored_hash = account.get("password_hash")
        stored_plain = account.get("password")
        stored_pin = account.get("mobile_pin")
        
        # Verify with bcrypt against password_hash
        is_valid = False
        if stored_hash:
            is_valid = verify_password(req.password, stored_hash)

        # Fallback to plain password or mobile_pin if not matched by hash (or before migration)
        if not is_valid:
            if stored_plain and verify_password(req.password, stored_plain):
                is_valid = True
            elif stored_pin and verify_password(req.password, stored_pin):
                is_valid = True
        
        if not is_valid:
            raise HTTPException(status_code=401, detail="Invalid username or password.")
        
        # Enforce role restriction for Web Dashboard login: Only Owner and Staff allowed
        user_role = account.get("role")
        if req.app == "web" and user_role not in ("Owner", "Staff"):
            raise HTTPException(
                status_code=403,
                detail="Access Denied: Only the Owner and Office Staff can sign in to the Web Dashboard."
            )
        
        # Seamlessly auto-hash unmigrated passwords to bcrypt
        if not stored_hash:
            new_hash = hash_password(req.password)
            try:
                supabase.table("user_accounts").update({
                    "password_hash": new_hash,
                    "password": None
                }).eq("id", account["id"]).execute()
            except Exception as mig_err:
                print(f"[AUTH MIGRATION NOTICE] Could not update password_hash: {mig_err}")

        token = create_access_token(account["id"], account["username"] or clean_user, account["role"])
        
        # Audit log login
        log_audit("user_accounts", account["id"], "LOGIN", account.get("full_name", clean_user))

        # Safe user without credentials
        account_safe = {k: v for k, v in account.items() if k not in ("password", "password_hash", "mobile_pin")}
        return {"status": "success", "token": token, "user": account_safe}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/auth/change-password")
@app.post("/auth/change-password")
def change_password(req: PasswordChangeRequest):
    """Self-service password update for logged-in user with bcrypt hashing."""
    try:
        res = supabase.table("user_accounts").select("*").eq("id", req.user_id).execute()
        if not res.data or len(res.data) == 0:
            raise HTTPException(status_code=404, detail="User account not found.")
        
        account = res.data[0]
        stored_hash = account.get("password_hash")
        stored_plain = account.get("password") or account.get("mobile_pin") or "1234"

        if not verify_password(req.old_password, stored_hash or stored_plain):
            raise HTTPException(status_code=400, detail="Current password does not match.")
        
        new_hashed = hash_password(req.new_password)
        supabase.table("user_accounts").update({
            "password_hash": new_hashed,
            "password": None,
            "mobile_pin": req.new_password
        }).eq("id", req.user_id).execute()

        log_audit("user_accounts", req.user_id, "CHANGE_PASSWORD", account.get("full_name", "User"))
        return {"status": "success", "message": "Password changed successfully."}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/auth/admin-reset-password")
@app.post("/auth/admin-reset-password")
def admin_reset_password(req: AdminPasswordResetRequest):
    """Admin/Owner reset of employee password with bcrypt hashing."""
    try:
        admin_res = supabase.table("user_accounts").select("role, full_name").eq("id", req.admin_user_id).execute()
        if not admin_res.data or admin_res.data[0].get("role") != "Owner":
            raise HTTPException(status_code=403, detail="Only Owner can reset employee passwords.")
        
        new_hashed = hash_password(req.new_password)
        supabase.table("user_accounts").update({
            "password_hash": new_hashed,
            "password": None,
            "mobile_pin": req.new_password
        }).eq("id", req.target_user_id).execute()

        log_audit("user_accounts", req.target_user_id, "ADMIN_PASSWORD_RESET", admin_res.data[0].get("full_name", "Owner"))
        return {"status": "success", "message": "Employee password reset successfully."}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def send_otp_email(to_email: str, otp_code: str, user_name: str) -> bool:
    """Send 6-digit OTP verification email via smtp.gmail.com ($0 extra cloud cost)."""
    if not SMTP_EMAIL or not SMTP_APP_PASSWORD:
        print(f"[SMTP SIMULATION] Sent OTP {otp_code} to {to_email}. Set SMTP_EMAIL and SMTP_APP_PASSWORD in jica-backend/.env for live Gmail delivery.")
        return True

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"JICA Ice Logistics - Verification Code: {otp_code}"
    msg["From"] = f"JICA Logistics Dispatch <{SMTP_EMAIL}>"
    msg["To"] = to_email

    html_content = f"""
    <div style="font-family: Arial, sans-serif; padding: 24px; background: #f8fafc; color: #1e293b;">
      <div style="max-width: 480px; margin: 0 auto; background: #ffffff; border-radius: 16px; border: 1px solid #e2e8f0; padding: 32px; box-shadow: 0 4px 12px rgba(0,0,0,0.05);">
        <div style="text-align: center; margin-bottom: 24px;">
          <h2 style="color: #00875A; margin: 0; font-size: 22px; font-weight: 800; letter-spacing: 1px;">J.I.C.A ICE LOGISTICS</h2>
          <p style="color: #64748b; font-size: 12px; margin: 4px 0 0 0; text-transform: uppercase; font-weight: 700;">Password Reset Verification</p>
        </div>
        <p style="font-size: 14px; line-height: 22px;">Hello <strong>{user_name}</strong>,</p>
        <p style="font-size: 13px; color: #475569; line-height: 20px;">We received a request to reset your JICA portal password. Please use the following 6-digit verification code:</p>
        <div style="background: #f0fdf4; border: 2px dashed #00875A; border-radius: 12px; padding: 20px; text-align: center; margin: 24px 0;">
          <span style="font-size: 36px; font-weight: 900; letter-spacing: 8px; color: #00875A; font-family: monospace;">{otp_code}</span>
        </div>
        <p style="font-size: 12px; color: #64748b; line-height: 18px;">This code will expire in <strong>10 minutes</strong>. If you did not request this password reset, you can safely ignore this email.</p>
        <hr style="border: none; border-top: 1px solid #f1f5f9; margin: 24px 0 16px 0;" />
        <p style="font-size: 11px; color: #94a3b8; text-align: center; margin: 0;">Gusa Depot, Cagayan de Oro City • RA 10173 Data Privacy Compliant</p>
      </div>
    </div>
    """
    msg.attach(MIMEText(html_content, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(SMTP_EMAIL, SMTP_APP_PASSWORD)
            server.sendmail(SMTP_EMAIL, to_email, msg.as_string())
        return True
    except Exception as e:
        print(f"[SMTP ERROR] Could not deliver email to {to_email}: {e}")
        return False

@app.post("/api/v1/auth/forgot-password")
@app.post("/auth/forgot-password")
@limiter.limit("5/minute")
def forgot_password(req: ForgotPasswordRequest, request: Request):
    """Generate and email a 6-digit OTP code to the user's registered Gmail address."""
    clean_id = req.identifier.strip().lower()

    # Query account by username or email
    res = supabase.table("user_accounts").select("*").ilike("username", clean_id).eq("is_active", True).execute()
    if not res.data or len(res.data) == 0:
        res = supabase.table("user_accounts").select("*").ilike("email", clean_id).eq("is_active", True).execute()

    if not res.data or len(res.data) == 0:
        raise HTTPException(status_code=404, detail="No active account found matching this username or email address.")

    account = res.data[0]
    email = account.get("email")
    if not email or "@" not in email:
        raise HTTPException(
            status_code=400,
            detail="No Gmail address registered for this account. Please ask JICA Office Dispatch to add your email in Employee Management."
        )

    # Generate 6-digit numeric OTP
    otp_code = f"{random.randint(100000, 999999)}"
    otp_hash = hashlib.sha256(otp_code.encode("utf-8")).hexdigest()

    # Store with 10-minute expiration
    expires_at = time.time() + 600
    store_key = account["id"]
    _otp_store[store_key] = {
        "otp_hash": otp_hash,
        "expires_at": expires_at,
        "user_id": account["id"],
        "email": email,
        "raw_otp_for_dev": otp_code
    }

    # Also map by username and email for quick lookup
    if account.get("username"):
        _otp_store[account["username"].lower()] = _otp_store[store_key]
    _otp_store[email.lower()] = _otp_store[store_key]

    send_otp_email(email, otp_code, account.get("full_name", "Employee"))

    # Mask email for privacy (e.g. j***d@gmail.com)
    parts = email.split("@")
    user_part = parts[0]
    domain_part = parts[1] if len(parts) > 1 else "gmail.com"
    masked_user = user_part[0] + "***" + user_part[-1] if len(user_part) > 2 else user_part[0] + "***"
    masked_email = f"{masked_user}@{domain_part}"

    return {
        "status": "success",
        "message": f"Verification code sent to {masked_email}.",
        "masked_email": masked_email,
        "expires_in_minutes": 10
    }

@app.post("/api/v1/auth/verify-otp-reset")
@app.post("/auth/verify-otp-reset")
@limiter.limit("10/minute")
def verify_otp_reset(req: VerifyOtpResetRequest, request: Request):
    """Verify 6-digit OTP code and securely update password using bcrypt."""
    clean_id = req.identifier.strip().lower()
    stored = _otp_store.get(clean_id)

    if not stored:
        # Search all entries in store by user_id
        for v in _otp_store.values():
            if v.get("email", "").lower() == clean_id:
                stored = v
                break

    if not stored:
        raise HTTPException(status_code=400, detail="No active password reset request found or code expired. Please request a new code.")

    if time.time() > stored["expires_at"]:
        _otp_store.pop(clean_id, None)
        raise HTTPException(status_code=400, detail="Verification code has expired. Please request a new code.")

    entered_hash = hashlib.sha256(req.otp.strip().encode("utf-8")).hexdigest()
    if entered_hash != stored["otp_hash"]:
        raise HTTPException(status_code=400, detail="Invalid verification code. Please check your email and try again.")

    if len(req.new_password) < 4:
        raise HTTPException(status_code=400, detail="New password must be at least 4 characters long.")

    # Bcrypt hash the new password
    new_hashed = hash_password(req.new_password)
    user_id = stored["user_id"]

    try:
        supabase.table("user_accounts").update({
            "password_hash": new_hashed,
            "password": None,
            "mobile_pin": req.new_password
        }).eq("id", user_id).execute()

        # Remove from OTP store
        _otp_store.pop(clean_id, None)
        _otp_store.pop(user_id, None)

        log_audit("user_accounts", user_id, "PASSWORD_RESET_VIA_OTP", stored.get("email", "User"))
        return {
            "status": "success",
            "message": "Password updated successfully! You can now log in with your new password."
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database update failed: {str(e)}")

@app.post("/api/v1/auth/update-email")
@app.post("/auth/update-email")
def update_user_email(req: UpdateEmailRequest):
    """Allow user roles (Owner, Staff, Security, Driver) to update their Gmail address."""
    clean_email = req.email.strip().lower()
    if "@" not in clean_email:
        raise HTTPException(status_code=400, detail="Invalid email format. Please provide a valid Gmail address.")

    try:
        res = supabase.table("user_accounts").update({
            "email": clean_email
        }).eq("id", req.user_id).execute()

        if not res.data or len(res.data) == 0:
            raise HTTPException(status_code=404, detail="User account not found.")

        log_audit("user_accounts", req.user_id, "UPDATE_GMAIL", clean_email)
        return {
            "status": "success",
            "message": "Gmail address saved successfully.",
            "email": clean_email
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/auth/create-user")
@app.post("/api/v1/users")
def create_user(req: UserAccountCreate):
    """Create user account through backend with bcrypt hashed credentials."""
    try:
        raw_pass = req.password or "1234"
        hashed = hash_password(raw_pass)
        clean_username = req.username.strip().lower() if req.username else req.full_name.strip().lower().replace(" ", "")

        payload = {
            "full_name": req.full_name.strip(),
            "role": req.role,
            "username": clean_username,
            "password_hash": hashed,
            "password": None,
            "mobile_pin": req.mobile_pin or raw_pass,
            "phone_number": req.phone_number,
            "email": req.email,
            "assigned_truck_id": req.assigned_truck_id,
            "photo_url": req.photo_url,
            "is_active": True
        }
        res = supabase.table("user_accounts").insert(payload).execute()
        if not res.data:
            raise HTTPException(status_code=400, detail="Failed to create user account.")
        
        created = res.data[0]
        log_audit("user_accounts", created["id"], "CREATE_USER", "Admin")

        safe_user = {k: v for k, v in created.items() if k not in ("password", "password_hash", "mobile_pin")}
        return {"status": "success", "user": safe_user}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/v1/users/{user_id}")
def update_user(user_id: str, req: UserAccountUpdate):
    """Update user account through backend with bcrypt hashing if password changed."""
    try:
        update_data: Dict[str, Any] = {}
        if req.full_name is not None: update_data["full_name"] = req.full_name.strip()
        if req.role is not None: update_data["role"] = req.role
        if req.username is not None: update_data["username"] = req.username.strip().lower()
        if req.phone_number is not None: update_data["phone_number"] = req.phone_number
        if req.email is not None: update_data["email"] = req.email
        if req.assigned_truck_id is not None: update_data["assigned_truck_id"] = req.assigned_truck_id
        if req.photo_url is not None: update_data["photo_url"] = req.photo_url
        if req.is_active is not None: update_data["is_active"] = req.is_active
        if req.password:
            update_data["password_hash"] = hash_password(req.password)
            update_data["password"] = None
            update_data["mobile_pin"] = req.password
        elif req.mobile_pin:
            update_data["mobile_pin"] = req.mobile_pin

        res = supabase.table("user_accounts").update(update_data).eq("id", user_id).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="User account not found.")

        log_audit("user_accounts", user_id, "UPDATE_USER", "Admin")
        safe_user = {k: v for k, v in res.data[0].items() if k not in ("password", "password_hash", "mobile_pin")}
        return {"status": "success", "user": safe_user}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- DISPATCH REFERENCE DATA ---

@app.get("/api/v1/products")
def get_products():
    try:
        query = supabase.table("products").select("*").eq("is_active", True)
        # Attempt to filter deleted_at if column exists
        try:
            res = query.is_("deleted_at", "null").order("created_at").execute()
        except Exception:
            res = query.order("created_at").execute()
        return {"status": "success", "data": res.data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/trucks")
def get_trucks():
    try:
        query = supabase.table("trucks").select("*").eq("is_active", True)
        try:
            res = query.is_("deleted_at", "null").order("id").execute()
        except Exception:
            res = query.order("id").execute()
        return {"status": "success", "data": res.data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/hubs")
def get_hubs():
    try:
        query = supabase.table("delivery_hubs").select("*").eq("is_active", True)
        try:
            res = query.is_("deleted_at", "null").order("name").execute()
        except Exception:
            res = query.order("name").execute()
        return {"status": "success", "data": res.data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/users")
def get_users():
    try:
        query = supabase.table("user_accounts").select("id, full_name, role, username, email, phone_number, assigned_truck_id, photo_url, is_active, created_at, deleted_at").eq("is_active", True)
        try:
            res = query.is_("deleted_at", "null").order("created_at").execute()
        except Exception:
            res = query.order("created_at").execute()
        return {"status": "success", "data": res.data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- TRASH & RECOVERY (UNIVERSAL 30-DAY SOFT DELETE) ---

@app.get("/api/v1/trash/items")
def get_trash_items():
    """Gathers all soft-deleted records across all core tables."""
    items = []
    now = datetime.now(timezone.utc)
    for table_name in VALID_TRASH_TABLES:
        try:
            res = supabase.table(table_name).select("*").not_.is_("deleted_at", "null").order("deleted_at", desc=True).execute()
            if res.data:
                for row in res.data:
                    del_at_str = row.get("deleted_at")
                    days_left = 30
                    if del_at_str:
                        try:
                            del_dt = datetime.fromisoformat(del_at_str.replace("Z", "+00:00"))
                            elapsed_days = (now - del_dt).total_seconds() / 86400.0
                            days_left = max(0, int(30 - elapsed_days))
                        except Exception:
                            pass
                    
                    display_name = (
                        row.get("name") or 
                        row.get("full_name") or 
                        row.get("slip_number") or 
                        row.get("invoice_number") or 
                        row.get("id") or 
                        "Item"
                    )
                    items.append({
                        "id": row.get("id"),
                        "entity_type": table_name,
                        "display_name": display_name,
                        "deleted_at": del_at_str,
                        "deleted_by": row.get("deleted_by") or "Staff",
                        "days_left": days_left,
                        "raw_data": {k: v for k, v in row.items() if k not in ("password", "password_hash", "mobile_pin")}
                    })
        except Exception as e:
            # Table might not have deleted_at yet before patch is run
            continue
    return {"status": "success", "items": items}

@app.post("/api/v1/trash/soft-delete")
def soft_delete_item(req: SoftDeleteRequest):
    """Soft-deletes a record by setting deleted_at timestamp and deleted_by user."""
    if req.entity_type not in VALID_TRASH_TABLES:
        raise HTTPException(status_code=400, detail=f"Invalid entity type: {req.entity_type}")
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        res = supabase.table(req.entity_type).update({
            "deleted_at": now_iso,
            "deleted_by": req.deleted_by
        }).eq("id", req.entity_id).execute()

        log_audit(req.entity_type, req.entity_id, "SOFT_DELETE", req.deleted_by)
        return {"status": "success", "message": f"{req.entity_type} item moved to Trash."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/trash/restore")
def restore_trash_item(req: RestoreRequest):
    """Restores a soft-deleted record by clearing deleted_at and deleted_by."""
    if req.entity_type not in VALID_TRASH_TABLES:
        raise HTTPException(status_code=400, detail=f"Invalid entity type: {req.entity_type}")
    try:
        res = supabase.table(req.entity_type).update({
            "deleted_at": None,
            "deleted_by": None
        }).eq("id", req.entity_id).execute()

        log_audit(req.entity_type, req.entity_id, "RESTORE", req.restored_by)
        return {"status": "success", "message": f"{req.entity_type} item restored successfully."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/trash/delete-forever")
def delete_forever(req: PermanentDeleteRequest):
    """Permanently deletes a record. Restricted to Owner role."""
    if req.entity_type not in VALID_TRASH_TABLES:
        raise HTTPException(status_code=400, detail=f"Invalid entity type: {req.entity_type}")
    try:
        admin_res = supabase.table("user_accounts").select("role, full_name").eq("id", req.admin_user_id).execute()
        if not admin_res.data or admin_res.data[0].get("role") != "Owner":
            raise HTTPException(status_code=403, detail="Permanent deletion is strictly restricted to the Owner role.")
        
        admin_name = admin_res.data[0].get("full_name", "Owner")

        # Cascade items if load slip
        if req.entity_type == "load_slips":
            try:
                supabase.table("load_slip_items").delete().eq("load_slip_id", req.entity_id).execute()
            except Exception:
                pass

        supabase.table(req.entity_type).delete().eq("id", req.entity_id).execute()
        log_audit(req.entity_type, req.entity_id, "PERMANENT_DELETE", admin_name)
        return {"status": "success", "message": "Record permanently deleted."}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- TELEMETRY & SOS ---

@app.post("/api/v1/telemetry/ping")
def save_gps_ping(ping: TelemetryPing):
    try:
        payload = {
            "load_slip_id": ping.trip_id if len(ping.trip_id) == 36 else None,
            "driver_id": ping.driver_id,
            "truck_id": ping.truck_id,
            "latitude": ping.latitude,
            "longitude": ping.longitude,
            "speed_kmh": ping.speed_kmh,
            "heading": ping.heading,
            "battery_level": ping.battery_level,
            "is_charging": ping.is_charging,
            "is_sos": ping.is_sos
        }
        res = supabase.table("gps_logs").insert(payload).execute()
        return {"status": "success", "id": res.data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/telemetry/sos/active")
@app.get("/telemetry/sos/active")
def get_active_sos():
    """Retrieve active emergency SOS alerts for real-time web dispatch listener and mobile sync."""
    # Return alerts sorted by created_at descending
    alerts = list(_active_sos_alerts.values())
    alerts.sort(key=lambda a: a.get("created_at", ""), reverse=True)
    return {"status": "success", "alerts": alerts}

@app.post("/api/v1/telemetry/sos")
@app.post("/telemetry/sos")
def save_sos_alert(sos: EmergencySOSAlert):
    try:
        alert_id = sos.idempotency_key or f"SOS-{int(time.time() * 1000)}"
        now_iso = datetime.now(timezone.utc).isoformat()

        alert_data = {
            "id": alert_id,
            "idempotency_key": sos.idempotency_key,
            "load_slip_id": sos.load_slip_id,
            "driver_id": sos.driver_id,
            "truck_id": sos.truck_id,
            "latitude": sos.latitude,
            "longitude": sos.longitude,
            "reason": sos.reason,
            "status": "Active",
            "created_at": now_iso,
            "dispatcher_reply": None,
            "acknowledged_by": None
        }

        # Store in active SOS alerts registry
        _active_sos_alerts[alert_id] = alert_data

        # Also insert to Supabase gps_logs
        try:
            payload = {
                "load_slip_id": sos.load_slip_id if (sos.load_slip_id and len(sos.load_slip_id) == 36) else None,
                "driver_id": sos.driver_id if (sos.driver_id and len(sos.driver_id) == 36) else None,
                "truck_id": sos.truck_id,
                "latitude": sos.latitude,
                "longitude": sos.longitude,
                "is_sos": True
            }
            supabase.table("gps_logs").insert(payload).execute()
        except Exception as db_err:
            print(f"[SOS GPS_LOGS NOTICE] {db_err}")

        log_audit("emergency_sos", alert_id, "SOS_TRIGGERED", sos.driver_id, new_data=alert_data)
        return {"status": "success", "message": f"SOS Alert: {sos.reason}. Dispatch alerted immediately.", "alert": alert_data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/telemetry/sos/acknowledge")
@app.post("/telemetry/sos/acknowledge")
def acknowledge_sos_alert(req: SosAcknowledgeRequest):
    """Dispatcher response to Emergency SOS: sets status to Acknowledged and broadcasts 'Help is on the Way'."""
    alert = _active_sos_alerts.get(req.alert_id)
    now_iso = datetime.now(timezone.utc).isoformat()

    reply_msg = req.dispatcher_reply or "Help is on the Way: Support vehicle en route to your coordinates."
    ack_by = req.acknowledged_by or "Office Dispatcher"

    if alert:
        alert["status"] = "Acknowledged"
        alert["dispatcher_reply"] = reply_msg
        alert["acknowledged_by"] = ack_by
        alert["acknowledged_at"] = now_iso
        log_audit("emergency_sos", req.alert_id, "SOS_ACKNOWLEDGED", ack_by, new_data=alert)
        return {"status": "success", "message": "SOS alert acknowledged. Reply sent to driver.", "alert": alert}

    # If alert was created before restart or keyed differently, record acknowledgement
    placeholder_alert = {
        "id": req.alert_id,
        "status": "Acknowledged",
        "dispatcher_reply": reply_msg,
        "acknowledged_by": ack_by,
        "acknowledged_at": now_iso
    }
    _active_sos_alerts[req.alert_id] = placeholder_alert
    return {"status": "success", "message": "SOS alert acknowledged.", "alert": placeholder_alert}

# --- OFFLINE BATCH SYNC ENDPOINT (IDEMPOTENT) ---

@app.post("/api/v1/sync")
def sync_offline_batch(payload: BatchSyncPayload):
    """
    Idempotent batch sync for mobile driver app when reconnecting to network.
    Uses idempotency_key to prevent duplicate submissions.
    """
    synced_tx_keys = []
    
    # 1. Process Transactions
    for tx in payload.transactions:
        try:
            # Check if transaction already synced
            existing = supabase.table("load_slip_items").select("id").eq("idempotency_key", tx.idempotency_key).execute()
            if existing.data and len(existing.data) > 0:
                synced_tx_keys.append(tx.idempotency_key)
                continue

            # Update existing stop item if load_slip_item matches stop_id
            if len(tx.stop_id) == 36:
                supabase.table("load_slip_items").update({
                    "delivered_qty": tx.delivered_qty,
                    "returned_qty": tx.returned_qty,
                    "total_amount": tx.total_amount,
                    "payment_mode": "Cash" if tx.payment_method == "cash" else "Charge",
                    "partial_reason": tx.partial_reason,
                    "idempotency_key": tx.idempotency_key
                }).eq("id", tx.stop_id).execute()
            
            synced_tx_keys.append(tx.idempotency_key)
        except Exception as tx_err:
            print(f"Error syncing transaction {tx.idempotency_key}: {tx_err}")

    # 2. Process GPS pings
    if payload.gps_pings:
        gps_rows = [{
            "load_slip_id": p.trip_id if len(p.trip_id) == 36 else None,
            "driver_id": p.driver_id,
            "truck_id": p.truck_id,
            "latitude": p.latitude,
            "longitude": p.longitude,
            "speed_kmh": p.speed_kmh,
            "heading": p.heading,
            "is_sos": p.is_sos
        } for p in payload.gps_pings]
        try:
            supabase.table("gps_logs").insert(gps_rows).execute()
        except Exception as e:
            print(f"Error bulk saving GPS pings: {e}")

    return {
        "status": "success",
        "synced_transactions": synced_tx_keys,
        "gps_pings_count": len(payload.gps_pings)
    }

# --- GOOGLE ROUTES API V2 OPTIMIZE & CACHE PROXY ---

@app.post("/api/v1/routes/optimize")
async def optimize_route(req: RouteOptimizeRequest):
    """
    Proxies Google Routes API v2 with a 10-minute in-memory cache.
    Eliminates repetitive API calls for the same route corridor ($0 overage guarantee).
    """
    cache_key = get_route_cache_key(
        f"{req.origin.latitude},{req.origin.longitude}",
        f"{req.destination.latitude},{req.destination.longitude}"
    )
    now = time.time()

    # 1. Return from cache if fresh
    if cache_key in _route_cache:
        cached_ts, cached_payload = _route_cache[cache_key]
        if now - cached_ts < ROUTE_CACHE_TTL_SECONDS:
            return cached_payload

    # 2. Call Google Routes API v2 if server key is configured
    if GOOGLE_ROUTES_KEY and not GOOGLE_ROUTES_KEY.startswith("your_"):
        try:
            routes_url = "https://routes.googleapis.com/directions/v2:computeRoutes"
            headers = {
                "Content-Type": "application/json",
                "X-Goog-Api-Key": GOOGLE_ROUTES_KEY,
                "X-Goog-FieldMask": "routes.duration,routes.distanceMeters,routes.polyline.encodedPolyline,routes.legs.steps"
            }
            body = {
                "origin": {
                    "location": {
                        "latLng": {
                            "latitude": req.origin.latitude,
                            "longitude": req.origin.longitude
                        }
                    }
                },
                "destination": {
                    "location": {
                        "latLng": {
                            "latitude": req.destination.latitude,
                            "longitude": req.destination.longitude
                        }
                    }
                },
                "travelMode": "DRIVE",
                "routingPreference": req.routing_preference or "TRAFFIC_AWARE_OPTIMAL",
                "computeAlternativeRoutes": False
            }

            async with httpx.AsyncClient(timeout=6.0) as client:
                res = await client.post(routes_url, headers=headers, json=body)
                if res.status_code == 200:
                    data = res.json()
                    route = data.get("routes", [{}])[0]
                    dist = route.get("distanceMeters", 3500)
                    dur_str = route.get("duration", "480s").replace("s", "")
                    dur = int(dur_str) if dur_str.isdigit() else 480

                    steps_raw = route.get("legs", [{}])[0].get("steps", [])
                    steps_formatted = []
                    for idx, s in enumerate(steps_raw):
                        nav_inst = s.get("navigationInstruction", {})
                        steps_formatted.append({
                            "stepIndex": idx,
                            "instruction": nav_inst.get("instructions", "Continue along CDO corridor"),
                            "maneuver": nav_inst.get("maneuver", "straight").lower().replace("_", "-"),
                            "distanceMeters": s.get("distanceMeters", 500),
                            "durationSeconds": int(s.get("staticDuration", "60s").replace("s", "")) if s.get("staticDuration", "").replace("s", "").isdigit() else 60,
                            "startLocation": {
                                "latitude": s.get("startLocation", {}).get("latLng", {}).get("latitude", req.origin.latitude),
                                "longitude": s.get("startLocation", {}).get("latLng", {}).get("longitude", req.origin.longitude)
                            },
                            "endLocation": {
                                "latitude": s.get("endLocation", {}).get("latLng", {}).get("latitude", req.destination.latitude),
                                "longitude": s.get("endLocation", {}).get("latLng", {}).get("longitude", req.destination.longitude)
                            }
                        })

                    formatted_payload = {
                        "routeId": f"GOOGLE-{cache_key[:8]}",
                        "destinationId": req.destination_id or "DEST",
                        "destinationName": "CDO Delivery Stop",
                        "totalDistanceMeters": dist,
                        "totalDurationSeconds": dur,
                        "overviewPolyline": [
                            {"latitude": req.origin.latitude, "longitude": req.origin.longitude},
                            {"latitude": req.destination.latitude, "longitude": req.destination.longitude}
                        ],
                        "steps": steps_formatted
                    }
                    _route_cache[cache_key] = (now, formatted_payload)
                    return formatted_payload
        except Exception as err:
            print(f"Routes API call error, falling back to precomputed CDO route: {err}")

    # 3. Fallback: CDO Corridor baseline
    fallback_payload = {
        "routeId": f"CDO-{cache_key[:8]}",
        "destinationId": req.destination_id or "DEST",
        "destinationName": "Cagayan de Oro Corridor",
        "totalDistanceMeters": 3500,
        "totalDurationSeconds": 480,
        "overviewPolyline": [
            {"latitude": req.origin.latitude, "longitude": req.origin.longitude},
            {"latitude": (req.origin.latitude + req.destination.latitude) / 2, "longitude": (req.origin.longitude + req.destination.longitude) / 2},
            {"latitude": req.destination.latitude, "longitude": req.destination.longitude}
        ],
        "steps": [
            {
                "stepIndex": 0,
                "instruction": "Depart along National Highway toward destination",
                "maneuver": "depart",
                "distanceMeters": 1200,
                "durationSeconds": 160,
                "startLocation": {"latitude": req.origin.latitude, "longitude": req.origin.longitude},
                "endLocation": {"latitude": (req.origin.latitude + req.destination.latitude) / 2, "longitude": (req.origin.longitude + req.destination.longitude) / 2}
            },
            {
                "stepIndex": 1,
                "instruction": "Continue straight along Claro M. Recto Ave toward unloading bay",
                "maneuver": "straight",
                "distanceMeters": 1800,
                "durationSeconds": 240,
                "startLocation": {"latitude": (req.origin.latitude + req.destination.latitude) / 2, "longitude": (req.origin.longitude + req.destination.longitude) / 2},
                "endLocation": {"latitude": req.destination.latitude, "longitude": req.destination.longitude}
            },
            {
                "stepIndex": 2,
                "instruction": "Arrive at customer delivery drop-off point",
                "maneuver": "arrive",
                "distanceMeters": 50,
                "durationSeconds": 15,
                "startLocation": {"latitude": req.destination.latitude, "longitude": req.destination.longitude},
                "endLocation": {"latitude": req.destination.latitude, "longitude": req.destination.longitude}
            }
        ]
    }
    _route_cache[cache_key] = (now, fallback_payload)
    return fallback_payload
