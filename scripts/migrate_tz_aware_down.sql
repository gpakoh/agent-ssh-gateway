-- Downgrade: timestamp with time zone → timestamp without time zone
-- File: scripts/migrate_tz_aware_down.sql
--
-- Strips timezone info. Existing UTC values are preserved as naive.
--
-- Usage:
--   docker exec -i mcp-postgres psql -U postgres -d gateway < scripts/migrate_tz_aware_down.sql

BEGIN;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema='public' AND table_name='ssh_sessions'
          AND column_name='connected_at' AND data_type='timestamp with time zone'
    ) THEN
        ALTER TABLE ssh_sessions ALTER COLUMN connected_at
            TYPE timestamp without time zone USING connected_at AT TIME ZONE 'UTC';
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema='public' AND table_name='ssh_sessions'
          AND column_name='last_activity' AND data_type='timestamp with time zone'
    ) THEN
        ALTER TABLE ssh_sessions ALTER COLUMN last_activity
            TYPE timestamp without time zone USING last_activity AT TIME ZONE 'UTC';
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema='public' AND table_name='ssh_sessions'
          AND column_name='expires_at' AND data_type='timestamp with time zone'
    ) THEN
        ALTER TABLE ssh_sessions ALTER COLUMN expires_at
            TYPE timestamp without time zone USING expires_at AT TIME ZONE 'UTC';
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema='public' AND table_name='event_hooks'
          AND column_name='created_at' AND data_type='timestamp with time zone'
    ) THEN
        ALTER TABLE event_hooks ALTER COLUMN created_at
            TYPE timestamp without time zone USING created_at AT TIME ZONE 'UTC';
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema='public' AND table_name='event_hooks'
          AND column_name='updated_at' AND data_type='timestamp with time zone'
    ) THEN
        ALTER TABLE event_hooks ALTER COLUMN updated_at
            TYPE timestamp without time zone USING updated_at AT TIME ZONE 'UTC';
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema='public' AND table_name='webhook_deliveries'
          AND column_name='next_retry_at' AND data_type='timestamp with time zone'
    ) THEN
        ALTER TABLE webhook_deliveries ALTER COLUMN next_retry_at
            TYPE timestamp without time zone USING next_retry_at AT TIME ZONE 'UTC';
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema='public' AND table_name='webhook_deliveries'
          AND column_name='leased_at' AND data_type='timestamp with time zone'
    ) THEN
        ALTER TABLE webhook_deliveries ALTER COLUMN leased_at
            TYPE timestamp without time zone USING leased_at AT TIME ZONE 'UTC';
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema='public' AND table_name='webhook_deliveries'
          AND column_name='created_at' AND data_type='timestamp with time zone'
    ) THEN
        ALTER TABLE webhook_deliveries ALTER COLUMN created_at
            TYPE timestamp without time zone USING created_at AT TIME ZONE 'UTC';
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema='public' AND table_name='webhook_deliveries'
          AND column_name='updated_at' AND data_type='timestamp with time zone'
    ) THEN
        ALTER TABLE webhook_deliveries ALTER COLUMN updated_at
            TYPE timestamp without time zone USING updated_at AT TIME ZONE 'UTC';
    END IF;
END $$;

COMMIT;
