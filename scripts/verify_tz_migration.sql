-- Post-migration verification script
-- Run AFTER: alembic upgrade head
-- Expected: all 9 columns show "timestamp with time zone"

-- 1. Verify all timestamp columns are now tz-aware
SELECT
    table_name,
    column_name,
    data_type,
    CASE
        WHEN data_type = 'timestamp with time zone' THEN 'OK'
        ELSE 'STILL NAIVE — MIGRATION INCOMPLETE'
    END AS status
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name IN ('ssh_sessions', 'event_hooks', 'webhook_deliveries')
  AND column_name LIKE '%at%'
ORDER BY table_name, ordinal_position;

-- 2. Check for any remaining timestamp without time zone columns
SELECT COUNT(*) AS naive_timestamp_columns_remaining
FROM information_schema.columns
WHERE table_schema = 'public'
  AND data_type = 'timestamp without time zone'
  AND table_name IN ('ssh_sessions', 'event_hooks', 'webhook_deliveries');

-- 3. Verify alembic_version table exists and shows correct revision
SELECT version_num FROM alembic_version;

-- 4. Sample data check: ensure existing values are preserved as UTC
SELECT
    'ssh_sessions' AS tbl,
    COUNT(*) AS total_rows,
    MIN(connected_at) AS oldest_connected,
    MAX(expires_at) AS latest_expires
FROM ssh_sessions
UNION ALL
SELECT
    'event_hooks' AS tbl,
    COUNT(*) AS total_rows,
    MIN(created_at) AS oldest_created,
    MAX(updated_at) AS latest_updated
FROM event_hooks
UNION ALL
SELECT
    'webhook_deliveries' AS tbl,
    COUNT(*) AS total_rows,
    MIN(created_at) AS oldest_created,
    MAX(updated_at) AS latest_updated
FROM webhook_deliveries;
