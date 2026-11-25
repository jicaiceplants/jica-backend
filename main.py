import os
import re
import time
import uuid
import hashlib
import threading
import random
import smtplib
from decimal import Decimal, ROUND_HALF_UP
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any
from dotenv import load_dotenv

import bcrypt
import jwt
from fastapi import FastAPI, HTTPException, Request, Depends, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from pydantic import BaseModel, Field
from supabase import create_client, Client
import httpx

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
load_dotenv()

# --- CONFIGURATION & SUPABASE INITIALIZATION ---
raw_url = os.getenv("SUPABASE_URL", "")
SUPABASE_URL = raw_url.replace("/rest/v1/", "").replace("/rest/v1", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_KEY")
GOOGLE_ROUTES_KEY = os.getenv("GOOGLE_ROUTES_API_KEY") or os.getenv("GOOGLE_ROUTES_SERVER_KEY")

# Gmail SMTP Configuration for Password Reset OTP
SMTP_EMAIL = os.getenv("SMTP_EMAIL", "")
SMTP_APP_PASSWORD = os.getenv("SMTP_APP_PASSWORD", "")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))

MANILA_TZ = timezone(timedelta(hours=8))
UUID_REGEX = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)

# In-memory OTP store, active SOS registry, and per-truck dispatch locks
_otp_store: Dict[str, Dict[str, Any]] = {}
_active_sos_alerts: Dict[str, Dict[str, Any]] = {}
_truck_locks_guard = threading.Lock()
_truck_locks: Dict[str, threading.Lock] = {}

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY / SUPABASE_ANON_KEY in .env file.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# CORS Origins
raw_origins = os.getenv("CORS_ORIGINS", "http://localhost:3000,http://localhost:8081,http://192.168.1.24:3000,http://192.168.1.24:8081")
allowed_origins = [o.strip() for o in raw_origins.split(",") if o.strip()]
if "*" not in allowed_origins and "http://localhost:3000" not in allowed_origins:
    allowed_origins.append("http://localhost:3000")

limiter = Limiter(key_func=get_remote_address, default_limits=["400/minute"])

app = FastAPI(
    title="JICA Ice Logistics & Delivery API",
    description="High-performance FastAPI backend for JICA Ice Logistics (Cagayan de Oro City).",
    version="2.2.0"
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins if allowed_origins else ["*"],
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- ROUTE CACHE ---
ROUTE_CACHE_TTL_SECONDS = int(os.getenv("ROUTE_CACHE_TTL_MINUTES", "10")) * 60
_route_cache: Dict[str, tuple[float, Dict[str, Any]]] = {}

def get_route_cache_key(origin_str: str, dest_str: str) -> str:
    serialized = f"{origin_str.strip().lower()}|{dest_str.strip().lower()}"
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

# --- HELPERS (TIMEZONE, CURRENCY, LOCKS, UUID) ---

def manila_now() -> datetime:
    return datetime.now(MANILA_TZ)

def manila_date_str() -> str:
    return manila_now().strftime("%Y-%m-%d")

def manila_time_str() -> str:
    return manila_now().strftime("%H:%M")

def round_currency(value: Any) -> float:
    try:
        dec = Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return float(dec)
    except Exception:
        return 0.0

def is_uuid(val: Optional[str]) -> bool:
    return bool(val and UUID_REGEX.match(str(val).strip()))

JICA_ASSETS_BUCKET = "jica-assets"

def sanitize_image_url(url: Optional[str]) -> Optional[str]:
    if url is None:
        return None
    clean = str(url).strip()
    if not clean:
        return None
    if clean.lower().startswith("data:"):
        raise HTTPException(
            status_code=400,
            detail="Base64 image data is not permitted in PostgreSQL. Upload photos to Supabase Storage jica-assets first."
        )
    return clean

def extract_jica_asset_path(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    marker = f"/storage/v1/object/public/{JICA_ASSETS_BUCKET}/"
    idx = url.find(marker)
    if idx == -1:
        return None
    return url[idx + len(marker):].split("?")[0]

def delete_jica_asset_url(url: Optional[str]) -> None:
    path = extract_jica_asset_path(url)
    if not path:
        return
    try:
        supabase.storage.from_(JICA_ASSETS_BUCKET).remove([path])
    except Exception as e:
        print(f"[STORAGE CLEANUP NOTICE] {e}")

def get_truck_lock(truck_id: str) -> threading.Lock:
    with _truck_locks_guard:
        if truck_id not in _truck_locks:
            _truck_locks[truck_id] = threading.Lock()
        return _truck_locks[truck_id]

ACTIVE_UNREMITTED_STATUSES = {
    "DISPATCHED", "ACTIVE", "PENDING", "IN_TRANSIT", "IN-TRANSIT",
    "DELIVERY_FINISHED", "AWAITING_REMIT", "COMPLETED / PENDING REMITTANCE"
}

FINISHED_AWAITING_REMIT_STATUSES = {
    "DELIVERY_FINISHED", "AWAITING_REMIT", "COMPLETED / PENDING REMITTANCE"
}

REMITTED_OR_DONE_STATUSES = {
    "REMITTED", "COMPLETED", "RECONCILED", "CANCELLED"
}

def is_unremitted_status(status_val: Optional[str]) -> bool:
    if not status_val:
        return False
    return status_val.strip().upper() not in REMITTED_OR_DONE_STATUSES

# --- PYDANTIC SCHEMAS ---

class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)
    app: Optional[str] = "web"

class LogoutRequest(BaseModel):
    user_id: Optional[str] = None
    session_token: Optional[str] = None

class ValidateSessionRequest(BaseModel):
    user_id: str = Field(..., min_length=1)
    session_token: str = Field(..., min_length=1)

class PasswordChangeRequest(BaseModel):
    user_id: str = Field(..., min_length=1)
    old_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=4)

class AdminPasswordResetRequest(BaseModel):
    admin_user_id: str = Field(..., min_length=1)
    target_user_id: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=4)

class ForgotPasswordRequest(BaseModel):
    identifier: str = Field(..., min_length=1)

class VerifyOtpResetRequest(BaseModel):
    identifier: str = Field(..., min_length=1)
    otp: Optional[str] = None
    otp_code: Optional[str] = None
    new_password: str = Field(..., min_length=4)

class UpdateEmailRequest(BaseModel):
    user_id: str = Field(..., min_length=1)
    email: str = Field(..., min_length=3)

class SosAcknowledgeRequest(BaseModel):
    alert_id: str = Field(..., min_length=1)
    dispatcher_reply: Optional[str] = "Help is on the Way: Mechanic dispatch en route to your coordinates."
    acknowledged_by: Optional[str] = "Dispatcher"

class UserAccountCreate(BaseModel):
    id: Optional[str] = None
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

class TruckUpsertRequest(BaseModel):
    id: str = Field(..., min_length=1, max_length=40)
    plate_number: str = Field(..., min_length=1, max_length=40)
    model: str = Field(..., min_length=1, max_length=120)
    sack_capacity: int = Field(default=120, ge=1, le=2000)
    image_url: Optional[str] = None

class TruckUpdateRequest(BaseModel):
    plate_number: Optional[str] = None
    model: Optional[str] = None
    sack_capacity: Optional[int] = Field(default=None, ge=1, le=2000)
    image_url: Optional[str] = None

class ProductCreateRequest(BaseModel):
    id: Optional[str] = None
    name: str = Field(..., min_length=1, max_length=120)
    category: Optional[str] = None
    sack_size: str = Field(default="Medium")
    standard_price: float = Field(default=0.0, ge=0.0)
    image_url: Optional[str] = None

class ProductUpdateRequest(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    sack_size: Optional[str] = None
    standard_price: Optional[float] = Field(default=None, ge=0.0)
    image_url: Optional[str] = None

class HubCreateRequest(BaseModel):
    id: Optional[str] = None
    name: str = Field(..., min_length=1, max_length=120)
    address: str = Field(default="Cagayan de Oro City")
    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)
    zone: str = Field(default="City Proper")
    photo_url: Optional[str] = None

class HubUpdateRequest(BaseModel):
    name: Optional[str] = None
    address: Optional[str] = None
    latitude: Optional[float] = Field(default=None, ge=-90.0, le=90.0)
    longitude: Optional[float] = Field(default=None, ge=-180.0, le=180.0)
    zone: Optional[str] = None
    photo_url: Optional[str] = None

class DispatchItemPayload(BaseModel):
    idempotency_key: Optional[str] = None
    customer_id: Optional[str] = None
    customer_name: str = Field(..., min_length=1)
    hub_id: Optional[str] = None
    hub_name: Optional[str] = None
    product_id: Optional[str] = None
    product_name: str = Field(default="Ice Product")
    sack_size: Optional[str] = "Standard Sack"
    planned_qty: int = Field(..., ge=1)
    unit_price: float = Field(..., ge=0.0)
    total_amount: Optional[float] = None
    payment_mode: str = Field(default="Cash")

class CreateLoadSlipRequest(BaseModel):
    slip_number: Optional[str] = None
    idempotency_key: str = Field(..., min_length=1)
    date: Optional[str] = None
    departure_time: Optional[str] = None
    truck_id: str = Field(..., min_length=1)
    driver_id: Optional[str] = None
    driver_name: Optional[str] = None
    notes: Optional[str] = None
    allow_queued_if_in_transit: bool = True
    items: List[DispatchItemPayload] = Field(..., min_length=1)

class FinishDeliveryRequest(BaseModel):
    driver_id: Optional[str] = None
    truck_id: Optional[str] = None
    notes: Optional[str] = None

class ConfirmRemitRequest(BaseModel):
    notes: Optional[str] = None
    closed_at: Optional[str] = None

class SoftDeleteRequest(BaseModel):
    entity_type: str
    entity_id: str = Field(..., min_length=1)
    deleted_by: str = Field(default="Staff")

class RestoreRequest(BaseModel):
    entity_type: str
    entity_id: str = Field(..., min_length=1)
    restored_by: str = Field(default="Staff")

class PermanentDeleteRequest(BaseModel):
    entity_type: str
    entity_id: str = Field(..., min_length=1)
    admin_user_id: str = Field(..., min_length=1)

class TelemetryPing(BaseModel):
    trip_id: str = Field(..., min_length=1)
    driver_id: Optional[str] = None
    truck_id: Optional[str] = None
    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)
    speed_kmh: float = Field(default=0.0, ge=0.0, le=200.0)
    heading: float = Field(default=0.0, ge=0.0, le=360.0)
    battery_level: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    is_charging: bool = False
    is_sos: bool = False
    idempotency_key: Optional[str] = None

class EmergencySOSAlert(BaseModel):
    driver_id: str = Field(..., min_length=1)
    driver_name: Optional[str] = None
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
    load_slip_id: Optional[str] = None
    hub_id: Optional[str] = None
    hub_name: Optional[str] = None
    customer_name: str = Field(..., min_length=1)
    product_id: Optional[str] = None
    product_name: Optional[str] = "Ice Product"
    sack_size: Optional[str] = "Standard Sack"
    planned_qty: int = Field(..., ge=0)
    delivered_qty: int = Field(..., ge=0)
    returned_qty: int = Field(default=0, ge=0)
    unit_price: float = Field(..., ge=0.0)
    total_amount: float = Field(..., ge=0.0)
    payment_method: str = Field(default="cash")
    partial_reason: Optional[str] = None
    is_adhoc: bool = False
    timestamp: Optional[str] = None

class BatchSyncPayload(BaseModel):
    driver_id: Optional[str] = None
    truck_id: Optional[str] = None
    load_slip_id: Optional[str] = None
    transactions: List[SyncTransactionItem] = Field(default_factory=list)
    gps_pings: List[TelemetryPing] = Field(default_factory=list)

# --- SECURITY & SESSION HELPERS ---
JWT_SECRET = os.getenv("JWT_SECRET", "jica_secret_jwt_key_cdo_production_994821a8f9024bce")
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_DAYS = 7

def hash_password(plain_password: str) -> str:
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(plain_password.encode("utf-8"), salt).decode("utf-8")

def verify_password(plain_password: str, hashed_or_plain: Optional[str]) -> bool:
    if not hashed_or_plain or not plain_password:
        return False
    if hashed_or_plain.startswith(("$2b$", "$2a$", "$2y$")):
        try:
            return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_or_plain.encode("utf-8"))
        except Exception:
            return False
    return plain_password == hashed_or_plain

def create_access_token(user_id: str, username: str, role: str, session_token: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "sid": session_token,
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

async def require_active_session(
    request: Request,
    x_session_token: Optional[str] = Header(default=None),
    x_user_id: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """
    Validates strict single active session against user_accounts.active_session_token.
    Accepts X-Session-Token + X-User-Id or Bearer JWT containing sub & sid.
    """
    user_id = x_user_id
    session_token = x_session_token

    if authorization and authorization.lower().startswith("bearer "):
        raw_jwt = authorization.split(" ", 1)[1].strip()
        try:
            decoded = jwt.decode(raw_jwt, JWT_SECRET, algorithms=[JWT_ALGORITHM])
            user_id = user_id or decoded.get("sub")
            session_token = session_token or decoded.get("sid")
        except jwt.InvalidTokenError:
            raise HTTPException(status_code=401, detail="Invalid or expired authorization token.")

    if not user_id or not session_token:
        raise HTTPException(
            status_code=401,
            detail="Authentication required: missing active session credentials."
        )

    try:
        res = (
            supabase.table("user_accounts")
            .select("id, full_name, role, username, email, phone_number, assigned_truck_id, is_active, deleted_at, active_session_token")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Session validation database error: {str(e)}")

    if not res.data:
        raise HTTPException(status_code=401, detail="User account not found.")

    acc = res.data[0]
    if not acc.get("is_active", True) or acc.get("deleted_at") is not None:
        raise HTTPException(status_code=401, detail="This account is inactive or deactivated.")

    db_session = acc.get("active_session_token")
    if not db_session or db_session != session_token:
        raise HTTPException(
            status_code=401,
            detail="SESSION_REVOKED: You have been logged out because this account was signed in on another device."
        )

    # Touch last_active_at asynchronously/cleanly
    try:
        supabase.table("user_accounts").update({
            "last_active_at": datetime.now(timezone.utc).isoformat()
        }).eq("id", acc["id"]).eq("active_session_token", session_token).execute()
    except Exception:
        pass

    return acc

async def optional_active_session(
    request: Request,
    x_session_token: Optional[str] = Header(default=None),
    x_user_id: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
) -> Optional[Dict[str, Any]]:
    if not (x_session_token or authorization):
        return None
    return await require_active_session(request, x_session_token, x_user_id, authorization)

# --- INTERNAL SHARED RECONCILIATION & INVOICE SYNC HELPERS ---

def _recalculate_slip_totals_and_invoices(load_slip_id: str, fallback_driver_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Recalculates delivered/returned sacks and cash/charge totals on load_slips and syncs itemized invoices."""
    slip_res = supabase.table("load_slips").select("*").eq("id", load_slip_id).limit(1).execute()
    if not slip_res.data:
        return None
    slip = slip_res.data[0]

    items_res = supabase.table("load_slip_items").select("*").eq("load_slip_id", load_slip_id).execute()
    items = items_res.data or []

    total_loaded = 0
    total_delivered = 0
    total_returned = 0
    total_cash = Decimal("0.00")
    total_charge = Decimal("0.00")

    truck_plate = slip.get("truck_plate") or slip.get("truck_id")
    driver_name = slip.get("driver_name") or fallback_driver_name
    in_charge_name = slip.get("in_charge_name") or slip.get("created_by")

    # Resolve truck/driver/in-charge names cleanly if missing
    if not slip.get("truck_plate") and slip.get("truck_id"):
        trk = supabase.table("trucks").select("plate_number").eq("id", slip["truck_id"]).limit(1).execute()
        if trk.data:
            truck_plate = trk.data[0].get("plate_number") or truck_plate
    if not driver_name and slip.get("driver_id"):
        drv = supabase.table("user_accounts").select("full_name").eq("id", slip["driver_id"]).limit(1).execute()
        if drv.data:
            driver_name = drv.data[0].get("full_name")

    is_finished = (slip.get("status") or "").upper() in FINISHED_AWAITING_REMIT_STATUSES | REMITTED_OR_DONE_STATUSES

    for idx, it in enumerate(items, start=1):
        planned = int(it.get("planned_qty") or 0)
        delivered = int(it.get("delivered_qty") or 0)
        returned = int(it.get("returned_qty") or max(0, planned - delivered))
        unit_price = Decimal(str(it.get("unit_price") or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        item_completed = (it.get("status") or "").lower() in ("completed", "partial") or it.get("delivered_at") is not None
        qty_for_money = delivered if (item_completed or is_finished) else planned
        line_amount = Decimal(str(it.get("total_amount") or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if item_completed or is_finished:
            line_amount = (unit_price * Decimal(delivered)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        total_loaded += planned
        if item_completed or is_finished:
            total_delivered += delivered
            total_returned += returned

        pay_mode = it.get("payment_mode") or "Cash"
        is_charge = str(pay_mode).lower().startswith("charge")
        if is_charge:
            total_charge += line_amount
        else:
            total_cash += line_amount

        # Keep invoice synced accurately
        if item_completed or is_finished:
            inv_status = "Unpaid Charge" if is_charge else "Cash Paid"
            existing_inv = supabase.table("invoices").select("id, status").eq("load_slip_item_id", it["id"]).limit(1).execute()
            inv_update = {
                "slip_number": slip.get("slip_number"),
                "truck_id": slip.get("truck_id"),
                "truck_plate": truck_plate,
                "driver_id": slip.get("driver_id"),
                "driver_name": driver_name or "Driver",
                "in_charge_id": slip.get("in_charge_id"),
                "in_charge_name": in_charge_name or "Dispatch Staff",
                "customer_name": it.get("customer_name") or "Customer Store",
                "hub_name": it.get("hub_name") or "Cagayan de Oro Hub",
                "product_name": it.get("product_name") or "Ice Product",
                "sack_size": it.get("sack_size") or "Standard Sack",
                "planned_qty": planned,
                "delivered_qty": delivered,
                "returned_qty": returned,
                "unit_price": float(unit_price),
                "total_amount": float(line_amount),
                "payment_type": "Charge" if is_charge else "Cash",
                "updated_at": datetime.now(timezone.utc).isoformat()
            }
            if existing_inv.data:
                curr_inv_status = existing_inv.data[0].get("status")
                if curr_inv_status != "Settled":
                    inv_update["status"] = inv_status
                supabase.table("invoices").update(inv_update).eq("id", existing_inv.data[0]["id"]).execute()
            else:
                slip_suffix = str(load_slip_id).replace("-", "")[-4:].upper()
                prefix = "CI" if is_charge else "OR"
                date_compact = str(slip.get("date") or manila_date_str()).replace("-", "")
                inv_num = f"{prefix}-{date_compact}-{slip_suffix}-{idx:02d}"
                inv_update.update({
                    "invoice_number": inv_num,
                    "idempotency_key": f"INV-{it['id']}",
                    "load_slip_id": load_slip_id,
                    "load_slip_item_id": it["id"],
                    "status": inv_status,
                    "issue_date": slip.get("date") or manila_date_str(),
                    "issued_at": datetime.now(timezone.utc).isoformat()
                })
                supabase.table("invoices").upsert(inv_update, on_conflict="idempotency_key").execute()

    update_slip = {
        "total_loaded_sacks": total_loaded,
        "total_delivered_sacks": total_delivered,
        "total_returned_sacks": total_returned,
        "total_cash_revenue": float(total_cash),
        "total_charge_revenue": float(total_charge),
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    updated = supabase.table("load_slips").update(update_slip).eq("id", load_slip_id).execute()
    return updated.data[0] if updated.data else {**slip, **update_slip}

# --- SYSTEM & HEALTH ENDPOINTS ---

@app.get("/")
def root():
    return {
        "service": "JICA Ice Logistics & Delivery Backend",
        "status": "online",
        "region": "Cagayan de Oro City (Asia/Manila)",
        "manila_date": manila_date_str(),
        "version": "2.2.0"
    }

@app.get("/api/v1/health")
@app.get("/api/health")
def health_check():
    return {
        "status": "ok",
        "timestamp": time.time(),
        "manila_date": manila_date_str(),
        "database": "connected"
    }

@app.get("/api/v1/keep-alive")
def keep_alive():
    try:
        supabase.table("user_accounts").select("id").limit(1).execute()
        return {"status": "active", "timestamp": time.time(), "ping": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database keep-alive failed: {str(e)}")

# --- AUTHENTICATION & STRICT SINGLE ACTIVE SESSION ---

@app.post("/api/v1/auth/login")
@app.post("/auth/login")
def login_user(req: LoginRequest):
    """Bcrypt auth + strict single active session token generation."""
    try:
        clean_user = req.username.strip().lower()
        res = supabase.table("user_accounts").select("*").ilike("username", clean_user).eq("is_active", True).execute()

        if not res.data:
            res = supabase.table("user_accounts").select("*").ilike("email", clean_user).eq("is_active", True).execute()

        if not res.data:
            res = supabase.table("user_accounts").select("*").ilike("full_name", clean_user).eq("is_active", True).execute()

        if not res.data:
            raise HTTPException(status_code=401, detail="Invalid username or password.")

        account = res.data[0]
        if account.get("deleted_at") is not None:
            raise HTTPException(status_code=401, detail="This account has been deactivated or soft-deleted.")

        stored_hash = account.get("password_hash")
        stored_plain = account.get("password")
        stored_pin = account.get("mobile_pin")

        is_valid = False
        if stored_hash:
            is_valid = verify_password(req.password, stored_hash)

        if not is_valid:
            if stored_plain and verify_password(req.password, stored_plain):
                is_valid = True
            elif stored_pin and verify_password(req.password, stored_pin):
                is_valid = True

        if not is_valid:
            raise HTTPException(status_code=401, detail="Invalid username or password.")

        user_role = account.get("role")
        if req.app == "web" and user_role not in ("Owner", "Staff"):
            raise HTTPException(
                status_code=403,
                detail="Access Denied: Only the Owner and Office Staff can sign in to the Web Dashboard."
            )
        if req.app == "mobile" and user_role != "Driver":
            raise HTTPException(
                status_code=403,
                detail="Access Denied: Mobile app is strictly for Delivery Drivers."
            )

        # Generate brand-new strict single session token
        session_token = str(uuid.uuid4())
        now_utc = datetime.now(timezone.utc).isoformat()

        update_fields: Dict[str, Any] = {
            "active_session_token": session_token,
            "last_active_at": now_utc
        }
        if not stored_hash:
            update_fields["password_hash"] = hash_password(req.password)
            update_fields["password"] = None

        try:
            supabase.table("user_accounts").update(update_fields).eq("id", account["id"]).execute()
        except Exception as mig_err:
            # Fallback if session columns weren't migrated yet (attempt without session fields, though migration should run)
            print(f"[LOGIN UPDATE NOTICE] {mig_err}")
            if not stored_hash:
                try:
                    supabase.table("user_accounts").update({
                        "password_hash": update_fields["password_hash"],
                        "password": None
                    }).eq("id", account["id"]).execute()
                except Exception:
                    pass

        jwt_token = create_access_token(
            account["id"],
            account.get("username") or clean_user,
            user_role,
            session_token
        )

        log_audit("user_accounts", account["id"], "LOGIN", account.get("full_name", clean_user), new_data={"app": req.app})

        account_safe = {
            k: v for k, v in account.items()
            if k not in ("password", "password_hash", "mobile_pin")
        }
        account_safe["active_session_token"] = session_token
        account_safe["last_active_at"] = now_utc

        return {
            "status": "success",
            "token": jwt_token,
            "session_token": session_token,
            "user": account_safe
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/auth/logout")
@app.post("/auth/logout")
def logout_user(
    req: LogoutRequest,
    x_session_token: Optional[str] = Header(default=None),
    x_user_id: Optional[str] = Header(default=None),
):
    """Clears active_session_token ONLY if the caller holds the current active session token."""
    user_id = req.user_id or x_user_id
    session_token = req.session_token or x_session_token

    if not user_id:
        return {"status": "success", "message": "Logged out locally."}

    try:
        query = supabase.table("user_accounts").update({
            "active_session_token": None
        }).eq("id", user_id)
        if session_token:
            query = query.eq("active_session_token", session_token)
        query.execute()
        log_audit("user_accounts", user_id, "LOGOUT", user_id)
    except Exception as e:
        print(f"[LOGOUT NOTICE] {e}")

    return {"status": "success", "message": "Session closed cleanly."}

@app.post("/api/v1/auth/validate-session")
@app.get("/api/v1/auth/validate-session")
def validate_session(
    req: Optional[ValidateSessionRequest] = None,
    x_session_token: Optional[str] = Header(default=None),
    x_user_id: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
):
    user_id = (req.user_id if req else None) or x_user_id
    session_token = (req.session_token if req else None) or x_session_token

    if authorization and authorization.lower().startswith("bearer "):
        try:
            decoded = jwt.decode(authorization.split(" ", 1)[1].strip(), JWT_SECRET, algorithms=[JWT_ALGORITHM])
            user_id = user_id or decoded.get("sub")
            session_token = session_token or decoded.get("sid")
        except jwt.InvalidTokenError:
            raise HTTPException(status_code=401, detail="Invalid token.")

    if not user_id or not session_token:
        raise HTTPException(status_code=401, detail="Missing session credentials.")

    res = (
        supabase.table("user_accounts")
        .select("id, full_name, role, username, email, assigned_truck_id, is_active, deleted_at, active_session_token")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=401, detail="Account not found.")

    acc = res.data[0]
    if not acc.get("is_active", True) or acc.get("deleted_at") is not None:
        raise HTTPException(status_code=401, detail="Account is deactivated.")

    db_token = acc.get("active_session_token")
    if not db_token or db_token != session_token:
        raise HTTPException(
            status_code=401,
            detail="SESSION_REVOKED: You have been logged out because this account was signed in on another device."
        )

    try:
        supabase.table("user_accounts").update({
            "last_active_at": datetime.now(timezone.utc).isoformat()
        }).eq("id", user_id).eq("active_session_token", session_token).execute()
    except Exception:
        pass

    return {"valid": True, "user": acc}

@app.post("/api/v1/auth/change-password")
@app.post("/auth/change-password")
def change_password(req: PasswordChangeRequest):
    try:
        res = supabase.table("user_accounts").select("*").eq("id", req.user_id).execute()
        if not res.data:
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
    if not SMTP_EMAIL or not SMTP_APP_PASSWORD:
        print(f"[SMTP SIMULATION] Sent OTP {otp_code} to {to_email}.")
        return True

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"JICA Ice Logistics - Verification Code: {otp_code}"
    msg["From"] = f"JICA Logistics Dispatch <{SMTP_EMAIL}>"
    msg["To"] = to_email

    html_content = f"""
    <div style="font-family: Arial, sans-serif; padding: 24px; background: #f8fafc; color: #1e293b;">
      <div style="max-width: 480px; margin: 0 auto; background: #ffffff; border-radius: 16px; border: 1px solid #e2e8f0; padding: 32px;">
        <h2 style="color: #00875A; margin: 0 0 8px 0;">J.I.C.A ICE LOGISTICS</h2>
        <p style="font-size: 14px;">Hello <strong>{user_name}</strong>,</p>
        <p style="font-size: 13px; color: #475569;">Your 6-digit password reset verification code is:</p>
        <div style="background: #f0fdf4; border: 2px dashed #00875A; border-radius: 12px; padding: 20px; text-align: center; margin: 20px 0;">
          <span style="font-size: 34px; font-weight: 900; letter-spacing: 8px; color: #00875A; font-family: monospace;">{otp_code}</span>
        </div>
        <p style="font-size: 12px; color: #64748b;">Valid for 10 minutes.</p>
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
    clean_id = req.identifier.strip().lower()
    res = supabase.table("user_accounts").select("*").ilike("username", clean_id).eq("is_active", True).execute()
    if not res.data:
        res = supabase.table("user_accounts").select("*").ilike("email", clean_id).eq("is_active", True).execute()

    if not res.data:
        raise HTTPException(status_code=404, detail="No active account found matching this username or email address.")

    account = res.data[0]
    email = account.get("email")
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="No Gmail address registered for this account.")

    otp_code = f"{random.randint(100000, 999999)}"
    otp_hash = hashlib.sha256(otp_code.encode("utf-8")).hexdigest()
    expires_at = time.time() + 600
    store_key = account["id"]
    entry = {
        "otp_hash": otp_hash,
        "expires_at": expires_at,
        "user_id": account["id"],
        "email": email
    }
    _otp_store[store_key] = entry
    if account.get("username"):
        _otp_store[account["username"].lower()] = entry
    _otp_store[email.lower()] = entry

    send_otp_email(email, otp_code, account.get("full_name", "Employee"))

    parts = email.split("@")
    user_part = parts[0]
    domain_part = parts[1] if len(parts) > 1 else "gmail.com"
    masked_user = user_part[0] + "***" + user_part[-1] if len(user_part) > 2 else user_part[0] + "***"
    masked_email = f"{masked_user}@{domain_part}"

    return {
        "status": "success",
        "message": f"Verification code sent to {masked_email}.",
        "masked_email": masked_email,
        "email_masked": masked_email,
        "expires_in_minutes": 10
    }

@app.post("/api/v1/auth/verify-otp-reset")
@app.post("/auth/verify-otp-reset")
@limiter.limit("10/minute")
def verify_otp_reset(req: VerifyOtpResetRequest, request: Request):
    clean_id = req.identifier.strip().lower()
    raw_otp = (req.otp or req.otp_code or "").strip()
    if len(raw_otp) < 4:
        raise HTTPException(status_code=400, detail="Verification code is required.")

    stored = _otp_store.get(clean_id)
    if not stored:
        for v in _otp_store.values():
            if v.get("email", "").lower() == clean_id:
                stored = v
                break

    if not stored:
        raise HTTPException(status_code=400, detail="No active password reset request found or code expired.")

    if time.time() > stored["expires_at"]:
        _otp_store.pop(clean_id, None)
        raise HTTPException(status_code=400, detail="Verification code has expired. Please request a new code.")

    entered_hash = hashlib.sha256(raw_otp.encode("utf-8")).hexdigest()
    if entered_hash != stored["otp_hash"]:
        raise HTTPException(status_code=400, detail="Invalid verification code.")

    new_hashed = hash_password(req.new_password)
    user_id = stored["user_id"]
    supabase.table("user_accounts").update({
        "password_hash": new_hashed,
        "password": None,
        "mobile_pin": req.new_password
    }).eq("id", user_id).execute()

    _otp_store.pop(clean_id, None)
    _otp_store.pop(user_id, None)
    log_audit("user_accounts", user_id, "PASSWORD_RESET_VIA_OTP", stored.get("email", "User"))

    return {"status": "success", "message": "Password updated successfully! You can now log in with your new password."}

@app.post("/api/v1/auth/update-email")
@app.post("/auth/update-email")
def update_user_email(req: UpdateEmailRequest):
    clean_email = req.email.strip().lower()
    if "@" not in clean_email:
        raise HTTPException(status_code=400, detail="Invalid email format.")

    res = supabase.table("user_accounts").update({"email": clean_email}).eq("id", req.user_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="User account not found.")

    log_audit("user_accounts", req.user_id, "UPDATE_GMAIL", clean_email)
    return {"status": "success", "message": "Gmail address saved successfully.", "email": clean_email}

@app.post("/api/v1/auth/create-user")
@app.post("/api/v1/users")
def create_user(req: UserAccountCreate):
    try:
        raw_pass = req.password or "1234"
        hashed = hash_password(raw_pass)
        clean_username = req.username.strip().lower() if req.username else req.full_name.strip().lower().replace(" ", "")

        safe_photo = sanitize_image_url(req.photo_url)
        payload: Dict[str, Any] = {
            "full_name": req.full_name.strip(),
            "role": req.role,
            "username": clean_username,
            "password_hash": hashed,
            "password": None,
            "mobile_pin": req.mobile_pin or raw_pass,
            "phone_number": req.phone_number,
            "email": req.email,
            "assigned_truck_id": req.assigned_truck_id or None,
            "photo_url": safe_photo,
            "is_active": True
        }
        if req.id and is_uuid(req.id):
            payload["id"] = req.id

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
    try:
        old_photo = None
        if req.photo_url is not None:
            prev = supabase.table("user_accounts").select("photo_url").eq("id", user_id).limit(1).execute()
            if prev.data:
                old_photo = prev.data[0].get("photo_url")

        update_data: Dict[str, Any] = {}
        if req.full_name is not None: update_data["full_name"] = req.full_name.strip()
        if req.role is not None: update_data["role"] = req.role
        if req.username is not None: update_data["username"] = req.username.strip().lower()
        if req.phone_number is not None: update_data["phone_number"] = req.phone_number
        if req.email is not None: update_data["email"] = req.email
        if req.assigned_truck_id is not None: update_data["assigned_truck_id"] = req.assigned_truck_id or None
        if req.photo_url is not None:
            new_photo = sanitize_image_url(req.photo_url)
            update_data["photo_url"] = new_photo
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

        if req.photo_url is not None and old_photo and old_photo != update_data.get("photo_url"):
            delete_jica_asset_url(old_photo)

        log_audit("user_accounts", user_id, "UPDATE_USER", "Admin")
        safe_user = {k: v for k, v in res.data[0].items() if k not in ("password", "password_hash", "mobile_pin")}
        return {"status": "success", "user": safe_user}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- DISPATCH REFERENCE DATA & SETTINGS CRUD ---

@app.get("/api/v1/products")
def get_products():
    try:
        res = (
            supabase.table("products")
            .select("id, code, name, category, description, sack_size, sack_weight_kg, standard_price, image_url, created_at")
            .eq("is_active", True)
            .is_("deleted_at", "null")
            .order("created_at")
            .execute()
        )
        return {"status": "success", "data": res.data or []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/products")
def create_product(req: ProductCreateRequest):
    try:
        prod_id = req.id if (req.id and is_uuid(req.id)) else str(uuid.uuid4())
        clean_sack = (req.sack_size or "Medium").strip()
        compat_cat = (req.category or clean_sack).strip()
        safe_img = sanitize_image_url(req.image_url)
        payload = {
            "id": prod_id,
            "code": f"PROD-{prod_id[:8].upper()}",
            "name": req.name.strip(),
            "description": clean_sack,
            "category": compat_cat,
            "sack_size": clean_sack,
            "standard_price": round_currency(req.standard_price),
            "image_url": safe_img,
            "is_active": True
        }
        res = supabase.table("products").insert(payload).execute()
        return {"status": "success", "data": res.data[0] if res.data else payload}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/v1/products/{product_id}")
def update_product(product_id: str, req: ProductUpdateRequest):
    try:
        old_img = None
        if req.image_url is not None:
            prev = supabase.table("products").select("image_url").eq("id", product_id).limit(1).execute()
            if prev.data:
                old_img = prev.data[0].get("image_url")

        updates: Dict[str, Any] = {}
        if req.name is not None: updates["name"] = req.name.strip()
        if req.sack_size is not None:
            clean_sack = req.sack_size.strip()
            updates["sack_size"] = clean_sack
            updates["description"] = clean_sack
            updates["category"] = req.category or clean_sack
        elif req.category is not None:
            updates["category"] = req.category
            updates["description"] = req.category
        if req.standard_price is not None: updates["standard_price"] = round_currency(max(0.0, req.standard_price))
        if req.image_url is not None:
            updates["image_url"] = sanitize_image_url(req.image_url)

        res = supabase.table("products").update(updates).eq("id", product_id).execute()
        if req.image_url is not None and old_img and old_img != updates.get("image_url"):
            delete_jica_asset_url(old_img)
        return {"status": "success", "data": res.data[0] if res.data else updates}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/trucks")
def get_trucks():
    try:
        res = (
            supabase.table("trucks")
            .select("id, plate_number, model, sack_capacity, max_sack_capacity, image_url, is_active")
            .eq("is_active", True)
            .is_("deleted_at", "null")
            .order("id")
            .execute()
        )
        return {"status": "success", "data": res.data or []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/trucks")
def create_truck(req: TruckUpsertRequest):
    try:
        clean_id = req.id.strip().upper()
        safe_img = sanitize_image_url(req.image_url)
        payload = {
            "id": clean_id,
            "plate_number": req.plate_number.strip().upper(),
            "model": req.model.strip(),
            "sack_capacity": req.sack_capacity,
            "max_sack_capacity": req.sack_capacity,
            "image_url": safe_img,
            "is_active": True,
            "deleted_at": None,
            "deleted_by": None
        }
        res = supabase.table("trucks").upsert(payload, on_conflict="id").execute()
        return {"status": "success", "data": res.data[0] if res.data else payload}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/v1/trucks/{truck_id}")
def update_truck(truck_id: str, req: TruckUpdateRequest):
    try:
        old_img = None
        if req.image_url is not None:
            prev = supabase.table("trucks").select("image_url").eq("id", truck_id).limit(1).execute()
            if prev.data:
                old_img = prev.data[0].get("image_url")

        updates: Dict[str, Any] = {}
        if req.plate_number is not None: updates["plate_number"] = req.plate_number.strip().upper()
        if req.model is not None: updates["model"] = req.model.strip()
        if req.sack_capacity is not None:
            updates["sack_capacity"] = req.sack_capacity
            updates["max_sack_capacity"] = req.sack_capacity
        if req.image_url is not None:
            updates["image_url"] = sanitize_image_url(req.image_url)

        res = supabase.table("trucks").update(updates).eq("id", truck_id).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="Truck not found.")
        if req.image_url is not None and old_img and old_img != updates.get("image_url"):
            delete_jica_asset_url(old_img)
        return {"status": "success", "data": res.data[0]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/trucks/capacity")
def get_trucks_capacity():
    """Returns live capacity, active loaded sacks, remaining sacks, and in-transit status per active truck."""
    try:
        trucks_res = (
            supabase.table("trucks")
            .select("id, plate_number, model, sack_capacity, max_sack_capacity")
            .eq("is_active", True)
            .is_("deleted_at", "null")
            .order("id")
            .execute()
        )
        trucks = trucks_res.data or []

        slips_res = supabase.table("load_slips").select("id, truck_id, status, total_loaded_sacks, date").is_("deleted_at", "null").execute()
        slips = slips_res.data or []

        usage_map: Dict[str, Dict[str, Any]] = {}
        for s in slips:
            if not is_unremitted_status(s.get("status")):
                continue
            tid = s.get("truck_id")
            if not tid:
                continue
            if tid not in usage_map:
                usage_map[tid] = {"used_sacks": 0, "active_slips": 0, "in_transit": False}
            usage_map[tid]["used_sacks"] += int(s.get("total_loaded_sacks") or 0)
            usage_map[tid]["active_slips"] += 1
            if (s.get("status") or "").upper() in ("IN_TRANSIT", "IN-TRANSIT"):
                usage_map[tid]["in_transit"] = True

        result = []
        for t in trucks:
            cap = int(t.get("sack_capacity") or t.get("max_sack_capacity") or 120)
            u = usage_map.get(t["id"], {"used_sacks": 0, "active_slips": 0, "in_transit": False})
            remaining = max(0, cap - u["used_sacks"])
            result.append({
                "truck_id": t["id"],
                "plate_number": t.get("plate_number"),
                "model": t.get("model"),
                "sack_capacity": cap,
                "used_sacks": u["used_sacks"],
                "remaining_capacity": remaining,
                "active_slips_count": u["active_slips"],
                "is_in_transit": u["in_transit"],
            })

        return {"status": "success", "data": result, "manila_date": manila_date_str()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/hubs")
def get_hubs():
    try:
        res = (
            supabase.table("delivery_hubs")
            .select("id, code, name, address, latitude, longitude, zone, photo_url")
            .eq("is_active", True)
            .is_("deleted_at", "null")
            .order("name")
            .execute()
        )
        return {"status": "success", "data": res.data or []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/hubs")
def create_hub(req: HubCreateRequest):
    try:
        hub_id = req.id if (req.id and is_uuid(req.id)) else str(uuid.uuid4())
        safe_photo = sanitize_image_url(req.photo_url)
        payload = {
            "id": hub_id,
            "code": f"HUB-{hub_id[:8].upper()}",
            "name": req.name.strip(),
            "address": req.address.strip(),
            "latitude": req.latitude,
            "longitude": req.longitude,
            "zone": req.zone or "City Proper",
            "photo_url": safe_photo,
            "is_active": True
        }
        res = supabase.table("delivery_hubs").insert(payload).execute()
        return {"status": "success", "data": res.data[0] if res.data else payload}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/v1/hubs/{hub_id}")
def update_hub(hub_id: str, req: HubUpdateRequest):
    try:
        old_photo = None
        if req.photo_url is not None:
            prev = supabase.table("delivery_hubs").select("photo_url").eq("id", hub_id).limit(1).execute()
            if prev.data:
                old_photo = prev.data[0].get("photo_url")

        updates: Dict[str, Any] = {}
        if req.name is not None: updates["name"] = req.name.strip()
        if req.address is not None: updates["address"] = req.address.strip()
        if req.latitude is not None: updates["latitude"] = req.latitude
        if req.longitude is not None: updates["longitude"] = req.longitude
        if req.zone is not None: updates["zone"] = req.zone
        if req.photo_url is not None:
            updates["photo_url"] = sanitize_image_url(req.photo_url)

        res = supabase.table("delivery_hubs").update(updates).eq("id", hub_id).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="Delivery hub not found.")
        if req.photo_url is not None and old_photo and old_photo != updates.get("photo_url"):
            delete_jica_asset_url(old_photo)
        return {"status": "success", "data": res.data[0]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/users")
def get_users():
    try:
        res = supabase.table("user_accounts").select("id, full_name, role, username, email, phone_number, assigned_truck_id, photo_url, is_active, created_at, deleted_at, last_active_at").eq("is_active", True).is_("deleted_at", "null").order("created_at").execute()
        return {"status": "success", "data": res.data or []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- LOAD SLIPS, ATOMIC CONCURRENT DISPATCH & SHIFT CLOSING ---

@app.get("/api/v1/load-slips")
def list_load_slips(
    truck_id: Optional[str] = None,
    driver_id: Optional[str] = None,
    status_filter: Optional[str] = Query(default=None, alias="status"),
    date_filter: Optional[str] = Query(default=None, alias="date"),
    active_only: bool = False
):
    try:
        query = (
            supabase.table("load_slips")
            .select("*, load_slip_items(*, delivery_hubs(*))")
            .is_("deleted_at", "null")
            .order("created_at", desc=True)
        )
        if truck_id:
            query = query.eq("truck_id", truck_id)
        if driver_id and is_uuid(driver_id):
            query = query.eq("driver_id", driver_id)
        if date_filter:
            query = query.eq("date", date_filter)

        res = query.execute()
        rows = res.data or []

        if active_only:
            rows = [r for r in rows if is_unremitted_status(r.get("status"))]
        elif status_filter:
            target = status_filter.strip().upper()
            rows = [r for r in rows if (r.get("status") or "").strip().upper() == target]

        return {"status": "success", "data": rows}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/driver/active-dispatch")
def get_driver_active_dispatch(
    driver_id: Optional[str] = None,
    truck_id: Optional[str] = None,
    shift_date: Optional[str] = None
):
    """Fetches active dispatched/in-transit load slip(s) for mobile driver app (timezone-aware Asia/Manila)."""
    try:
        target_date = shift_date or manila_date_str()
        query = (
            supabase.table("load_slips")
            .select("*, load_slip_items(*, delivery_hubs(*)), trucks(id, plate_number, model, sack_capacity, max_sack_capacity)")
            .is_("deleted_at", "null")
            .order("created_at", desc=True)
            .limit(20)
        )

        # Build valid Postgres OR filter carefully (never send non-UUID to UUID column)
        or_parts = []
        if truck_id:
            or_parts.append(f"truck_id.eq.{truck_id}")
        if driver_id and is_uuid(driver_id):
            or_parts.append(f"driver_id.eq.{driver_id}")

        if or_parts:
            query = query.or_(",".join(or_parts))

        res = query.execute()
        rows = res.data or []

        # Prioritize active/in-transit dispatches with items, preferring driver_id match then today Manila date
        active_rows = [
            r for r in rows
            if (r.get("status") or "").upper() in ("DISPATCHED", "ACTIVE", "PENDING", "IN_TRANSIT", "IN-TRANSIT")
            and len(r.get("load_slip_items") or []) > 0
        ]

        # Sort so exact driver_id match comes first, then date == today Manila, then newest created_at
        active_rows.sort(
            key=lambda r: (
                1 if (driver_id and r.get("driver_id") == driver_id) else 0,
                1 if r.get("date") == target_date else 0,
                r.get("created_at") or ""
            ),
            reverse=True
        )

        # Also check if driver recently finished a delivery today (for viewing summary if needed)
        finished_rows = [
            r for r in rows
            if (r.get("status") or "").upper() in FINISHED_AWAITING_REMIT_STATUSES
            and r.get("date") == target_date
            and len(r.get("load_slip_items") or []) > 0
        ]

        return {
            "status": "success",
            "manila_date": target_date,
            "active_dispatch": active_rows[0] if active_rows else None,
            "all_active_dispatches": active_rows,
            "finished_today": finished_rows[0] if finished_rows else None
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/load-slips")
@app.post("/load-slips")
def create_load_slip(
    req: CreateLoadSlipRequest,
    auth_user: Dict[str, Any] = Depends(require_active_session)
):
    """
    Atomic Load Slip Creation & Dispatch:
    - Enforces In-Charge strictly from authenticated session user
    - Locks target truck to guarantee no concurrent capacity overbooking
    - Atomically creates Load Slip, Load Slip Items, and itemized Invoices
    """
    in_charge_id = auth_user["id"]
    in_charge_name = auth_user["full_name"]

    if auth_user.get("role") not in ("Owner", "Staff"):
        raise HTTPException(status_code=403, detail="Only Owner or Staff can dispatch Load Slips.")

    shift_date = req.date or manila_date_str()
    dep_time = req.departure_time or manila_time_str()
    unique_suffix = uuid.uuid4().hex[:4].upper()
    slip_number = req.slip_number or f"LS-{shift_date.replace('-', '')}-{req.truck_id}-{unique_suffix}"

    # Look up driver name
    driver_name = req.driver_name
    driver_id_clean = req.driver_id if is_uuid(req.driver_id) else None
    if driver_id_clean and not driver_name:
        drv_res = supabase.table("user_accounts").select("full_name").eq("id", driver_id_clean).limit(1).execute()
        if drv_res.data:
            driver_name = drv_res.data[0].get("full_name")

    # Prepare normalized items with Decimal currency rounding
    items_payload = []
    requested_sacks = 0
    total_cash = Decimal("0.00")
    total_charge = Decimal("0.00")

    for idx, item in enumerate(req.items, start=1):
        unit_price = Decimal(str(round_currency(item.unit_price)))
        line_total_raw = item.total_amount if item.total_amount is not None else float(unit_price * item.planned_qty)
        line_total = Decimal(str(round_currency(line_total_raw)))
        requested_sacks += item.planned_qty
        is_charge = "charge" in (item.payment_mode or "").lower()
        if is_charge:
            total_charge += line_total
        else:
            total_cash += line_total

        items_payload.append({
            "idempotency_key": item.idempotency_key or f"{req.idempotency_key}-{idx}",
            "customer_id": item.customer_id if is_uuid(item.customer_id) else None,
            "customer_name": item.customer_name.strip(),
            "hub_id": item.hub_id if is_uuid(item.hub_id) else None,
            "hub_name": item.hub_name or "Cagayan de Oro Hub",
            "product_id": item.product_id if is_uuid(item.product_id) else None,
            "product_name": item.product_name,
            "sack_size": item.sack_size or "Standard Sack",
            "planned_qty": item.planned_qty,
            "unit_price": float(unit_price),
            "total_amount": float(line_total),
            "payment_mode": "Charge" if is_charge else "Cash"
        })

    # Try atomic PostgreSQL RPC first (true DB-level FOR UPDATE lock)
    try:
        rpc_res = supabase.rpc("dispatch_load_slip_atomic", {
            "p_slip_number": slip_number,
            "p_idempotency_key": req.idempotency_key,
            "p_date": shift_date,
            "p_departure_time": dep_time,
            "p_truck_id": req.truck_id,
            "p_in_charge_id": in_charge_id,
            "p_in_charge_name": in_charge_name,
            "p_driver_id": driver_id_clean,
            "p_driver_name": driver_name or "Assigned Driver",
            "p_notes": req.notes,
            "p_items": items_payload
        }).execute()

        rpc_data = rpc_res.data
        if isinstance(rpc_data, dict):
            if not rpc_data.get("ok"):
                code = rpc_data.get("code")
                if code == "CAPACITY_EXCEEDED":
                    raise HTTPException(
                        status_code=409,
                        detail=rpc_data.get("message") or f"Capacity exceeded: Another staff member just dispatched to this truck. Only {rpc_data.get('remaining_sacks', 0)} sacks remaining."
                    )
                raise HTTPException(status_code=400, detail=rpc_data.get("message") or "Dispatch rejected.")

            # Fetch full inserted slip with items
            slip_id = rpc_data["load_slip_id"]
            full_res = supabase.table("load_slips").select("*, load_slip_items(*, delivery_hubs(*))").eq("id", slip_id).single().execute()
            log_audit("load_slips", slip_id, "DISPATCH", in_charge_name, new_data={"slip_number": rpc_data.get("slip_number"), "sacks": requested_sacks})
            return {
                "status": "success",
                "message": f"Load Slip #{rpc_data.get('slip_number')} saved & dispatched!",
                "data": full_res.data,
                "remaining_capacity": rpc_data.get("remaining_sacks"),
                "in_transit_warning": rpc_data.get("in_transit_warning", False)
            }
    except HTTPException:
        raise
    except Exception as rpc_err:
        print(f"[DISPATCH RPC FALLBACK] {rpc_err}")

    # Application-level locked fallback (when RPC is not yet installed before SQL run)
    lock = get_truck_lock(req.truck_id)
    with lock:
        # Check idempotency
        existing = supabase.table("load_slips").select("*, load_slip_items(*)").eq("idempotency_key", req.idempotency_key).is_("deleted_at", "null").limit(1).execute()
        if existing.data:
            return {"status": "success", "message": "Duplicate prevented (idempotent).", "data": existing.data[0]}

        # Check truck capacity
        trk_res = supabase.table("trucks").select("*").eq("id", req.truck_id).eq("is_active", True).is_("deleted_at", "null").limit(1).execute()
        if not trk_res.data:
            raise HTTPException(status_code=404, detail="Selected truck not found or inactive.")
        truck = trk_res.data[0]
        capacity = int(truck.get("sack_capacity") or truck.get("max_sack_capacity") or 120)

        active_slips = supabase.table("load_slips").select("id, status, total_loaded_sacks").eq("truck_id", req.truck_id).is_("deleted_at", "null").execute()
        used_sacks = sum(int(s.get("total_loaded_sacks") or 0) for s in (active_slips.data or []) if is_unremitted_status(s.get("status")))
        in_transit = any((s.get("status") or "").upper() in ("IN_TRANSIT", "IN-TRANSIT") for s in (active_slips.data or []))
        remaining = max(0, capacity - used_sacks)

        if requested_sacks > remaining:
            raise HTTPException(
                status_code=409,
                detail=f"Capacity exceeded: Another staff member just dispatched to this truck. Only {remaining} sacks remaining."
            )

        now_utc = datetime.now(timezone.utc).isoformat()
        slip_insert = {
            "slip_number": slip_number,
            "idempotency_key": req.idempotency_key,
            "date": shift_date,
            "departure_time": dep_time,
            "truck_id": req.truck_id,
            "truck_plate": truck.get("plate_number"),
            "in_charge_id": in_charge_id,
            "in_charge_name": in_charge_name,
            "driver_id": driver_id_clean,
            "driver_name": driver_name or "Assigned Driver",
            "status": "DISPATCHED",
            "total_loaded_sacks": requested_sacks,
            "total_delivered_sacks": 0,
            "total_returned_sacks": 0,
            "total_cash_revenue": float(total_cash),
            "total_charge_revenue": float(total_charge),
            "created_by": in_charge_name,
            "updated_by": in_charge_name,
            "created_at": now_utc,
            "updated_at": now_utc
        }
        ins_res = supabase.table("load_slips").insert(slip_insert).execute()
        if not ins_res.data:
            raise HTTPException(status_code=500, detail="Failed to insert Load Slip.")
        new_slip = ins_res.data[0]
        slip_id = new_slip["id"]
        slip_suffix = str(slip_id).replace("-", "")[-4:].upper()

        created_items = []
        for idx, it in enumerate(items_payload, start=1):
            item_row = {
                "load_slip_id": slip_id,
                "idempotency_key": it["idempotency_key"],
                "sequence_order": idx,
                "hub_id": it["hub_id"],
                "hub_name": it["hub_name"],
                "customer_id": it["customer_id"],
                "customer_name": it["customer_name"],
                "product_id": it["product_id"],
                "product_name": it["product_name"],
                "sack_size": it["sack_size"],
                "planned_qty": it["planned_qty"],
                "delivered_qty": 0,
                "returned_qty": 0,
                "unit_price": it["unit_price"],
                "total_amount": it["total_amount"],
                "payment_mode": it["payment_mode"],
                "status": "Pending"
            }
            it_res = supabase.table("load_slip_items").insert(item_row).execute()
            if it_res.data:
                created_it = it_res.data[0]
                created_items.append(created_it)
                prefix = "CI" if it["payment_mode"] == "Charge" else "OR"
                inv_num = f"{prefix}-{shift_date.replace('-', '')}-{slip_suffix}-{idx:02d}"
                supabase.table("invoices").upsert({
                    "invoice_number": inv_num,
                    "idempotency_key": f"INV-{created_it['id']}",
                    "load_slip_id": slip_id,
                    "load_slip_item_id": created_it["id"],
                    "slip_number": slip_number,
                    "truck_id": req.truck_id,
                    "truck_plate": truck.get("plate_number"),
                    "driver_id": driver_id_clean,
                    "driver_name": driver_name or "Assigned Driver",
                    "in_charge_id": in_charge_id,
                    "in_charge_name": in_charge_name,
                    "customer_name": it["customer_name"],
                    "hub_name": it["hub_name"],
                    "product_name": it["product_name"],
                    "sack_size": it["sack_size"],
                    "planned_qty": it["planned_qty"],
                    "delivered_qty": 0,
                    "returned_qty": 0,
                    "unit_price": it["unit_price"],
                    "total_amount": it["total_amount"],
                    "payment_type": it["payment_mode"],
                    "status": "Pending Delivery",
                    "issue_date": shift_date,
                    "issued_at": now_utc
                }, on_conflict="idempotency_key").execute()

        new_slip["load_slip_items"] = created_items
        log_audit("load_slips", slip_id, "DISPATCH", in_charge_name, new_data={"slip_number": slip_number, "sacks": requested_sacks})
        return {
            "status": "success",
            "message": f"Load Slip #{slip_number} saved & dispatched!",
            "data": new_slip,
            "remaining_capacity": max(0, remaining - requested_sacks),
            "in_transit_warning": in_transit
        }

@app.post("/api/v1/load-slips/{slip_id}/start-route")
def start_load_slip_route(slip_id: str):
    """Transitions a DISPATCHED load slip into IN_TRANSIT when the driver starts navigation/deliveries."""
    try:
        res = supabase.table("load_slips").select("id, status").eq("id", slip_id).limit(1).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="Load slip not found.")
        curr = (res.data[0].get("status") or "").upper()
        if curr in ("DISPATCHED", "PENDING", "ACTIVE"):
            supabase.table("load_slips").update({
                "status": "IN_TRANSIT",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat()
            }).eq("id", slip_id).execute()
        return {"status": "success", "slip_status": "IN_TRANSIT"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/load-slips/{slip_id}/finish-delivery")
def finish_delivery_route(slip_id: str, req: Optional[FinishDeliveryRequest] = None):
    """Driver clicks 'Finish Delivery' on mobile -> sets status = DELIVERY_FINISHED so it appears in Shift Closing & Remit."""
    try:
        slip_res = supabase.table("load_slips").select("*").eq("id", slip_id).limit(1).execute()
        if not slip_res.data:
            raise HTTPException(status_code=404, detail="Load slip not found.")

        slip = slip_res.data[0]
        curr_status = (slip.get("status") or "").upper()
        if curr_status in REMITTED_OR_DONE_STATUSES:
            return {"status": "success", "message": "Load slip is already remitted.", "data": slip}

        now_utc = datetime.now(timezone.utc).isoformat()
        supabase.table("load_slips").update({
            "status": "DELIVERY_FINISHED",
            "delivery_finished_at": now_utc,
            "updated_at": now_utc
        }).eq("id", slip_id).execute()

        updated = _recalculate_slip_totals_and_invoices(slip_id)
        log_audit("load_slips", slip_id, "FINISH_DELIVERY", (req.driver_id if req else None) or slip.get("driver_name") or "Driver")

        return {
            "status": "success",
            "message": "Delivery finished! Dispatch notified for Shift Closing & Remit.",
            "data": updated
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/shift-closing/pending")
def get_pending_shift_closing():
    """Strictly returns ONLY load slips whose drivers finished delivery ('DELIVERY_FINISHED' / 'AWAITING_REMIT')."""
    try:
        res = (
            supabase.table("load_slips")
            .select("*, load_slip_items(*, delivery_hubs(*)), trucks(id, plate_number, model, sack_capacity, max_sack_capacity)")
            .is_("deleted_at", "null")
            .order("delivery_finished_at", desc=True)
            .execute()
        )
        rows = res.data or []
        finished_pending = [
            r for r in rows
            if (r.get("status") or "").strip().upper() in FINISHED_AWAITING_REMIT_STATUSES
        ]
        in_transit_count = sum(
            1 for r in rows
            if (r.get("status") or "").strip().upper() in ("DISPATCHED", "ACTIVE", "PENDING", "IN_TRANSIT", "IN-TRANSIT")
        )
        return {
            "status": "success",
            "data": finished_pending,
            "in_transit_count": in_transit_count,
            "manila_date": manila_date_str()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/load-slips/{slip_id}/confirm-remit")
def confirm_shift_remit(
    slip_id: str,
    req: ConfirmRemitRequest,
    auth_user: Dict[str, Any] = Depends(require_active_session)
):
    """Confirms remittance for a finished delivery -> sets status = REMITTED so it clears out of Shift Closing."""
    if auth_user.get("role") not in ("Owner", "Staff"):
        raise HTTPException(status_code=403, detail="Only Owner or Staff can confirm shift remittance.")

    try:
        slip_res = supabase.table("load_slips").select("*").eq("id", slip_id).is_("deleted_at", "null").limit(1).execute()
        if not slip_res.data:
            raise HTTPException(status_code=404, detail="Load slip not found.")

        slip = slip_res.data[0]
        curr_status = (slip.get("status") or "").strip().upper()

        if curr_status in REMITTED_OR_DONE_STATUSES:
            raise HTTPException(status_code=409, detail="This delivery shift has already been remitted and closed.")
        if curr_status not in FINISHED_AWAITING_REMIT_STATUSES:
            raise HTTPException(status_code=400, detail="Driver has not clicked 'Finish Delivery' yet. Cannot close shift while still in transit.")

        recalculated = _recalculate_slip_totals_and_invoices(slip_id) or slip
        now_utc = datetime.now(timezone.utc).isoformat()
        closed_by = auth_user["full_name"]
        cash_remitted = round_currency(recalculated.get("total_cash_revenue") or 0)
        charge_issued = round_currency(recalculated.get("total_charge_revenue") or 0)
        sacks_returned = int(recalculated.get("total_returned_sacks") or max(0, (recalculated.get("total_loaded_sacks") or 0) - (recalculated.get("total_delivered_sacks") or 0)))

        # Idempotent insert into shift_reconciliations
        existing_rec = supabase.table("shift_reconciliations").select("id").eq("load_slip_id", slip_id).limit(1).execute()
        if not existing_rec.data:
            supabase.table("shift_reconciliations").insert({
                "truck_id": slip["truck_id"],
                "load_slip_id": slip_id,
                "reconciliation_date": manila_date_str(),
                "cash_remitted": cash_remitted,
                "charge_issued": charge_issued,
                "sacks_returned": sacks_returned,
                "closed_at": req.closed_at or manila_time_str(),
                "closed_by": closed_by,
                "notes": req.notes
            }).execute()

        # Mark load slip REMITTED so it leaves active Shift Closing and frees truck capacity
        supabase.table("load_slips").update({
            "status": "REMITTED",
            "remitted_at": now_utc,
            "remitted_by": closed_by,
            "updated_by": closed_by,
            "updated_at": now_utc
        }).eq("id", slip_id).execute()

        log_audit("load_slips", slip_id, "CONFIRM_REMIT", closed_by, new_data={
            "cash_remitted": cash_remitted,
            "charge_issued": charge_issued,
            "sacks_returned": sacks_returned
        })

        return {
            "status": "success",
            "message": f"Shift for {slip['truck_id']} (#{slip.get('slip_number')}) confirmed & remitted cleanly!",
            "slip_id": slip_id
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- INVOICES & OFFICIAL RECEIPTS ---

@app.get("/api/v1/invoices")
def list_invoices():
    """Returns enriched itemized invoices & receipts with real truck, driver, and in-charge staff metadata."""
    try:
        try:
            res = supabase.table("receipts_view").select("*").order("created_at", desc=True).execute()
            return {"status": "success", "data": res.data or []}
        except Exception:
            # Fallback before migration view exists
            res = supabase.table("invoices").select("*, load_slips(slip_number, truck_id, truck_plate, driver_name, in_charge_name, status)").is_("deleted_at", "null").order("created_at", desc=True).execute()
            return {"status": "success", "data": res.data or []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/invoices/{invoice_id}/settle")
def settle_invoice(
    invoice_id: str,
    auth_user: Dict[str, Any] = Depends(require_active_session)
):
    if auth_user.get("role") not in ("Owner", "Staff"):
        raise HTTPException(status_code=403, detail="Only Owner or Staff can settle charge invoices.")
    try:
        inv_res = supabase.table("invoices").select("*").eq("id", invoice_id).is_("deleted_at", "null").limit(1).execute()
        if not inv_res.data:
            raise HTTPException(status_code=404, detail="Invoice not found.")
        inv = inv_res.data[0]
        if inv.get("status") == "Settled":
            return {"status": "success", "message": "Invoice is already settled.", "data": inv}

        now_utc = datetime.now(timezone.utc).isoformat()
        staff_name = auth_user["full_name"]
        upd = supabase.table("invoices").update({
            "status": "Settled",
            "settled_at": now_utc,
            "settled_by": staff_name,
            "updated_at": now_utc
        }).eq("id", invoice_id).execute()

        log_audit("invoices", invoice_id, "SETTLE_INVOICE", staff_name)
        return {"status": "success", "message": f"Invoice {inv.get('invoice_number')} marked as Settled.", "data": upd.data[0] if upd.data else inv}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- LIVE FLEET TELEMETRY AGGREGATION ---

@app.get("/api/v1/telemetry/live")
def get_live_fleet_telemetry():
    """Aggregates live truck coordinates from gps_logs, active load slips, and active SOS alerts."""
    try:
        slips_res = (
            supabase.table("load_slips")
            .select("id, slip_number, truck_id, truck_plate, driver_id, driver_name, status, total_loaded_sacks, total_delivered_sacks, updated_at, trucks(id, plate_number, model), load_slip_items(id, status, delivered_qty, hub_name, customer_name, delivery_hubs(name, latitude, longitude))")
            .is_("deleted_at", "null")
            .order("created_at", desc=True)
            .execute()
        )
        all_slips = slips_res.data or []
        active_slips = [s for s in all_slips if is_unremitted_status(s.get("status"))]

        gps_res = (
            supabase.table("gps_logs")
            .select("truck_id, latitude, longitude, speed_kmh, heading, logged_at")
            .order("logged_at", desc=True)
            .limit(100)
            .execute()
        )
        latest_gps_by_truck: Dict[str, Dict[str, Any]] = {}
        for g in (gps_res.data or []):
            tid = g.get("truck_id")
            if tid and tid not in latest_gps_by_truck:
                latest_gps_by_truck[tid] = g

        # Group active slips by truck
        by_truck: Dict[str, List[Dict[str, Any]]] = {}
        for s in active_slips:
            tid = s.get("truck_id")
            if tid:
                by_truck.setdefault(tid, []).append(s)

        telemetry_list = []
        for tid, t_slips in by_truck.items():
            primary = t_slips[0]
            gps = latest_gps_by_truck.get(tid)
            loaded = sum(int(s.get("total_loaded_sacks") or 0) for s in t_slips)
            delivered = sum(int(s.get("total_delivered_sacks") or 0) for s in t_slips)
            items = [it for s in t_slips for it in (s.get("load_slip_items") or [])]
            completed_stops = sum(1 for it in items if (it.get("status") or "").lower() in ("completed", "partial") or (it.get("delivered_qty") or 0) > 0)
            next_pending = next((it for it in items if (it.get("status") or "").lower() not in ("completed", "partial")), None)

            sos_match = next(
                (a for a in _active_sos_alerts.values() if a.get("truck_id") == tid and a.get("status") == "Active"),
                None
            )

            slip_status_upper = (primary.get("status") or "").upper()
            truck_status = "Alert" if sos_match else ("Delivering" if slip_status_upper in ("IN_TRANSIT", "IN-TRANSIT") else "In Transit")

            plate = (primary.get("trucks") or {}).get("plate_number") or primary.get("truck_plate") or tid
            drv_name = primary.get("driver_name") or "Assigned Driver"
            lat = float(gps["latitude"]) if gps else 8.4770172
            lng = float(gps["longitude"]) if gps else 124.6798424
            if next_pending and not gps and next_pending.get("delivery_hubs"):
                lat = float(next_pending["delivery_hubs"].get("latitude") or lat)
                lng = float(next_pending["delivery_hubs"].get("longitude") or lng)

            telemetry_list.append({
                "truckId": tid,
                "truckPlate": plate,
                "driverId": primary.get("driver_id") or "",
                "driverName": drv_name,
                "status": truck_status,
                "currentLocationName": (next_pending or {}).get("hub_name") or "JICA Gusa Corridor",
                "currentStopStoreName": (next_pending or {}).get("customer_name") or ("Finished Route" if slip_status_upper in FINISHED_AWAITING_REMIT_STATUSES else "En Route"),
                "latitude": lat,
                "longitude": lng,
                "speedKmh": float(gps.get("speed_kmh") or 0) if gps else 0,
                "heading": float(gps.get("heading") or 0) if gps else 0,
                "loadedSacks": loaded,
                "deliveredSacks": delivered,
                "remainingSacks": max(0, loaded - delivered),
                "totalStops": len(items),
                "completedStops": completed_stops,
                "lastPingTime": (gps.get("logged_at") if gps else primary.get("updated_at")) or datetime.now(timezone.utc).isoformat(),
                "sosAlert": {
                    "active": True,
                    "reason": sos_match.get("reason"),
                    "timestamp": sos_match.get("created_at"),
                    "latitude": sos_match.get("latitude"),
                    "longitude": sos_match.get("longitude"),
                    "dispatchedRescue": sos_match.get("status") == "Acknowledged"
                } if sos_match else None
            })

        return {"status": "success", "data": telemetry_list}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- TRASH & RECOVERY (UNIVERSAL 30-DAY SOFT DELETE WITH SAFE FK INTEGRITY) ---

@app.get("/api/v1/trash/items")
def get_trash_items():
    items = []
    now = datetime.now(timezone.utc)
    for table_name in VALID_TRASH_TABLES:
        try:
            res = supabase.table(table_name).select("*").not_.is_("deleted_at", "null").order("deleted_at", desc=True).execute()
            for row in (res.data or []):
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
                    row.get("plate_number") or
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
                    "raw_data": {k: v for k, v in row.items() if k not in ("password", "password_hash", "mobile_pin", "active_session_token")}
                })
        except Exception:
            continue
    return {"status": "success", "items": items}

@app.post("/api/v1/trash/soft-delete")
def soft_delete_item(req: SoftDeleteRequest):
    if req.entity_type not in VALID_TRASH_TABLES:
        raise HTTPException(status_code=400, detail=f"Invalid entity type: {req.entity_type}")
    try:
        # Prevent soft-deleting a truck currently assigned to an active/unremitted load slip
        if req.entity_type == "trucks":
            active = supabase.table("load_slips").select("id, status, slip_number").eq("truck_id", req.entity_id).is_("deleted_at", "null").execute()
            busy = [s for s in (active.data or []) if is_unremitted_status(s.get("status"))]
            if busy:
                raise HTTPException(
                    status_code=409,
                    detail=f"Cannot archive truck {req.entity_id}: it has active/unremitted Load Slip #{busy[0].get('slip_number')}. Confirm Shift Closing first."
                )

        now_iso = datetime.now(timezone.utc).isoformat()
        update_fields: Dict[str, Any] = {
            "deleted_at": now_iso,
            "deleted_by": req.deleted_by
        }
        if req.entity_type in ("trucks", "products", "delivery_hubs", "customers", "user_accounts"):
            update_fields["is_active"] = False

        # Clean up Supabase Storage image asset on delete so 1 GB storage quota is protected
        asset_col_map = {
            "products": "image_url",
            "trucks": "image_url",
            "delivery_hubs": "photo_url",
            "user_accounts": "photo_url"
        }
        asset_col = asset_col_map.get(req.entity_type)
        if asset_col:
            prev = supabase.table(req.entity_type).select(asset_col).eq("id", req.entity_id).limit(1).execute()
            if prev.data and prev.data[0].get(asset_col):
                delete_jica_asset_url(prev.data[0].get(asset_col))
                update_fields[asset_col] = None

        supabase.table(req.entity_type).update(update_fields).eq("id", req.entity_id).execute()
        log_audit(req.entity_type, req.entity_id, "SOFT_DELETE", req.deleted_by)
        return {"status": "success", "message": f"{req.entity_type} item safely archived to Trash."}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/trash/restore")
def restore_trash_item(req: RestoreRequest):
    if req.entity_type not in VALID_TRASH_TABLES:
        raise HTTPException(status_code=400, detail=f"Invalid entity type: {req.entity_type}")
    try:
        update_fields: Dict[str, Any] = {
            "deleted_at": None,
            "deleted_by": None
        }
        if req.entity_type in ("trucks", "products", "delivery_hubs", "customers", "user_accounts"):
            update_fields["is_active"] = True

        supabase.table(req.entity_type).update(update_fields).eq("id", req.entity_id).execute()
        log_audit(req.entity_type, req.entity_id, "RESTORE", req.restored_by)
        return {"status": "success", "message": f"{req.entity_type} item restored successfully."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/trash/delete-forever")
def delete_forever(req: PermanentDeleteRequest):
    if req.entity_type not in VALID_TRASH_TABLES:
        raise HTTPException(status_code=400, detail=f"Invalid entity type: {req.entity_type}")
    try:
        admin_res = supabase.table("user_accounts").select("role, full_name").eq("id", req.admin_user_id).execute()
        if not admin_res.data or admin_res.data[0].get("role") != "Owner":
            raise HTTPException(status_code=403, detail="Permanent deletion is strictly restricted to the Owner role.")
        admin_name = admin_res.data[0].get("full_name", "Owner")

        asset_col_map = {
            "products": "image_url",
            "trucks": "image_url",
            "delivery_hubs": "photo_url",
            "user_accounts": "photo_url"
        }
        asset_col = asset_col_map.get(req.entity_type)
        stale_asset_url = None
        if asset_col:
            prev = supabase.table(req.entity_type).select(asset_col).eq("id", req.entity_id).limit(1).execute()
            if prev.data:
                stale_asset_url = prev.data[0].get(asset_col)

        if req.entity_type == "load_slips":
            try:
                supabase.table("load_slip_items").delete().eq("load_slip_id", req.entity_id).execute()
            except Exception:
                pass

        try:
            supabase.table(req.entity_type).delete().eq("id", req.entity_id).execute()
        except Exception as fk_err:
            err_msg = str(fk_err).lower()
            if "foreign key" in err_msg or "23503" in err_msg or "restrict" in err_msg or "violates" in err_msg:
                raise HTTPException(
                    status_code=409,
                    detail="Cannot permanently purge this item because historical Load Slips or Invoices reference it. Keep it safely archived (deactivated) to preserve audit history."
                )
            raise

        if stale_asset_url:
            delete_jica_asset_url(stale_asset_url)

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
        if ping.idempotency_key:
            exists = supabase.table("gps_logs").select("id").eq("idempotency_key", ping.idempotency_key).limit(1).execute()
            if exists.data:
                return {"status": "success", "duplicate": True, "id": exists.data[0]["id"]}

        payload = {
            "load_slip_id": ping.trip_id if is_uuid(ping.trip_id) else None,
            "driver_id": ping.driver_id if is_uuid(ping.driver_id) else None,
            "truck_id": ping.truck_id,
            "latitude": ping.latitude,
            "longitude": ping.longitude,
            "speed_kmh": ping.speed_kmh,
            "heading": ping.heading,
            "battery_level": ping.battery_level,
            "is_charging": ping.is_charging,
            "is_sos": ping.is_sos,
            "idempotency_key": ping.idempotency_key
        }
        res = supabase.table("gps_logs").insert(payload).execute()
        return {"status": "success", "id": res.data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/telemetry/sos/active")
@app.get("/telemetry/sos/active")
def get_active_sos():
    alerts = list(_active_sos_alerts.values())
    alerts.sort(key=lambda a: a.get("created_at", ""), reverse=True)
    return {"status": "success", "alerts": alerts}

@app.post("/api/v1/telemetry/sos")
@app.post("/telemetry/sos")
def save_sos_alert(sos: EmergencySOSAlert):
    try:
        alert_id = sos.idempotency_key
        if alert_id in _active_sos_alerts:
            return {"status": "success", "duplicate": True, "message": "SOS already logged.", "alert": _active_sos_alerts[alert_id]}

        now_iso = datetime.now(timezone.utc).isoformat()
        alert_data = {
            "id": alert_id,
            "idempotency_key": sos.idempotency_key,
            "load_slip_id": sos.load_slip_id,
            "driver_id": sos.driver_id,
            "driver_name": sos.driver_name or sos.driver_id,
            "truck_id": sos.truck_id,
            "latitude": sos.latitude,
            "longitude": sos.longitude,
            "reason": sos.reason,
            "status": "Active",
            "created_at": now_iso,
            "dispatcher_reply": None,
            "acknowledged_by": None
        }
        _active_sos_alerts[alert_id] = alert_data

        try:
            exists = supabase.table("gps_logs").select("id").eq("idempotency_key", sos.idempotency_key).limit(1).execute()
            if not exists.data:
                supabase.table("gps_logs").insert({
                    "load_slip_id": sos.load_slip_id if is_uuid(sos.load_slip_id) else None,
                    "driver_id": sos.driver_id if is_uuid(sos.driver_id) else None,
                    "truck_id": sos.truck_id,
                    "latitude": sos.latitude,
                    "longitude": sos.longitude,
                    "is_sos": True,
                    "idempotency_key": sos.idempotency_key
                }).execute()
        except Exception as db_err:
            print(f"[SOS GPS_LOGS NOTICE] {db_err}")

        log_audit("emergency_sos", alert_id, "SOS_TRIGGERED", sos.driver_name or sos.driver_id, new_data=alert_data)
        return {"status": "success", "message": f"SOS Alert: {sos.reason}. Dispatch alerted immediately.", "alert": alert_data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/telemetry/sos/acknowledge")
@app.post("/telemetry/sos/acknowledge")
def acknowledge_sos_alert(req: SosAcknowledgeRequest):
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
        return {"status": "success", "message": "SOS alert acknowledged.", "alert": alert}

    placeholder_alert = {
        "id": req.alert_id,
        "status": "Acknowledged",
        "dispatcher_reply": reply_msg,
        "acknowledged_by": ack_by,
        "acknowledged_at": now_iso
    }
    _active_sos_alerts[req.alert_id] = placeholder_alert
    return {"status": "success", "message": "SOS alert acknowledged.", "alert": placeholder_alert}

# --- IDEMPOTENT OFFLINE BATCH SYNC ---

@app.post("/api/v1/sync")
def sync_offline_batch(payload: BatchSyncPayload):
    """
    Idempotent batch sync for mobile driver app when online or reconnecting after a cellular dead zone.
    Updates load_slip_items, auto-updates load_slips.status -> IN_TRANSIT, recalculates totals,
    and updates itemized invoices automatically.
    """
    synced_tx_keys = []
    affected_slips = set()

    for tx in payload.transactions:
        try:
            # 1. Check idempotency via delivery_idempotency_key
            existing = supabase.table("load_slip_items").select("id, load_slip_id").eq("delivery_idempotency_key", tx.idempotency_key).limit(1).execute()
            if existing.data:
                synced_tx_keys.append(tx.idempotency_key)
                if existing.data[0].get("load_slip_id"):
                    affected_slips.add(existing.data[0]["load_slip_id"])
                continue

            now_utc = datetime.now(timezone.utc).isoformat()
            unit_price = round_currency(tx.unit_price)
            total_amt = round_currency(Decimal(str(unit_price)) * Decimal(tx.delivered_qty))
            pay_mode = "Charge" if "charge" in (tx.payment_method or "").lower() else "Cash"
            item_status = "Partial" if tx.delivered_qty < tx.planned_qty else "Completed"

            # 2. Update scheduled stop OR insert unscheduled extra delivery
            if is_uuid(tx.stop_id):
                upd_res = supabase.table("load_slip_items").update({
                    "delivered_qty": tx.delivered_qty,
                    "returned_qty": tx.returned_qty,
                    "unit_price": unit_price,
                    "total_amount": total_amt,
                    "payment_mode": pay_mode,
                    "partial_reason": tx.partial_reason,
                    "status": item_status,
                    "delivered_at": tx.timestamp or now_utc,
                    "delivery_idempotency_key": tx.idempotency_key
                }).eq("id", tx.stop_id).execute()

                slip_id = tx.load_slip_id or (upd_res.data[0]["load_slip_id"] if upd_res.data else None)
                if slip_id:
                    affected_slips.add(slip_id)
            elif tx.is_adhoc or (tx.load_slip_id and is_uuid(tx.load_slip_id)):
                target_slip_id = tx.load_slip_id or payload.load_slip_id
                if target_slip_id and is_uuid(target_slip_id):
                    ins_item = supabase.table("load_slip_items").insert({
                        "load_slip_id": target_slip_id,
                        "idempotency_key": tx.idempotency_key,
                        "delivery_idempotency_key": tx.idempotency_key,
                        "sequence_order": 99,
                        "hub_id": tx.hub_id if is_uuid(tx.hub_id) else None,
                        "hub_name": tx.hub_name or "Walk-In / Extra Hub",
                        "customer_name": tx.customer_name,
                        "product_id": tx.product_id if is_uuid(tx.product_id) else None,
                        "product_name": tx.product_name or "Ice Product",
                        "sack_size": tx.sack_size or "Standard Sack",
                        "planned_qty": tx.planned_qty,
                        "delivered_qty": tx.delivered_qty,
                        "returned_qty": tx.returned_qty,
                        "unit_price": unit_price,
                        "total_amount": total_amt,
                        "payment_mode": pay_mode,
                        "is_adhoc": True,
                        "partial_reason": tx.partial_reason,
                        "status": item_status,
                        "delivered_at": tx.timestamp or now_utc
                    }).execute()
                    if ins_item.data:
                        affected_slips.add(target_slip_id)

            synced_tx_keys.append(tx.idempotency_key)
        except Exception as tx_err:
            print(f"[SYNC TX ERROR] {tx.idempotency_key}: {tx_err}")

    # Recalculate affected load slips & transition DISPATCHED -> IN_TRANSIT
    for sid in affected_slips:
        try:
            s_res = supabase.table("load_slips").select("status").eq("id", sid).limit(1).execute()
            if s_res.data and (s_res.data[0].get("status") or "").upper() in ("DISPATCHED", "ACTIVE", "PENDING"):
                supabase.table("load_slips").update({
                    "status": "IN_TRANSIT",
                    "started_at": datetime.now(timezone.utc).isoformat()
                }).eq("id", sid).execute()
            _recalculate_slip_totals_and_invoices(sid)
        except Exception as rec_err:
            print(f"[SYNC RECALC NOTICE] {sid}: {rec_err}")

    # Save GPS pings with idempotency where present
    if payload.gps_pings:
        gps_rows = []
        for p in payload.gps_pings:
            gps_rows.append({
                "load_slip_id": p.trip_id if is_uuid(p.trip_id) else (payload.load_slip_id if is_uuid(payload.load_slip_id) else None),
                "driver_id": p.driver_id if is_uuid(p.driver_id) else (payload.driver_id if is_uuid(payload.driver_id) else None),
                "truck_id": p.truck_id or payload.truck_id,
                "latitude": p.latitude,
                "longitude": p.longitude,
                "speed_kmh": p.speed_kmh,
                "heading": p.heading,
                "is_sos": p.is_sos,
                "idempotency_key": p.idempotency_key
            })
        try:
            supabase.table("gps_logs").insert(gps_rows).execute()
        except Exception as e:
            print(f"[SYNC GPS NOTICE] {e}")

    return {
        "status": "success",
        "synced_transactions": synced_tx_keys,
        "gps_pings_count": len(payload.gps_pings)
    }

# --- GOOGLE ROUTES API V2 OPTIMIZE & CACHE PROXY ---

@app.post("/api/v1/routes/optimize")
async def optimize_route(req: RouteOptimizeRequest):
    cache_key = get_route_cache_key(
        f"{req.origin.latitude},{req.origin.longitude}",
        f"{req.destination.latitude},{req.destination.longitude}"
    )
    now = time.time()

    if cache_key in _route_cache:
        cached_ts, cached_payload = _route_cache[cache_key]
        if now - cached_ts < ROUTE_CACHE_TTL_SECONDS:
            return cached_payload

    if GOOGLE_ROUTES_KEY and not GOOGLE_ROUTES_KEY.startswith("your_"):
        try:
            routes_url = "https://routes.googleapis.com/directions/v2:computeRoutes"
            headers = {
                "Content-Type": "application/json",
                "X-Goog-Api-Key": GOOGLE_ROUTES_KEY,
                "X-Goog-FieldMask": "routes.duration,routes.distanceMeters,routes.polyline.encodedPolyline,routes.legs.steps"
            }
            body = {
                "origin": {"location": {"latLng": {"latitude": req.origin.latitude, "longitude": req.origin.longitude}}},
                "destination": {"location": {"latLng": {"latitude": req.destination.latitude, "longitude": req.destination.longitude}}},
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
            print(f"Routes API call error, falling back to CDO route: {err}")

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
