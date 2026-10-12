-- Shared by search and price Jobs. Callers get EXECUTE on the functions below, never the tables.
CREATE SCHEMA IF NOT EXISTS coordination;
REVOKE ALL ON SCHEMA coordination FROM PUBLIC;

CREATE TABLE IF NOT EXISTS coordination.search_executions (
    execution_id text PRIMARY KEY,
    work text NOT NULL,
    owner text NOT NULL,
    fence bigint NOT NULL UNIQUE,
    admitted_at timestamptz NOT NULL,
    deadline_at timestamptz NOT NULL,
    ended_at timestamptz,
    ended_by text CHECK (ended_by IN ('cleanup', 'recovery')),
    note text
);
-- The one fresh-search slot: an execution holds it until its cleanup or recovery is recorded.
CREATE UNIQUE INDEX IF NOT EXISTS one_fresh_search
    ON coordination.search_executions ((true)) WHERE ended_at IS NULL;
CREATE SEQUENCE IF NOT EXISTS coordination.fences;

-- The strictest Site policy per retailer: a caller's own policy can only tighten it.
CREATE TABLE IF NOT EXISTS coordination.retailer_policies (
    retailer text PRIMARY KEY,
    min_interval_ms integer NOT NULL CHECK (min_interval_ms >= 0),
    max_concurrent integer NOT NULL CHECK (max_concurrent >= 1)
);
CREATE TABLE IF NOT EXISTS coordination.retailer_pace (
    retailer text PRIMARY KEY,
    next_start_at timestamptz NOT NULL
);
-- A request in flight. Price Job permits lapse with their lease; search permits end with
-- their execution, since a lease alone doesn't prove the browser stopped.
CREATE TABLE IF NOT EXISTS coordination.permits (
    id bigserial PRIMARY KEY,
    retailer text NOT NULL,
    holder text NOT NULL,
    execution_id text REFERENCES coordination.search_executions ON DELETE CASCADE,
    granted_at timestamptz NOT NULL,
    expires_at timestamptz,
    CHECK ((execution_id IS NULL) <> (expires_at IS NULL))
);
CREATE INDEX IF NOT EXISTS permits_by_retailer ON coordination.permits (retailer);

CREATE OR REPLACE FUNCTION coordination.acquire_permit(
    p_retailer text, p_holder text, p_min_interval_ms integer, p_max_concurrent integer,
    p_lease_ms integer, p_execution text, p_fence bigint, p_paced boolean, p_charge boolean
) RETURNS TABLE (permit_id bigint, granted_at timestamptz, wait_until timestamptz, refused text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = coordination, pg_temp AS $$
DECLARE
    v_now timestamptz;
    v_next timestamptz;
    v_interval interval;
    v_limit integer;
    v_active integer;
BEGIN
    IF p_execution IS NOT NULL THEN
        -- Held until commit, so the execution can't end between this check and the grant.
        PERFORM 1 FROM coordination.search_executions e
            WHERE e.execution_id = p_execution AND e.fence = p_fence AND e.ended_at IS NULL
                AND e.deadline_at > clock_timestamp()
            FOR SHARE;
        IF NOT FOUND THEN
            RETURN QUERY SELECT NULL::bigint, NULL::timestamptz, NULL::timestamptz, 'stale';
            RETURN;
        END IF;
        -- Search may only tighten a configured retailer's limits, never set them.
        IF NOT EXISTS (
            SELECT 1 FROM coordination.retailer_policies p WHERE p.retailer = p_retailer
        ) THEN
            RETURN QUERY SELECT NULL::bigint, NULL::timestamptz, NULL::timestamptz,
                'unconfigured';
            RETURN;
        END IF;
    END IF;
    INSERT INTO coordination.retailer_pace VALUES (p_retailer, '-infinity') ON CONFLICT DO NOTHING;
    -- Serializes this retailer's grants across every Job and search.
    SELECT r.next_start_at INTO v_next FROM coordination.retailer_pace r
        WHERE r.retailer = p_retailer FOR UPDATE;
    v_now := clock_timestamp();
    SELECT make_interval(secs => greatest(p_min_interval_ms, coalesce(max(p.min_interval_ms), 0))
                                 / 1000.0),
           least(p_max_concurrent, coalesce(min(p.max_concurrent), p_max_concurrent))
        INTO v_interval, v_limit
        FROM coordination.retailer_policies p WHERE p.retailer = p_retailer;
    DELETE FROM coordination.permits p WHERE p.retailer = p_retailer AND p.expires_at <= v_now;
    SELECT count(*) INTO v_active FROM coordination.permits p WHERE p.retailer = p_retailer;
    IF NOT p_charge THEN
        IF v_active >= v_limit THEN
            RETURN QUERY SELECT NULL::bigint, NULL::timestamptz, NULL::timestamptz, NULL::text;
            RETURN;
        END IF;
        IF p_paced AND v_now < v_next THEN
            RETURN QUERY SELECT NULL::bigint, NULL::timestamptz, v_next, NULL::text;
            RETURN;
        END IF;
    END IF;
    -- A charged request may have come early: the pace owes its interval from the next start.
    IF p_paced THEN
        UPDATE coordination.retailer_pace r
            SET next_start_at = greatest(v_next, v_now) + v_interval
            WHERE r.retailer = p_retailer;
    END IF;
    RETURN QUERY INSERT INTO coordination.permits AS p
            (retailer, holder, execution_id, granted_at, expires_at)
        VALUES (p_retailer, p_holder, p_execution, v_now,
                CASE WHEN p_execution IS NULL
                     THEN v_now + make_interval(secs => p_lease_ms / 1000.0) END)
        RETURNING p.id, p.granted_at, NULL::timestamptz, NULL::text;
END $$;

CREATE OR REPLACE FUNCTION coordination.release_permit(p_id bigint, p_holder text)
RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path = coordination, pg_temp AS $$
    DELETE FROM coordination.permits WHERE id = p_id AND holder = p_holder;
$$;

CREATE OR REPLACE FUNCTION coordination.admit_search(
    p_execution text, p_work text, p_owner text, p_deadline timestamptz
) RETURNS TABLE (outcome text, fence bigint, deadline_at timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = coordination, pg_temp AS $$
DECLARE
    known coordination.search_executions;
BEGIN
    -- Serializes admissions, so a free slot and a known id are decided together.
    LOCK TABLE coordination.search_executions IN SHARE ROW EXCLUSIVE MODE;
    SELECT * INTO known FROM coordination.search_executions e WHERE e.execution_id = p_execution;
    IF FOUND THEN
        IF known.ended_at IS NULL AND known.work = p_work AND known.owner = p_owner THEN
            RETURN QUERY SELECT 'admitted', known.fence, known.deadline_at;
        ELSE
            RETURN QUERY SELECT 'replayed', NULL::bigint, NULL::timestamptz;
        END IF;
        RETURN;
    END IF;
    IF p_deadline <= clock_timestamp() THEN
        RETURN QUERY SELECT 'expired', NULL::bigint, NULL::timestamptz;
        RETURN;
    END IF;
    IF EXISTS (SELECT 1 FROM coordination.search_executions e WHERE e.ended_at IS NULL) THEN
        RETURN QUERY SELECT 'busy', NULL::bigint, NULL::timestamptz;
        RETURN;
    END IF;
    -- Ids stay refusable for a day past their deadline; later, the deadline alone refuses them.
    DELETE FROM coordination.search_executions e
        WHERE e.ended_at IS NOT NULL AND e.deadline_at < clock_timestamp() - interval '1 day';
    RETURN QUERY INSERT INTO coordination.search_executions AS e
        VALUES (p_execution, p_work, p_owner, nextval('coordination.fences'),
                clock_timestamp(), p_deadline)
        RETURNING 'admitted', e.fence, e.deadline_at;
END $$;

-- The owner reports that the execution's processes and browser are gone.
CREATE OR REPLACE FUNCTION coordination.end_search(p_execution text, p_fence bigint)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = coordination, pg_temp AS $$
BEGIN
    UPDATE coordination.search_executions e SET ended_at = clock_timestamp(), ended_by = 'cleanup'
        WHERE e.execution_id = p_execution AND e.fence = p_fence AND e.ended_at IS NULL;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    DELETE FROM coordination.permits p WHERE p.execution_id = p_execution;
    RETURN true;
END $$;

-- Operator only: after confirming the owner's Pod or process tree is terminated.
CREATE OR REPLACE FUNCTION coordination.recover_search(
    p_execution text, p_fence bigint, p_owner text, p_note text
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = coordination, pg_temp AS $$
BEGIN
    UPDATE coordination.search_executions e
        SET ended_at = clock_timestamp(), ended_by = 'recovery', note = p_note
        WHERE e.execution_id = p_execution AND e.fence = p_fence AND e.owner = p_owner
            AND e.ended_at IS NULL;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    DELETE FROM coordination.permits p WHERE p.execution_id = p_execution;
    RETURN true;
END $$;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA coordination FROM PUBLIC;
