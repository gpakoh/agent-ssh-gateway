-- Migration: timestamp without time zone → timestamp with time zone
-- File: scripts/migrate_tz_aware.sql
--
-- Safety: Uses AT TIME ZONE 'UTC' to convert existing naive UTC data.
-- Idempotent: Checks information_schema before altering each column.
-- Reversible: Run migrate_tz_aware_down.sql to revert.
--
-- Usage:
--   docker exec -i mcp-postgres psql -U postgres -d gateway < scripts/migrate_tz_aware.sql
--
-- Verification:
--   docker exec mcp-postgres psql -U postgres -d gateway -c "
--     SELECT table_name, column_name, data_type
--     FROM information_schema.columns
--     WHERE table_schema = 'public'
--       AND data_type = 'timestamp without time zone'
--       AND table_name IN ('ssh_sessions','event_hooks','webhook_deliveries');
--   "
-- Expected: 0 rows

BEGIN;

-- ssh_sessions
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema='public' AND table_name='ssh_sessions'
          AND column_name='connected_at' AND data_type='timestamp without time zone'
    ) THEN
        ALTER TABLE ssh_sessions ALTER COLUMN connected_at
            TYPE timestamp with time zone USING connected_at AT TIME ZONE 'UTC';
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema='public' AND table_name='ssh_sessions'
          AND column_name='last_activity' AND data_type='timestamp without time zone'
    ) THEN
        ALTER TABLE ssh_sessions ALTER COLUMN last_activity
            TYPE timestamp with time zone USING last_activity AT TIME ZONE 'UTC';
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema='public' AND table_name='ssh_sessions'
          AND column_name='expires_at' AND data_type='timestamp without time zone'
    ) THEN
        ALTER TABLE ssh_sessions ALTER COLUMN expires_at
            TYPE timestamp with time zone USING expires_at AT TIME ZONE 'UTC';
    END IF;
END $$;

-- event_hooks
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema='public' AND table_name='event_hooks'
          AND column_name='created_at' AND data_type='timestamp without time zone'
    ) THEN
        ALTER TABLE event_hooks ALTER COLUMN created_at
            TYPE timestamp with time zone USING created_at AT TIME ZONE 'UTC';
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema='public' AND table_name='event_hooks'
          AND column_name='updated_at' AND data_type='timestamp without time zone'
    ) THEN
        ALTER TABLE event_hooks ALTER COLUMN updated_at
            TYPE timestamp with time zone USING updated_at AT TIME ZONE 'UTC';
    END IF;
END $$;

-- webhook_deliveries
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema='public' AND table_name='webhook_deliveries'
          AND column_name='next_retry_at' AND data_type='timestamp without time zone'
    ) THEN
        ALTER TABLE webhook_deliveries ALTER COLUMN next_retry_at
            TYPE timestamp with time zone USING next_retry_at AT TIME ZONE 'UTC';
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema='public' AND table_name='webhook_deliveries'
          AND column_name='leased_at' AND data_type='timestamp without time zone'
    ) THEN
        ALTER TABLE webhook_deliveries ALTER COLUMN leased_at
            TYPE timestamp with time zone USING leased_at AT TIME ZONE 'UTC';
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema='public' AND table_name='webhook_deliveries'
          AND column_name='created_at' AND data_type='timestamp without time zone'
    ) THEN
        ALTER TABLE webhook_deliveries ALTER COLUMN created_at
            TYPE timestamp with time zone USING created_at AT TIME ZONE 'UTC';
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema='public' AND table_name='webhook_deliveries'
          AND column_name='updated_at' AND data_type='timestamp without time zone'
    ) THEN
        ALTER TABLE webhook_deliveries ALTER COLUMN updated_at
            TYPE timestamp with time zone USING updated_at AT TIME ZONE 'UTC';
    END IF;
END $$;

COMMIT;
