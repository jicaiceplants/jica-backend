-- ==============================================================================
-- JICA ICE LOGISTICS & DELIVERY - CAGAYAN DE ORO CITY
-- PRODUCTION ROLLOUT MIGRATION SCRIPT (FIELD-TEST FIXES & ATOMIC DISPATCH)
-- ==============================================================================
-- Safe, idempotent migration script for Supabase SQL Editor.
-- Covers:
--   1. Strict Single Active Session (user_accounts.active_session_token & last_active_at)
--   2. Truck sack_capacity column + bidirectional sync with max_sack_capacity
--   3. Product & Load Slip workflow columns + Asia/Manila UTC+8 date defaults
--   4. Invoices & Receipts full metadata + load_slip_item_id idempotency
--   5. Atomic PostgreSQL RPC: dispatch_load_slip_atomic (FOR UPDATE row lock)
--   6. Safe delete indexes, FK integrity & Realtime publication setup
-- ==============================================================================

BEGIN;

-- ------------------------------------------------------------------------------
-- 1. PERMISSIONS & EXTENSIONS
-- ------------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

GRANT USAGE ON SCHEMA public TO postgres, anon, authenticated, service_role;
GRANT ALL ON ALL TABLES IN SCHEMA public TO postgres, anon, authenticated, service_role;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO postgres, anon, authenticated, service_role;
GRANT ALL ON ALL ROUTINES IN SCHEMA public TO postgres, anon, authenticated, service_role;

-- ------------------------------------------------------------------------------
-- 2. USER ACCOUNTS: STRICT SINGLE ACTIVE SESSION & CLEAN AUTH FIELDS
-- ------------------------------------------------------------------------------
ALTER TABLE public.user_accounts
    ADD COLUMN IF NOT EXISTS active_session_token TEXT DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS last_active_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS password_hash TEXT DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS deleted_by TEXT DEFAULT NULL;

DO $$ BEGIN
    ALTER TABLE public.user_accounts ALTER COLUMN password DROP NOT NULL;
EXCEPTION WHEN OTHERS THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS idx_user_accounts_session
    ON public.user_accounts (id, active_session_token);
CREATE INDEX IF NOT EXISTS idx_user_accounts_deleted_at
    ON public.user_accounts (deleted_at);

-- ------------------------------------------------------------------------------
-- 3. TRUCKS: sack_capacity FIELD & AUTOMATIC SYNC WITH max_sack_capacity
-- ------------------------------------------------------------------------------
ALTER TABLE public.trucks
    ADD COLUMN IF NOT EXISTS sack_capacity INT NOT NULL DEFAULT 120,
    ADD COLUMN IF NOT EXISTS max_sack_capacity INT NOT NULL DEFAULT 120,
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS deleted_by TEXT DEFAULT NULL;

UPDATE public.trucks
SET
    sack_capacity = COALESCE(NULLIF(sack_capacity, 0), NULLIF(max_sack_capacity, 0), 120),
    max_sack_capacity = COALESCE(NULLIF(max_sack_capacity, 0), NULLIF(sack_capacity, 0), 120)
WHERE sack_capacity IS DISTINCT FROM max_sack_capacity
   OR sack_capacity IS NULL
   OR max_sack_capacity IS NULL;

CREATE OR REPLACE FUNCTION public.sync_truck_capacity_columns()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.sack_capacity := COALESCE(NEW.sack_capacity, NEW.max_sack_capacity, 120);
        NEW.max_sack_capacity := NEW.sack_capacity;
    ELSIF TG_OP = 'UPDATE' THEN
        IF NEW.sack_capacity IS DISTINCT FROM OLD.sack_capacity THEN
            NEW.max_sack_capacity := NEW.sack_capacity;
        ELSIF NEW.max_sack_capacity IS DISTINCT FROM OLD.max_sack_capacity THEN
            NEW.sack_capacity := NEW.max_sack_capacity;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_sync_truck_capacity ON public.trucks;
CREATE TRIGGER trg_sync_truck_capacity
BEFORE INSERT OR UPDATE ON public.trucks
FOR EACH ROW EXECUTE FUNCTION public.sync_truck_capacity_columns();

CREATE INDEX IF NOT EXISTS idx_trucks_active
    ON public.trucks (id) WHERE deleted_at IS NULL AND is_active = true;

-- ------------------------------------------------------------------------------
-- 4. PRODUCTS & DELIVERY HUBS: ZERO-PRICE SUPPORT & SOFT DELETE ALIGNMENT
-- ------------------------------------------------------------------------------
ALTER TABLE public.products
    ADD COLUMN IF NOT EXISTS category TEXT DEFAULT 'Medium',
    ADD COLUMN IF NOT EXISTS sack_size TEXT DEFAULT 'Medium',
    ADD COLUMN IF NOT EXISTS image_url TEXT DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS deleted_by TEXT DEFAULT NULL;

UPDATE public.products
SET
    category = COALESCE(NULLIF(category, ''), NULLIF(sack_size, ''), NULLIF(description, ''), 'Medium'),
    description = COALESCE(NULLIF(sack_size, ''), NULLIF(description, ''), 'Medium'),
    sack_size = COALESCE(NULLIF(sack_size, ''), COALESCE(sack_weight_kg::INT::TEXT, 'Medium'))
WHERE category IS NULL OR sack_size IS NULL;

-- Ensure standard_price allows >= 0 (no negative prices)
ALTER TABLE public.products DROP CONSTRAINT IF EXISTS products_standard_price_check;
ALTER TABLE public.products ADD CONSTRAINT products_standard_price_check CHECK (standard_price >= 0);

ALTER TABLE public.delivery_hubs
    ADD COLUMN IF NOT EXISTS zone TEXT DEFAULT 'City Proper',
    ADD COLUMN IF NOT EXISTS photo_url TEXT DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS deleted_by TEXT DEFAULT NULL;

ALTER TABLE public.trucks
    ADD COLUMN IF NOT EXISTS image_url TEXT DEFAULT NULL;

ALTER TABLE public.user_accounts
    ADD COLUMN IF NOT EXISTS photo_url TEXT DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_products_active
    ON public.products (id) WHERE deleted_at IS NULL AND is_active = true;
CREATE INDEX IF NOT EXISTS idx_delivery_hubs_active
    ON public.delivery_hubs (id) WHERE deleted_at IS NULL AND is_active = true;

-- ------------------------------------------------------------------------------
-- 5. LOAD SLIPS & LOAD SLIP ITEMS: WORKFLOW STATUSES & IDEMPOTENCY
-- ------------------------------------------------------------------------------
-- Convert rigid enum slip_status to flexible TEXT for full dispatch lifecycle
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'load_slips' AND column_name = 'status' AND data_type = 'USER-DEFINED'
    ) THEN
        ALTER TABLE public.load_slips ALTER COLUMN status DROP DEFAULT;
        ALTER TABLE public.load_slips ALTER COLUMN status TYPE TEXT USING status::TEXT;
    END IF;
END $$;

ALTER TABLE public.load_slips
    ALTER COLUMN status SET DEFAULT 'DISPATCHED',
    ALTER COLUMN date SET DEFAULT (now() AT TIME ZONE 'Asia/Manila')::date,
    ADD COLUMN IF NOT EXISTS truck_plate TEXT DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS in_charge_name TEXT DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS driver_name TEXT DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS dispatched_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
    ADD COLUMN IF NOT EXISTS started_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS delivery_finished_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS remitted_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS remitted_by TEXT DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS notes TEXT DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS deleted_by TEXT DEFAULT NULL;

-- Normalize legacy slip statuses to clean workflow states
UPDATE public.load_slips
SET status = CASE status
    WHEN 'Pending' THEN 'DISPATCHED'
    WHEN 'Active' THEN 'DISPATCHED'
    WHEN 'In-Transit' THEN 'IN_TRANSIT'
    WHEN 'Completed' THEN 'REMITTED'
    WHEN 'Reconciled' THEN 'REMITTED'
    WHEN 'Cancelled' THEN 'CANCELLED'
    ELSE status
END
WHERE status IN ('Pending', 'Active', 'In-Transit', 'Completed', 'Reconciled', 'Cancelled');

ALTER TABLE public.load_slip_items
    ADD COLUMN IF NOT EXISTS hub_name TEXT DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS sack_size TEXT DEFAULT 'Standard Sack',
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'Pending',
    ADD COLUMN IF NOT EXISTS delivery_idempotency_key TEXT DEFAULT NULL;

DO $$ BEGIN
    ALTER TABLE public.load_slip_items ALTER COLUMN hub_id DROP NOT NULL;
EXCEPTION WHEN OTHERS THEN NULL;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_load_slip_items_delivery_idem
    ON public.load_slip_items (delivery_idempotency_key)
    WHERE delivery_idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_load_slips_truck_status
    ON public.load_slips (truck_id, status, date) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_load_slips_driver_status
    ON public.load_slips (driver_id, status, date) WHERE deleted_at IS NULL;

-- ------------------------------------------------------------------------------
-- 6. INVOICES & RECEIPTS: COMPLETE ITEMIZED METADATA & SHIFT RECONCILIATION
-- ------------------------------------------------------------------------------
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'invoices' AND column_name = 'status' AND data_type = 'USER-DEFINED'
    ) THEN
        ALTER TABLE public.invoices ALTER COLUMN status DROP DEFAULT;
        ALTER TABLE public.invoices ALTER COLUMN status TYPE TEXT USING status::TEXT;
    END IF;
END $$;

ALTER TABLE public.invoices
    ALTER COLUMN status SET DEFAULT 'Pending Delivery',
    ALTER COLUMN issue_date SET DEFAULT (now() AT TIME ZONE 'Asia/Manila')::date,
    ADD COLUMN IF NOT EXISTS slip_number TEXT DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS truck_id TEXT DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS truck_plate TEXT DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS driver_id UUID DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS driver_name TEXT DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS in_charge_id UUID DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS in_charge_name TEXT DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS sack_size TEXT DEFAULT 'Standard Sack',
    ADD COLUMN IF NOT EXISTS planned_qty INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS returned_qty INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS issued_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS deleted_by TEXT DEFAULT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_invoices_load_slip_item_unique
    ON public.invoices (load_slip_item_id)
    WHERE load_slip_item_id IS NOT NULL AND deleted_at IS NULL;

-- Ensure GPS logs & Shift Reconciliations prevent duplicates & respect Manila timezone
ALTER TABLE public.gps_logs
    ADD COLUMN IF NOT EXISTS idempotency_key TEXT DEFAULT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_gps_logs_idempotency
    ON public.gps_logs (idempotency_key)
    WHERE idempotency_key IS NOT NULL;

ALTER TABLE public.shift_reconciliations
    ALTER COLUMN reconciliation_date SET DEFAULT (now() AT TIME ZONE 'Asia/Manila')::date;

CREATE UNIQUE INDEX IF NOT EXISTS idx_shift_reconciliations_slip_unique
    ON public.shift_reconciliations (load_slip_id)
    WHERE load_slip_id IS NOT NULL;

-- Backfill invoice truck/driver/in-charge metadata from linked load_slips where missing
UPDATE public.invoices inv
SET
    slip_number = COALESCE(inv.slip_number, ls.slip_number),
    truck_id = COALESCE(inv.truck_id, ls.truck_id),
    truck_plate = COALESCE(inv.truck_plate, trk.plate_number, ls.truck_plate, ls.truck_id),
    driver_id = COALESCE(inv.driver_id, ls.driver_id),
    driver_name = COALESCE(inv.driver_name, drv.full_name, ls.driver_name),
    in_charge_id = COALESCE(inv.in_charge_id, ls.in_charge_id),
    in_charge_name = COALESCE(inv.in_charge_name, ls.in_charge_name, ls.created_by)
FROM public.load_slips ls
LEFT JOIN public.trucks trk ON trk.id = ls.truck_id
LEFT JOIN public.user_accounts drv ON drv.id = ls.driver_id
WHERE inv.load_slip_id = ls.id
  AND (inv.slip_number IS NULL OR inv.truck_plate IS NULL OR inv.driver_name IS NULL OR inv.in_charge_name IS NULL);

-- Clean relational Receipts View combining Invoice, Load Slip, Truck & Driver metadata
CREATE OR REPLACE VIEW public.receipts_view AS
SELECT
    inv.id,
    inv.invoice_number,
    inv.idempotency_key,
    inv.load_slip_id,
    inv.load_slip_item_id,
    COALESCE(inv.slip_number, ls.slip_number) AS slip_number,
    COALESCE(inv.truck_id, ls.truck_id) AS truck_id,
    COALESCE(inv.truck_plate, trk.plate_number, ls.truck_id) AS truck_plate,
    COALESCE(inv.driver_id, ls.driver_id) AS driver_id,
    COALESCE(inv.driver_name, drv.full_name, ls.driver_name, 'Unassigned Driver') AS driver_name,
    COALESCE(inv.in_charge_id, ls.in_charge_id) AS in_charge_id,
    COALESCE(inv.in_charge_name, inc.full_name, ls.in_charge_name, ls.created_by, 'Dispatch Staff') AS in_charge_name,
    inv.customer_id,
    inv.customer_name,
    inv.hub_name,
    inv.product_name,
    inv.sack_size,
    COALESCE(NULLIF(inv.planned_qty, 0), lsi.planned_qty, inv.delivered_qty) AS planned_qty,
    inv.delivered_qty,
    inv.returned_qty,
    inv.unit_price,
    inv.total_amount,
    inv.payment_type,
    inv.status,
    ls.status AS load_slip_status,
    inv.issue_date,
    inv.issued_at,
    inv.settled_at,
    inv.settled_by,
    inv.created_at,
    inv.updated_at
FROM public.invoices inv
LEFT JOIN public.load_slips ls ON ls.id = inv.load_slip_id
LEFT JOIN public.load_slip_items lsi ON lsi.id = inv.load_slip_item_id
LEFT JOIN public.trucks trk ON trk.id = COALESCE(inv.truck_id, ls.truck_id)
LEFT JOIN public.user_accounts drv ON drv.id = COALESCE(inv.driver_id, ls.driver_id)
LEFT JOIN public.user_accounts inc ON inc.id = COALESCE(inv.in_charge_id, ls.in_charge_id)
WHERE inv.deleted_at IS NULL
  AND UPPER(TRIM(COALESCE(ls.status, ''))) IN ('REMITTED', 'COMPLETED', 'RECONCILED');

-- ------------------------------------------------------------------------------
-- 7. ATOMIC CONCURRENT DISPATCH RPC (ACID CAPACITY GUARD & ITEMIZED INVOICING)
-- ------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.dispatch_load_slip_atomic(
    p_slip_number TEXT,
    p_idempotency_key TEXT,
    p_date DATE,
    p_departure_time TIME,
    p_truck_id TEXT,
    p_in_charge_id UUID,
    p_in_charge_name TEXT,
    p_driver_id UUID,
    p_driver_name TEXT,
    p_notes TEXT,
    p_items JSONB
)
RETURNS JSONB AS $$
DECLARE
    v_existing RECORD;
    v_truck RECORD;
    v_capacity INT;
    v_used_sacks INT;
    v_remaining INT;
    v_requested_sacks INT := 0;
    v_in_transit_count INT := 0;
    v_slip_id UUID;
    v_item JSONB;
    v_item_id UUID;
    v_idx INT := 0;
    v_total_cash NUMERIC(12, 2) := 0.00;
    v_total_charge NUMERIC(12, 2) := 0.00;
    v_planned INT;
    v_unit_price NUMERIC(10, 2);
    v_line_total NUMERIC(12, 2);
    v_pay_mode payment_mode;
    v_hub_uuid UUID;
    v_cust_uuid UUID;
    v_prod_uuid UUID;
    v_inv_num TEXT;
    v_slip_suffix TEXT;
    v_manila_now TIMESTAMPTZ := now();
BEGIN
    -- 1. Idempotency protection: return existing slip cleanly if already created
    SELECT id, slip_number, status, total_loaded_sacks
    INTO v_existing
    FROM public.load_slips
    WHERE idempotency_key = p_idempotency_key
      AND deleted_at IS NULL
    LIMIT 1;

    IF FOUND THEN
        RETURN jsonb_build_object(
            'ok', true,
            'duplicate', true,
            'load_slip_id', v_existing.id,
            'slip_number', v_existing.slip_number,
            'status', v_existing.status
        );
    END IF;

    -- 2. Lock target truck row (FOR UPDATE) so concurrent dispatch requests serialize atomically
    SELECT *
    INTO v_truck
    FROM public.trucks
    WHERE id = p_truck_id
      AND deleted_at IS NULL
      AND is_active = true
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object(
            'ok', false,
            'code', 'TRUCK_NOT_FOUND',
            'message', 'Selected truck does not exist or is inactive.'
        );
    END IF;

    v_capacity := COALESCE(v_truck.sack_capacity, v_truck.max_sack_capacity, 120);

    -- Sum planned sacks from input JSON
    IF p_items IS NULL OR jsonb_typeof(p_items) <> 'array' OR jsonb_array_length(p_items) = 0 THEN
        RETURN jsonb_build_object(
            'ok', false,
            'code', 'EMPTY_ITEMS',
            'message', 'Load Slip must contain at least one delivery item.'
        );
    END IF;

    FOR v_item IN SELECT * FROM jsonb_array_elements(p_items)
    LOOP
        v_planned := GREATEST(0, COALESCE((v_item->>'planned_qty')::INT, 0));
        v_unit_price := ROUND(GREATEST(0, COALESCE((v_item->>'unit_price')::NUMERIC, 0)), 2);
        v_line_total := ROUND(COALESCE((v_item->>'total_amount')::NUMERIC, v_planned * v_unit_price), 2);
        v_requested_sacks := v_requested_sacks + v_planned;

        IF LOWER(COALESCE(v_item->>'payment_mode', 'cash')) LIKE '%charge%' THEN
            v_total_charge := v_total_charge + v_line_total;
        ELSE
            v_total_cash := v_total_cash + v_line_total;
        END IF;
    END LOOP;

    -- 3. Sum unremitted active sacks on this truck (across current shift / active unremitted slips)
    SELECT
        COALESCE(SUM(total_loaded_sacks), 0),
        COALESCE(COUNT(*) FILTER (WHERE UPPER(status) IN ('IN_TRANSIT', 'IN-TRANSIT')), 0)
    INTO v_used_sacks, v_in_transit_count
    FROM public.load_slips
    WHERE truck_id = p_truck_id
      AND deleted_at IS NULL
      AND UPPER(status) NOT IN ('REMITTED', 'COMPLETED', 'RECONCILED', 'CANCELLED');

    v_remaining := GREATEST(0, v_capacity - v_used_sacks);

    -- 4. Enforce strict capacity limit inside locked transaction
    IF v_requested_sacks > v_remaining THEN
        RETURN jsonb_build_object(
            'ok', false,
            'code', 'CAPACITY_EXCEEDED',
            'capacity', v_capacity,
            'used_sacks', v_used_sacks,
            'remaining_sacks', v_remaining,
            'requested_sacks', v_requested_sacks,
            'message', format('Capacity exceeded: Another staff member just dispatched to this truck. Only %s sacks remaining.', v_remaining)
        );
    END IF;

    -- 5. Insert Load Slip Header (status strictly 'DISPATCHED')
    INSERT INTO public.load_slips (
        slip_number,
        idempotency_key,
        date,
        departure_time,
        truck_id,
        truck_plate,
        in_charge_id,
        in_charge_name,
        driver_id,
        driver_name,
        status,
        total_loaded_sacks,
        total_delivered_sacks,
        total_returned_sacks,
        total_cash_revenue,
        total_charge_revenue,
        notes,
        created_by,
        updated_by,
        dispatched_at,
        created_at,
        updated_at
    ) VALUES (
        p_slip_number,
        p_idempotency_key,
        COALESCE(p_date, (now() AT TIME ZONE 'Asia/Manila')::date),
        COALESCE(p_departure_time, (now() AT TIME ZONE 'Asia/Manila')::time),
        p_truck_id,
        v_truck.plate_number,
        p_in_charge_id,
        p_in_charge_name,
        p_driver_id,
        p_driver_name,
        'DISPATCHED',
        v_requested_sacks,
        0,
        0,
        v_total_cash,
        v_total_charge,
        p_notes,
        COALESCE(p_in_charge_name, 'Dispatch Staff'),
        COALESCE(p_in_charge_name, 'Dispatch Staff'),
        v_manila_now,
        v_manila_now,
        v_manila_now
    )
    RETURNING id INTO v_slip_id;

    v_slip_suffix := UPPER(RIGHT(REPLACE(v_slip_id::TEXT, '-', ''), 4));

    -- 6. Insert Load Slip Items + Corresponding Initial Itemized Invoices/Receipts
    FOR v_item IN SELECT * FROM jsonb_array_elements(p_items)
    LOOP
        v_idx := v_idx + 1;
        v_planned := GREATEST(0, COALESCE((v_item->>'planned_qty')::INT, 0));
        v_unit_price := ROUND(GREATEST(0, COALESCE((v_item->>'unit_price')::NUMERIC, 0)), 2);
        v_line_total := ROUND(COALESCE((v_item->>'total_amount')::NUMERIC, v_planned * v_unit_price), 2);
        v_pay_mode := CASE
            WHEN LOWER(COALESCE(v_item->>'payment_mode', 'cash')) LIKE '%charge%' THEN 'Charge'::payment_mode
            ELSE 'Cash'::payment_mode
        END;

        v_hub_uuid := CASE WHEN COALESCE(v_item->>'hub_id', '') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            THEN (v_item->>'hub_id')::UUID ELSE NULL END;
        v_cust_uuid := CASE WHEN COALESCE(v_item->>'customer_id', '') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            THEN (v_item->>'customer_id')::UUID ELSE NULL END;
        v_prod_uuid := CASE WHEN COALESCE(v_item->>'product_id', '') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            THEN (v_item->>'product_id')::UUID ELSE NULL END;

        INSERT INTO public.load_slip_items (
            load_slip_id,
            idempotency_key,
            sequence_order,
            hub_id,
            hub_name,
            customer_id,
            customer_name,
            product_id,
            product_name,
            sack_size,
            planned_qty,
            delivered_qty,
            returned_qty,
            unit_price,
            total_amount,
            payment_mode,
            status,
            is_adhoc
        ) VALUES (
            v_slip_id,
            COALESCE(NULLIF(v_item->>'idempotency_key', ''), p_idempotency_key || '-' || v_idx),
            v_idx,
            v_hub_uuid,
            COALESCE(NULLIF(v_item->>'hub_name', ''), 'Cagayan de Oro Hub'),
            v_cust_uuid,
            COALESCE(NULLIF(v_item->>'customer_name', ''), 'Customer Store'),
            v_prod_uuid,
            COALESCE(NULLIF(v_item->>'product_name', ''), 'Ice Product'),
            COALESCE(NULLIF(v_item->>'sack_size', ''), 'Standard Sack'),
            v_planned,
            0,
            0,
            v_unit_price,
            v_line_total,
            v_pay_mode,
            'Pending',
            false
        )
        RETURNING id INTO v_item_id;

        v_inv_num := format(
            '%s-%s-%s-%s',
            CASE WHEN v_pay_mode = 'Charge'::payment_mode THEN 'CI' ELSE 'OR' END,
            to_char(COALESCE(p_date, (now() AT TIME ZONE 'Asia/Manila')::date), 'YYYYMMDD'),
            v_slip_suffix,
            LPAD(v_idx::TEXT, 2, '0')
        );

        INSERT INTO public.invoices (
            invoice_number,
            idempotency_key,
            load_slip_id,
            load_slip_item_id,
            slip_number,
            truck_id,
            truck_plate,
            driver_id,
            driver_name,
            in_charge_id,
            in_charge_name,
            customer_id,
            customer_name,
            hub_name,
            product_name,
            sack_size,
            planned_qty,
            delivered_qty,
            returned_qty,
            unit_price,
            total_amount,
            payment_type,
            status,
            issue_date,
            issued_at,
            created_at,
            updated_at
        ) VALUES (
            v_inv_num,
            'INV-' || v_item_id::TEXT,
            v_slip_id,
            v_item_id,
            p_slip_number,
            p_truck_id,
            v_truck.plate_number,
            p_driver_id,
            p_driver_name,
            p_in_charge_id,
            p_in_charge_name,
            v_cust_uuid,
            COALESCE(NULLIF(v_item->>'customer_name', ''), 'Customer Store'),
            COALESCE(NULLIF(v_item->>'hub_name', ''), 'Cagayan de Oro Hub'),
            COALESCE(NULLIF(v_item->>'product_name', ''), 'Ice Product'),
            COALESCE(NULLIF(v_item->>'sack_size', ''), 'Standard Sack'),
            v_planned,
            0,
            0,
            v_unit_price,
            v_line_total,
            v_pay_mode,
            'Pending Delivery',
            COALESCE(p_date, (now() AT TIME ZONE 'Asia/Manila')::date),
            v_manila_now,
            v_manila_now,
            v_manila_now
        );
    END LOOP;

    RETURN jsonb_build_object(
        'ok', true,
        'duplicate', false,
        'load_slip_id', v_slip_id,
        'slip_number', p_slip_number,
        'status', 'DISPATCHED',
        'capacity', v_capacity,
        'used_sacks', v_used_sacks + v_requested_sacks,
        'remaining_sacks', GREATEST(0, v_remaining - v_requested_sacks),
        'in_transit_warning', (v_in_transit_count > 0)
    );
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- ------------------------------------------------------------------------------
-- 8. SUPABASE STORAGE BUCKET (jica-assets) & FREE-TIER IMAGE POLICIES
-- ------------------------------------------------------------------------------
INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
VALUES (
    'jica-assets',
    'jica-assets',
    true,
    5242880, -- 5 MB hard upper ceiling; web uploads pre-compress to ~40-100 KB WebP/JPEG
    ARRAY['image/webp', 'image/jpeg', 'image/png']
)
ON CONFLICT (id) DO UPDATE SET
    public = true,
    file_size_limit = 5242880,
    allowed_mime_types = ARRAY['image/webp', 'image/jpeg', 'image/png'];

DROP POLICY IF EXISTS "Public access to jica-assets" ON storage.objects;
DROP POLICY IF EXISTS "Allow authenticated/service upload to jica-assets" ON storage.objects;
DROP POLICY IF EXISTS "Allow update/delete to jica-assets" ON storage.objects;
DROP POLICY IF EXISTS "jica_assets_public_read" ON storage.objects;
DROP POLICY IF EXISTS "jica_assets_public_insert" ON storage.objects;
DROP POLICY IF EXISTS "jica_assets_public_update" ON storage.objects;
DROP POLICY IF EXISTS "jica_assets_public_delete" ON storage.objects;

CREATE POLICY "jica_assets_public_read"
ON storage.objects FOR SELECT
USING (bucket_id = 'jica-assets');

CREATE POLICY "jica_assets_public_insert"
ON storage.objects FOR INSERT
WITH CHECK (bucket_id = 'jica-assets');

CREATE POLICY "jica_assets_public_update"
ON storage.objects FOR UPDATE
USING (bucket_id = 'jica-assets')
WITH CHECK (bucket_id = 'jica-assets');

CREATE POLICY "jica_assets_public_delete"
ON storage.objects FOR DELETE
USING (bucket_id = 'jica-assets');

-- ------------------------------------------------------------------------------
-- 9. PERSISTENT EMERGENCY SOS ALERTS (sos_alerts)
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.sos_alerts (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE NOT NULL,
    load_slip_id UUID REFERENCES public.load_slips(id) ON DELETE SET NULL,
    driver_id TEXT,
    driver_name TEXT,
    truck_id TEXT NOT NULL,
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Active',
    dispatcher_reply TEXT,
    acknowledged_by TEXT,
    acknowledged_at TIMESTAMP WITH TIME ZONE,
    resolved_by TEXT,
    resolved_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sos_alerts_status_created
    ON public.sos_alerts (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sos_alerts_truck_status
    ON public.sos_alerts (truck_id, status);

GRANT ALL ON TABLE public.sos_alerts TO postgres, anon, authenticated, service_role;

ALTER TABLE public.sos_alerts ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "jica_sos_alerts_all" ON public.sos_alerts;
CREATE POLICY "jica_sos_alerts_all"
ON public.sos_alerts FOR ALL
USING (true)
WITH CHECK (true);

-- ------------------------------------------------------------------------------
-- 10. SUPABASE REALTIME PUBLICATION FOR LIVE WEB & MOBILE SYNC
-- ------------------------------------------------------------------------------
DO $$
DECLARE
    t_name TEXT;
    realtime_tables TEXT[] := ARRAY[
        'user_accounts',
        'trucks',
        'products',
        'delivery_hubs',
        'load_slips',
        'load_slip_items',
        'invoices',
        'shift_reconciliations',
        'sos_alerts'
    ];
BEGIN
    FOREACH t_name IN ARRAY realtime_tables
    LOOP
        BEGIN
            EXECUTE format('ALTER PUBLICATION supabase_realtime ADD TABLE public.%I', t_name);
        EXCEPTION
            WHEN duplicate_object THEN NULL;
            WHEN OTHERS THEN NULL;
        END;
    END LOOP;
END $$;

COMMIT;

-- Verification output
SELECT 'JICA Field-Test Stabilization Migration Applied Successfully!' AS migration_status;
