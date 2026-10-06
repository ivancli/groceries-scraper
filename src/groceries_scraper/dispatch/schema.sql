CREATE SCHEMA IF NOT EXISTS dispatch;
CREATE TABLE IF NOT EXISTS dispatch.collector_state (
    key text PRIMARY KEY,
    value text NOT NULL
);
CREATE TABLE IF NOT EXISTS dispatch.watch_entries (
    ref text PRIMARY KEY,
    url text NOT NULL,
    location text,
    check_soon boolean NOT NULL DEFAULT false,
    etag text
);
CREATE TABLE IF NOT EXISTS dispatch.dispatches (
    site text NOT NULL,
    run_id text NOT NULL,
    location text NOT NULL,
    job_name text NOT NULL UNIQUE,
    refs text[] NOT NULL,
    created_at timestamptz NOT NULL,
    finished_at timestamptz,
    ingested_at timestamptz,
    PRIMARY KEY (site, run_id)
);
CREATE TABLE IF NOT EXISTS dispatch.checks (
    site text NOT NULL,
    location text NOT NULL,
    ref text NOT NULL,
    run_id text NOT NULL,
    outcome text NOT NULL,
    error text,
    at timestamptz NOT NULL,
    PRIMARY KEY (site, run_id, ref),
    FOREIGN KEY (site, run_id) REFERENCES dispatch.dispatches
);
CREATE TABLE IF NOT EXISTS dispatch.price_observations (
    site text NOT NULL,
    location text NOT NULL,
    ref text NOT NULL,
    run_id text NOT NULL,
    capture_no integer,
    observed_at timestamptz NOT NULL,
    shelf_price_cents bigint NOT NULL,
    regular_price_cents bigint,
    is_deal boolean,
    unit_price_cents bigint,
    unit_basis text,
    unit_price_text text,
    price_kind text,
    availability text,
    store_verified boolean,
    promo_text text,
    name text NOT NULL,
    brand text,
    size_text text,
    product_url text NOT NULL,
    PRIMARY KEY (site, run_id, ref),
    FOREIGN KEY (site, run_id, ref) REFERENCES dispatch.checks
);
CREATE INDEX IF NOT EXISTS price_observations_by_ref
    ON dispatch.price_observations (ref, observed_at);
CREATE TABLE IF NOT EXISTS dispatch.latest_state (
    ref text PRIMARY KEY,
    state_hash text,
    last_success_at timestamptz,
    last_attempt_at timestamptz,
    health text CHECK (health IN ('ok', 'failing')),
    failing_since timestamptz,
    consecutive_failures integer NOT NULL DEFAULT 0,
    last_heartbeat_date date,
    reported_outcomes text[] NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS dispatch.outbox (
    id text PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('change', 'health', 'heartbeat', 'outcome')),
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL,
    sent_at timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    last_error text
);
CREATE INDEX IF NOT EXISTS outbox_pending ON dispatch.outbox (created_at, id)
    WHERE sent_at IS NULL;
ALTER TABLE dispatch.outbox ADD COLUMN IF NOT EXISTS next_attempt_at timestamptz;
CREATE INDEX IF NOT EXISTS heartbeats_by_ref ON dispatch.outbox ((payload->>'ref'), created_at)
    WHERE kind = 'heartbeat';
