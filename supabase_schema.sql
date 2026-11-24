-- ==============================================================================
-- JICA ICE LOGISTICS & DELIVERY - CAGAYAN DE ORO CITY
-- FULL SUPABASE POSTGRESQL + POSTGIS PRODUCTION SCHEMA (PHASE 2 CERTIFIED)
-- ==============================================================================
-- Regional Baseline: Cagayan de Oro City, Philippines (Asia/Manila UTC+8)
-- Zero-Overage Cloud Design: Supabase Free Tier + Railway FastAPI + Vercel Web
-- ==============================================================================

-- 1. EXTENSIONS
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "postgis";

-- 2. ENUMS & DOMAINS
DO $$ BEGIN
    CREATE TYPE payment_mode AS ENUM ('Cash', 'Charge');
EXCEPTION
    WHEN duplicate_object THEN null;
END $$;

DO $$ BEGIN
    CREATE TYPE slip_status AS ENUM ('Pending', 'In-Transit', 'Completed', 'Cancelled');
EXCEPTION
    WHEN duplicate_object THEN null;
END $$;

DO $$ BEGIN
    CREATE TYPE user_role AS ENUM ('Owner', 'Staff', 'Driver', 'Security', 'Admin');
EXCEPTION
    WHEN duplicate_object THEN
        BEGIN
            ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'Owner';
            ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'Security';
        EXCEPTION WHEN OTHERS THEN null;
        END;
END $$;

DO $$ BEGIN
    CREATE TYPE invoice_status AS ENUM ('Cash Paid', 'Unpaid Charge', 'Settled');
EXCEPTION
    WHEN duplicate_object THEN null;
END $$;

-- ------------------------------------------------------------------------------
-- 3. PERMISSIONS GRANT (POSTGREST COMPATIBILITY)
-- ------------------------------------------------------------------------------
GRANT USAGE ON SCHEMA public TO postgres, anon, authenticated, service_role;
GRANT ALL ON ALL TABLES IN SCHEMA public TO postgres, anon, authenticated, service_role;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO postgres, anon, authenticated, service_role;
GRANT ALL ON ALL ROUTINES IN SCHEMA public TO postgres, anon, authenticated, service_role;

ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO postgres, anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO postgres, anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON ROUTINES TO postgres, anon, authenticated, service_role;

-- ------------------------------------------------------------------------------
-- 4. TABLES DEFINITIONS (WITH UNIVERSAL 30-DAY SOFT DELETE & TIMEZONE DEFAULTS)
-- ------------------------------------------------------------------------------

-- Table: Delivery Location Hubs (Clustering points to minimize Google Maps calls)
CREATE TABLE IF NOT EXISTS public.delivery_hubs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    address TEXT NOT NULL,
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    geom GEOMETRY(Point, 4326),
    is_active BOOLEAN NOT NULL DEFAULT true,
    deleted_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
    deleted_by TEXT DEFAULT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

-- Table: Store Customers (belonging to a Delivery Hub)
CREATE TABLE IF NOT EXISTS public.customers (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    hub_id UUID NOT NULL REFERENCES public.delivery_hubs(id) ON DELETE RESTRICT,
    contact_person TEXT,
    contact_phone TEXT,
    default_payment payment_mode NOT NULL DEFAULT 'Cash',
    is_active BOOLEAN NOT NULL DEFAULT true,
    deleted_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
    deleted_by TEXT DEFAULT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

-- Table: Ice Products & Standard Pricing
CREATE TABLE IF NOT EXISTS public.products (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    sack_weight_kg NUMERIC(6, 2) NOT NULL DEFAULT 50.00,
    standard_price NUMERIC(10, 2) NOT NULL,
    image_url TEXT,
    is_active BOOLEAN NOT NULL DEFAULT true,
    deleted_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
    deleted_by TEXT DEFAULT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

-- Table: Delivery Trucks / Fleet Vehicles
CREATE TABLE IF NOT EXISTS public.trucks (
    id TEXT PRIMARY KEY, -- e.g. 'TRK-01'
    plate_number TEXT UNIQUE NOT NULL,
    model TEXT NOT NULL,
    max_sack_capacity INT NOT NULL DEFAULT 120,
    image_url TEXT,
    is_active BOOLEAN NOT NULL DEFAULT true,
    deleted_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
    deleted_by TEXT DEFAULT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

-- Table: User Accounts & Staff/Driver Profiles
CREATE TABLE IF NOT EXISTS public.user_accounts (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    auth_user_id UUID REFERENCES auth.users(id) ON DELETE SET NULL,
    full_name TEXT NOT NULL,
    role user_role NOT NULL DEFAULT 'Staff',
    username TEXT UNIQUE,
    password TEXT,
    password_hash TEXT DEFAULT NULL,
    mobile_pin TEXT DEFAULT '1234',
    phone_number TEXT,
    email TEXT,
    photo_url TEXT,
    assigned_truck_id TEXT REFERENCES public.trucks(id),
    is_active BOOLEAN NOT NULL DEFAULT true,
    deleted_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
    deleted_by TEXT DEFAULT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

-- Table: Load Slips (Dispatch Manifests)
CREATE TABLE IF NOT EXISTS public.load_slips (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    slip_number TEXT UNIQUE NOT NULL, -- e.g. 'LS-20261124-TRK01'
    idempotency_key TEXT UNIQUE NOT NULL,
    date DATE NOT NULL DEFAULT (now() AT TIME ZONE 'Asia/Manila')::date,
    departure_time TIME NOT NULL,
    truck_id TEXT NOT NULL REFERENCES public.trucks(id) ON DELETE RESTRICT,
    in_charge_id UUID REFERENCES public.user_accounts(id),
    driver_id UUID REFERENCES public.user_accounts(id),
    status slip_status NOT NULL DEFAULT 'Pending',
    total_loaded_sacks INT NOT NULL DEFAULT 0,
    total_delivered_sacks INT NOT NULL DEFAULT 0,
    total_returned_sacks INT NOT NULL DEFAULT 0,
    total_cash_revenue NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    total_charge_revenue NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    created_by TEXT NOT NULL DEFAULT 'Admin Dispatcher',
    updated_by TEXT NOT NULL DEFAULT 'Admin Dispatcher',
    deleted_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
    deleted_by TEXT DEFAULT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

-- Table: Load Slip Order Rows (Individual Store Drops)
CREATE TABLE IF NOT EXISTS public.load_slip_items (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    load_slip_id UUID NOT NULL REFERENCES public.load_slips(id) ON DELETE CASCADE,
    idempotency_key TEXT UNIQUE NOT NULL,
    sequence_order INT NOT NULL DEFAULT 1,
    hub_id UUID NOT NULL REFERENCES public.delivery_hubs(id),
    customer_id UUID REFERENCES public.customers(id),
    customer_name TEXT NOT NULL,
    product_id UUID REFERENCES public.products(id),
    product_name TEXT NOT NULL,
    planned_qty INT NOT NULL CHECK (planned_qty >= 0),
    delivered_qty INT NOT NULL DEFAULT 0 CHECK (delivered_qty >= 0),
    returned_qty INT NOT NULL DEFAULT 0 CHECK (returned_qty >= 0),
    unit_price NUMERIC(10, 2) NOT NULL CHECK (unit_price >= 0),
    total_amount NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    payment_mode payment_mode NOT NULL DEFAULT 'Cash',
    is_adhoc BOOLEAN NOT NULL DEFAULT false,
    partial_reason TEXT,
    delivered_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

-- Table: Invoices & Receipts (Ledger for Collections)
CREATE TABLE IF NOT EXISTS public.invoices (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    invoice_number TEXT UNIQUE NOT NULL,
    idempotency_key TEXT UNIQUE NOT NULL,
    load_slip_id UUID REFERENCES public.load_slips(id) ON DELETE SET NULL,
    load_slip_item_id UUID REFERENCES public.load_slip_items(id) ON DELETE SET NULL,
    customer_id UUID REFERENCES public.customers(id),
    customer_name TEXT NOT NULL,
    hub_name TEXT NOT NULL,
    product_name TEXT NOT NULL,
    delivered_qty INT NOT NULL CHECK (delivered_qty >= 0),
    unit_price NUMERIC(10, 2) NOT NULL,
    total_amount NUMERIC(12, 2) NOT NULL,
    payment_type payment_mode NOT NULL,
    status invoice_status NOT NULL,
    issue_date DATE NOT NULL DEFAULT (now() AT TIME ZONE 'Asia/Manila')::date,
    settled_at TIMESTAMP WITH TIME ZONE,
    settled_by TEXT,
    deleted_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
    deleted_by TEXT DEFAULT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

-- Table: GPS Telemetry Logs (Driver Smartphone Tracking During Active Shift)
CREATE TABLE IF NOT EXISTS public.gps_logs (
    id BIGSERIAL PRIMARY KEY,
    load_slip_id UUID REFERENCES public.load_slips(id) ON DELETE CASCADE,
    driver_id UUID REFERENCES public.user_accounts(id),
    truck_id TEXT REFERENCES public.trucks(id),
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    geom GEOMETRY(Point, 4326),
    speed_kmh NUMERIC(5, 2) DEFAULT 0.00,
    heading NUMERIC(5, 2) DEFAULT 0.00,
    battery_level NUMERIC(4, 2),
    is_charging BOOLEAN DEFAULT false,
    is_sos BOOLEAN DEFAULT false,
    logged_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

-- Table: End-of-Day Shift Reconciliations
CREATE TABLE IF NOT EXISTS public.shift_reconciliations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    truck_id TEXT NOT NULL REFERENCES public.trucks(id),
    load_slip_id UUID REFERENCES public.load_slips(id),
    reconciliation_date DATE NOT NULL DEFAULT (now() AT TIME ZONE 'Asia/Manila')::date,
    cash_remitted NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    charge_issued NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    sacks_returned INT NOT NULL DEFAULT 0,
    closed_at TIME NOT NULL,
    closed_by TEXT NOT NULL,
    notes TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

-- Table: System Audit Logs
CREATE TABLE IF NOT EXISTS public.audit_logs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    old_data JSONB,
    new_data JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

-- ------------------------------------------------------------------------------
-- 5. GEOMETRY TRIGGERS (Auto-sync lat/lng into PostGIS geometry points)
-- ------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.sync_hub_geom()
RETURNS TRIGGER AS $$
BEGIN
    NEW.geom := ST_SetSRID(ST_MakePoint(NEW.longitude, NEW.latitude), 4326);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER trg_sync_hub_geom
BEFORE INSERT OR UPDATE OF latitude, longitude ON public.delivery_hubs
FOR EACH ROW EXECUTE FUNCTION public.sync_hub_geom();

CREATE OR REPLACE FUNCTION public.sync_gps_geom()
RETURNS TRIGGER AS $$
BEGIN
    NEW.geom := ST_SetSRID(ST_MakePoint(NEW.longitude, NEW.latitude), 4326);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER trg_sync_gps_geom
BEFORE INSERT OR UPDATE OF latitude, longitude ON public.gps_logs
FOR EACH ROW EXECUTE FUNCTION public.sync_gps_geom();

-- ------------------------------------------------------------------------------
-- 6. SPATIAL, DELETED_AT & PERFORMANCE INDEXES
-- ------------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_delivery_hubs_geom ON public.delivery_hubs USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_delivery_hubs_deleted_at ON public.delivery_hubs (deleted_at);
CREATE INDEX IF NOT EXISTS idx_customers_deleted_at ON public.customers (deleted_at);
CREATE INDEX IF NOT EXISTS idx_products_deleted_at ON public.products (deleted_at);
CREATE INDEX IF NOT EXISTS idx_trucks_deleted_at ON public.trucks (deleted_at);
CREATE INDEX IF NOT EXISTS idx_user_accounts_deleted_at ON public.user_accounts (deleted_at);
CREATE INDEX IF NOT EXISTS idx_user_accounts_username ON public.user_accounts (username);
CREATE INDEX IF NOT EXISTS idx_gps_logs_geom ON public.gps_logs USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_gps_logs_recent ON public.gps_logs (load_slip_id, logged_at DESC);
CREATE INDEX IF NOT EXISTS idx_load_slips_date ON public.load_slips (date DESC);
CREATE INDEX IF NOT EXISTS idx_load_slips_deleted_at ON public.load_slips (deleted_at);
CREATE INDEX IF NOT EXISTS idx_load_slips_idempotency ON public.load_slips (idempotency_key);
CREATE INDEX IF NOT EXISTS idx_items_load_slip ON public.load_slip_items (load_slip_id);
CREATE INDEX IF NOT EXISTS idx_items_idempotency ON public.load_slip_items (idempotency_key);
CREATE INDEX IF NOT EXISTS idx_invoices_status_date ON public.invoices (status, issue_date DESC);
CREATE INDEX IF NOT EXISTS idx_invoices_deleted_at ON public.invoices (deleted_at);

-- ------------------------------------------------------------------------------
-- 7. ZERO-OVERAGE MAINTENANCE FUNCTIONS (Prune Old GPS Logs & Purge Trash)
-- ------------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.prune_old_gps_logs()
RETURNS INT AS $$
DECLARE
    deleted_rows INT;
BEGIN
    DELETE FROM public.gps_logs
    WHERE logged_at < (NOW() - INTERVAL '180 days');
    GET DIAGNOSTICS deleted_rows = ROW_COUNT;
    RETURN deleted_rows;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

CREATE OR REPLACE FUNCTION public.keep_alive_ping()
RETURNS TEXT AS $$
BEGIN
    RETURN 'JICA Database is active - ' || (now() AT TIME ZONE 'Asia/Manila')::TEXT;
END;
$$ LANGUAGE plpgsql;

-- Automated 30-Day Trash Purge Function
CREATE OR REPLACE FUNCTION public.purge_expired_trash()
RETURNS JSONB AS $$
DECLARE
    purged_summary JSONB := '{}'::JSONB;
    cnt INT;
BEGIN
    WITH deleted_slips AS (
        DELETE FROM public.load_slips
        WHERE deleted_at IS NOT NULL AND deleted_at < (NOW() - INTERVAL '30 days')
        RETURNING id
    )
    SELECT COUNT(*) INTO cnt FROM deleted_slips;
    purged_summary := jsonb_set(purged_summary, '{load_slips}', to_jsonb(cnt));

    WITH deleted_invs AS (
        DELETE FROM public.invoices
        WHERE deleted_at IS NOT NULL AND deleted_at < (NOW() - INTERVAL '30 days')
        RETURNING id
    )
    SELECT COUNT(*) INTO cnt FROM deleted_invs;
    purged_summary := jsonb_set(purged_summary, '{invoices}', to_jsonb(cnt));

    WITH deleted_prods AS (
        DELETE FROM public.products
        WHERE deleted_at IS NOT NULL AND deleted_at < (NOW() - INTERVAL '30 days')
        RETURNING id
    )
    SELECT COUNT(*) INTO cnt FROM deleted_prods;
    purged_summary := jsonb_set(purged_summary, '{products}', to_jsonb(cnt));

    WITH deleted_trks AS (
        DELETE FROM public.trucks
        WHERE deleted_at IS NOT NULL AND deleted_at < (NOW() - INTERVAL '30 days')
        RETURNING id
    )
    SELECT COUNT(*) INTO cnt FROM deleted_trks;
    purged_summary := jsonb_set(purged_summary, '{trucks}', to_jsonb(cnt));

    WITH deleted_hubs AS (
        DELETE FROM public.delivery_hubs
        WHERE deleted_at IS NOT NULL AND deleted_at < (NOW() - INTERVAL '30 days')
        RETURNING id
    )
    SELECT COUNT(*) INTO cnt FROM deleted_hubs;
    purged_summary := jsonb_set(purged_summary, '{delivery_hubs}', to_jsonb(cnt));

    WITH deleted_custs AS (
        DELETE FROM public.customers
        WHERE deleted_at IS NOT NULL AND deleted_at < (NOW() - INTERVAL '30 days')
        RETURNING id
    )
    SELECT COUNT(*) INTO cnt FROM deleted_custs;
    purged_summary := jsonb_set(purged_summary, '{customers}', to_jsonb(cnt));

    WITH deleted_users AS (
        DELETE FROM public.user_accounts
        WHERE deleted_at IS NOT NULL AND deleted_at < (NOW() - INTERVAL '30 days')
        RETURNING id
    )
    SELECT COUNT(*) INTO cnt FROM deleted_users;
    purged_summary := jsonb_set(purged_summary, '{user_accounts}', to_jsonb(cnt));

    RETURN purged_summary;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- ------------------------------------------------------------------------------
-- 8. ROW LEVEL SECURITY (RLS) POLICIES
-- ------------------------------------------------------------------------------
ALTER TABLE public.delivery_hubs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.customers ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.products ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.trucks ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.load_slips ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.load_slip_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.invoices ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.gps_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.shift_reconciliations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.audit_logs ENABLE ROW LEVEL SECURITY;

-- Reference Data Policies
CREATE POLICY "Public read reference data" ON public.delivery_hubs FOR SELECT USING (true);
CREATE POLICY "Public write reference data" ON public.delivery_hubs FOR ALL USING (true);

CREATE POLICY "Public read products" ON public.products FOR SELECT USING (true);
CREATE POLICY "Public write products" ON public.products FOR ALL USING (true);

CREATE POLICY "Public read trucks" ON public.trucks FOR SELECT USING (true);
CREATE POLICY "Public write trucks" ON public.trucks FOR ALL USING (true);

CREATE POLICY "Public read customers" ON public.customers FOR SELECT USING (true);
CREATE POLICY "Public write customers" ON public.customers FOR ALL USING (true);

-- User Accounts: Hardened Security
CREATE POLICY "Public read user accounts" ON public.user_accounts 
FOR SELECT USING (deleted_at IS NULL AND is_active = true);

CREATE POLICY "Service write user accounts" ON public.user_accounts 
FOR ALL TO service_role USING (true) WITH CHECK (true);

-- Operations Policies
CREATE POLICY "Allow read load slips" ON public.load_slips FOR SELECT USING (true);
CREATE POLICY "Allow write load slips" ON public.load_slips FOR ALL USING (true);

CREATE POLICY "Allow read items" ON public.load_slip_items FOR SELECT USING (true);
CREATE POLICY "Allow write items" ON public.load_slip_items FOR ALL USING (true);

CREATE POLICY "Allow read invoices" ON public.invoices FOR SELECT USING (true);
CREATE POLICY "Allow write invoices" ON public.invoices FOR ALL USING (true);

CREATE POLICY "Allow read reconciliations" ON public.shift_reconciliations FOR SELECT USING (true);
CREATE POLICY "Allow write reconciliations" ON public.shift_reconciliations FOR ALL USING (true);

CREATE POLICY "Allow read gps" ON public.gps_logs FOR SELECT USING (true);
CREATE POLICY "Allow write gps" ON public.gps_logs FOR ALL USING (true);

CREATE POLICY "Allow read audit logs" ON public.audit_logs FOR SELECT USING (true);
CREATE POLICY "Allow write audit logs" ON public.audit_logs FOR ALL USING (true);

-- ------------------------------------------------------------------------------
-- 9. STORAGE BUCKET CONFIGURATION (jica-assets - 5 MB LIMIT)
-- ------------------------------------------------------------------------------
INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
VALUES (
    'jica-assets',
    'jica-assets',
    true,
    5242880, -- 5 MB max per file
    ARRAY['image/jpeg', 'image/png', 'image/webp']
) ON CONFLICT (id) DO UPDATE SET file_size_limit = 5242880;

CREATE POLICY "Public access to jica-assets"
ON storage.objects FOR SELECT
USING (bucket_id = 'jica-assets');

CREATE POLICY "Allow authenticated/service upload to jica-assets"
ON storage.objects FOR INSERT
WITH CHECK (bucket_id = 'jica-assets');

CREATE POLICY "Allow update/delete to jica-assets"
ON storage.objects FOR ALL
USING (bucket_id = 'jica-assets');

-- ------------------------------------------------------------------------------
-- 10. MINIMAL BOOTSTRAP: INITIAL OWNER ACCOUNT ONLY
-- ------------------------------------------------------------------------------
INSERT INTO public.user_accounts (full_name, role, username, password, mobile_pin, phone_number, email)
VALUES ('Dino Trocino', 'Owner', 'owner', 'owner', '9999', '0917-111-2233', 'dino@jicaice.com')
ON CONFLICT (username) DO UPDATE SET
    full_name = EXCLUDED.full_name,
    role = EXCLUDED.role,
    password = EXCLUDED.password;
