from __future__ import annotations

from app.command_policy import DestructivePattern, PatternSuggestion, Severity
from app.packs import Pack

DATABASE_PATTERNS: tuple[DestructivePattern, ...] = (
    # ---- PostgreSQL (psql) ----
    DestructivePattern(
        name="psql-drop-database",
        regex=r"(?i)\bDROP\s+DATABASE\b",
        reason="DROP DATABASE permanently deletes the entire database",
        severity=Severity.CRITICAL,
        description="All schemas, tables, indexes, and data lost.",
        suggestions=(
            PatternSuggestion(command="pg_dump {db} > {db}.sql", description="Dump database before dropping"),
            PatternSuggestion(command="CREATE DATABASE {name} WITH TEMPLATE {old_db}", description="Copy database as backup first"),
        ),
    ),
    DestructivePattern(
        name="psql-drop-table",
        regex=r"(?i)\bDROP\s+TABLE\b",
        reason="DROP TABLE permanently deletes the table and its data",
        severity=Severity.HIGH,
        description="Table definition and all rows lost. Related views/indexes affected.",
        suggestions=(
            PatternSuggestion(command="DROP TABLE IF EXISTS {table}", description="Avoid error if table is missing"),
            PatternSuggestion(command="DELETE FROM {table} WHERE {condition}", description="Selective deletion instead of full drop"),
        ),
    ),
    DestructivePattern(
        name="psql-drop-schema",
        regex=r"(?i)\bDROP\s+SCHEMA\b",
        reason="DROP SCHEMA deletes the schema and all objects within it",
        severity=Severity.CRITICAL,
        description="All tables, views, functions in the schema lost.",
        suggestions=(
            PatternSuggestion(command="pg_dump --schema={schema} {db} > schema.sql", description="Dump schema first"),
            PatternSuggestion(command="DROP SCHEMA IF EXISTS {schema} CASCADE", description="Use IF EXISTS for safety"),
        ),
    ),
    DestructivePattern(
        name="psql-truncate-table",
        regex=r"(?i)\bTRUNCATE\s+(?:TABLE\s+)?(?!TABLE\b)[a-zA-Z_][a-zA-Z0-9_]*",
        reason="TRUNCATE removes ALL rows from the table irreversibly",
        severity=Severity.HIGH,
        description="All rows deleted. Cannot roll back in many contexts.",
        suggestions=(
            PatternSuggestion(command="SELECT COUNT(*) FROM {table}", description="Check row count before truncating"),
            PatternSuggestion(command="BEGIN; DELETE FROM {table}; ROLLBACK;", description="Wrap in transaction — can roll back"),
        ),
    ),
    DestructivePattern(
        name="psql-delete-without-where",
        regex=r"(?i)DELETE\s+FROM\s+(?:(?:[a-zA-Z_][a-zA-Z0-9_]*|\"[^\"]+\")(?:\.(?:[a-zA-Z_][a-zA-Z0-9_]*|\"[^\"]+\"))?)\s*(?:;|$)",
        reason="DELETE without WHERE clause removes ALL rows",
        severity=Severity.HIGH,
        description="All rows deleted if WHERE clause is missing.",
        suggestions=(
            PatternSuggestion(command="BEGIN; DELETE FROM {table} ... ; ROLLBACK;", description="Wrap in transaction for safety"),
            PatternSuggestion(command="DELETE FROM {table} WHERE {condition}", description="Add a WHERE clause"),
        ),
    ),
    DestructivePattern(
        name="psql-dropdb-cli",
        regex=r"\bdropdb\s+",
        reason="dropdb CLI permanently deletes the entire database",
        severity=Severity.CRITICAL,
        description="The dropdb command-line tool destroys the database cluster-side.",
        suggestions=(
            PatternSuggestion(command="pg_dump {db} > {db}.sql", description="Backup database before dropping"),
            PatternSuggestion(command="createdb {new_db}", description="Create a new database instead of dropping"),
        ),
    ),
    DestructivePattern(
        name="psql-dump-clean",
        regex=r"pg_dump\s+.*(?:--clean|-c\b)",
        reason="pg_dump --clean adds DROP statements to the dump script",
        severity=Severity.HIGH,
        description="Restoring the dump will DROP existing objects before recreating them.",
        suggestions=(
            PatternSuggestion(command="pg_dump {db} > {db}.sql", description="Dump without --clean for safe restore"),
            PatternSuggestion(command="pg_dump --clean --if-exists {db}", description="Use --if-exists to avoid errors"),
        ),
    ),
    # ---- MySQL / MariaDB ----
    DestructivePattern(
        name="mysql-drop-database",
        regex=r"(?i)\bDROP\s+DATABASE\b",
        reason="DROP DATABASE permanently deletes the entire database",
        severity=Severity.CRITICAL,
        description="All tables and data within the database lost.",
        suggestions=(
            PatternSuggestion(command="mysqldump {db} > {db}.sql", description="Dump database before dropping"),
            PatternSuggestion(command="CREATE DATABASE {new_db}", description="Create a new database instead"),
        ),
    ),
    DestructivePattern(
        name="mysql-drop-table",
        regex=r"(?i)\bDROP\s+TABLE\b",
        reason="DROP TABLE permanently deletes the table",
        severity=Severity.HIGH,
        description="Table definition and all rows lost.",
        suggestions=(
            PatternSuggestion(command="mysqldump {db} {table} > {table}.sql", description="Dump table before dropping"),
            PatternSuggestion(command="RENAME TABLE {table} TO {table}_old", description="Rename as a safer alternative"),
        ),
    ),
    DestructivePattern(
        name="mysql-truncate-table",
        regex=r"(?i)\bTRUNCATE\s+(?:TABLE\s+)?(?!TABLE\b)[a-zA-Z_][a-zA-Z0-9_]*",
        reason="TRUNCATE removes ALL rows — cannot roll back in MySQL",
        severity=Severity.HIGH,
        description="InnoDB: all rows removed implicitly. No per-row delete triggers fired.",
        suggestions=(
            PatternSuggestion(command="SELECT COUNT(*) FROM {table}", description="Check row count first"),
            PatternSuggestion(command="DELETE FROM {table} WHERE {condition}", description="Use DELETE with WHERE if possible (InnoDB transaction)"),
        ),
    ),
    DestructivePattern(
        name="mysql-delete-without-where",
        regex=r"(?i)DELETE\s+FROM\s+(?:(?:[a-zA-Z_][a-zA-Z0-9_]*|`[^`]+`)(?:\.(?:[a-zA-Z_][a-zA-Z0-9_]*|`[^`]+`))?)\s*(?:;|$)",
        reason="DELETE without WHERE clause removes ALL rows",
        severity=Severity.HIGH,
        description="All rows deleted if WHERE clause is missing.",
        suggestions=(
            PatternSuggestion(command="BEGIN; DELETE FROM {table} ... ; ROLLBACK;", description="Wrap in transaction for safety"),
            PatternSuggestion(command="SELECT * FROM {table} LIMIT 100", description="Preview rows first"),
            PatternSuggestion(command="DELETE FROM {table} WHERE {condition}", description="Add a WHERE clause"),
        ),
    ),
    DestructivePattern(
        name="mysql-mysqladmin-drop",
        regex=r"mysqladmin\s+.*drop\b",
        reason="mysqladmin drop permanently deletes the entire database",
        severity=Severity.CRITICAL,
        description="mysqladmin drop destroys the database server-side without confirmation.",
        suggestions=(
            PatternSuggestion(command="mysqldump {db} > {db}.sql", description="Dump database first"),
            PatternSuggestion(command="mysqladmin ping", description="Check server status before destructive action"),
        ),
    ),
    DestructivePattern(
        name="mysql-mysqldump-add-drop-database",
        regex=r"mysqldump\s+.*--add-drop-database",
        reason="mysqldump --add-drop-database adds DROP DATABASE to the dump",
        severity=Severity.HIGH,
        description="Restoring the dump will DROP the database first.",
        suggestions=(
            PatternSuggestion(command="mysqldump {db} > {db}.sql", description="Dump without --add-drop-database"),
            PatternSuggestion(command="mysqldump --no-data {db} > schema.sql", description="Dump schema only"),
        ),
    ),
    DestructivePattern(
        name="mysql-mysqldump-add-drop-table",
        regex=r"mysqldump\s+.*--add-drop-table",
        reason="mysqldump --add-drop-table adds DROP TABLE before CREATE TABLE",
        severity=Severity.MEDIUM,
        description="Existing tables will be dropped before restore.",
        suggestions=(
            PatternSuggestion(command="mysqldump {db} > {db}.sql", description="Dump without --add-drop-table"),
            PatternSuggestion(command="mysqldump --no-data {db} > schema.sql", description="Dump schema only"),
        ),
    ),
    DestructivePattern(
        name="mysql-grant-all",
        regex=r"(?i)GRANT\s+ALL\s+(?:PRIVILEGES\s+)?ON\s+\*\.\*",
        reason="GRANT ALL ON *.* gives unrestricted access to all databases",
        severity=Severity.HIGH,
        description="Full administrative access granted across all databases.",
        suggestions=(
            PatternSuggestion(command="GRANT {privileges} ON {db}.* TO {user}", description="Grant only specific privileges on specific DBs"),
            PatternSuggestion(command="SHOW GRANTS FOR {user}", description="Check current grants first"),
        ),
    ),
    DestructivePattern(
        name="mysql-drop-user",
        regex=r"(?i)\bDROP\s+USER\b",
        reason="DROP USER permanently deletes the user account",
        severity=Severity.MEDIUM,
        description="User deleted. All privileges revoked. Existing connections may break.",
        suggestions=(
            PatternSuggestion(command="DROP USER IF EXISTS {user}", description="Avoid error if user does not exist"),
            PatternSuggestion(command="REVOKE ALL PRIVILEGES ... FROM {user}", description="Revoke privileges instead of dropping user"),
        ),
    ),
    DestructivePattern(
        name="mysql-reset-master",
        regex=r"(?i)\bRESET\s+MASTER\b",
        reason="RESET MASTER deletes all binary logs and resets binlog position",
        severity=Severity.CRITICAL,
        description="Breaks replication. All binlog history lost.",
        suggestions=(
            PatternSuggestion(command="SHOW BINARY LOGS", description="Check binary log status before reset"),
            PatternSuggestion(command="PURGE BINARY LOGS BEFORE NOW()", description="Remove old logs safely without full reset"),
        ),
    ),
    # ---- SQLite (sqlite3) ----
    DestructivePattern(
        name="sqlite-drop-table",
        regex=r"(?i)\bDROP\s+TABLE\b",
        reason="DROP TABLE permanently deletes the table and all data",
        severity=Severity.CRITICAL,
        description="Table and all rows deleted from the database file.",
        suggestions=(
            PatternSuggestion(command="DROP TABLE IF EXISTS {table}", description="Avoid error if table is missing"),
            PatternSuggestion(command="sqlite3 {db} '.dump {table}' > {table}.sql", description="Dump table before dropping"),
        ),
    ),
    DestructivePattern(
        name="sqlite-delete-without-where",
        regex=r"(?i)DELETE\s+FROM\s+[a-zA-Z_][a-zA-Z0-9_]*\s*(?:;|$)",
        reason="DELETE without WHERE clause removes ALL rows",
        severity=Severity.CRITICAL,
        description="SQLite does not support TRUNCATE. DELETE without WHERE removes all rows.",
        suggestions=(
            PatternSuggestion(command="BEGIN; DELETE FROM {table} ... ; ROLLBACK;", description="Wrap in transaction for safety"),
            PatternSuggestion(command="SELECT COUNT(*) FROM {table}", description="Check row count first"),
        ),
    ),
    DestructivePattern(
        name="sqlite-vacuum-into",
        regex=r"(?i)VACUUM\s+INTO\s+",
        reason="VACUUM INTO overwrites the target file if it exists",
        severity=Severity.MEDIUM,
        description="Target file is overwritten without warning.",
        suggestions=(
            PatternSuggestion(command="VACUUM", description="Vacuum in-place without overwriting another file"),
            PatternSuggestion(command="PRAGMA page_count", description="Check database size first"),
        ),
    ),
    DestructivePattern(
        name="sqlite-sqlite3-file-input",
        regex=r"sqlite3\s+[^\s]+\s+<\s+",
        reason="SQL loaded from file may contain destructive commands",
        severity=Severity.HIGH,
        description="Read SQL from file command — file contents not inspected by guard.",
        suggestions=(
            PatternSuggestion(command="sqlite3 {db} '.read {file}'", description="Use dot-command to read file in-process"),
            PatternSuggestion(command="cat {file} | sqlite3 {db}", description="Pipe file contents for visibility"),
        ),
    ),
    # ---- MongoDB (mongosh) ----
    DestructivePattern(
        name="mongodb-drop-database",
        regex=r"\.dropDatabase\s*\(",
        reason="dropDatabase() permanently deletes the entire database",
        severity=Severity.CRITICAL,
        description="All collections, indexes, and data lost.",
        suggestions=(
            PatternSuggestion(command="db.adminCommand('listDatabases')", description="List databases first"),
            PatternSuggestion(command="mongodump --db {db}", description="Backup before dropping"),
        ),
    ),
    DestructivePattern(
        name="mongodb-drop-collection",
        regex=r"\.drop\s*\(\s*\)|\.dropCollection\s*\(",
        reason="drop()/dropCollection() permanently deletes the collection",
        severity=Severity.HIGH,
        description="All documents and indexes in the collection lost.",
        suggestions=(
            PatternSuggestion(command="db.{coll}.find().limit(10)", description="Preview documents first"),
            PatternSuggestion(command="mongodump --collection {coll} --db {db}", description="Backup collection first"),
        ),
    ),
    DestructivePattern(
        name="mongodb-delete-all",
        regex=r"\.(?:remove|deleteMany)\s*\(\s*\{\s*\}\s*\)",
        reason="remove({})/deleteMany({}) removes ALL documents",
        severity=Severity.HIGH,
        description="All documents in the collection deleted.",
        suggestions=(
            PatternSuggestion(command="db.{coll}.find().limit(100)", description="Preview documents first"),
            PatternSuggestion(command="db.{coll}.deleteMany({filter})", description="Use a filter for selective deletion"),
        ),
    ),
    DestructivePattern(
        name="mongodb-mongorestore-drop",
        regex=r"mongorestore\s+.*--drop",
        reason="mongorestore --drop drops existing collections before restoring",
        severity=Severity.HIGH,
        description="Existing collections are dropped before data restoration.",
        suggestions=(
            PatternSuggestion(command="mongodump --db {db} --out /tmp/backup", description="Backup before restoring"),
            PatternSuggestion(command="mongorestore --nsInclude {db}.*", description="Restore specific namespaces only"),
        ),
    ),
    # ---- Redis (redis-cli) ----
    DestructivePattern(
        name="redis-flushall",
        regex=r"(?i)\bFLUSHALL\b",
        reason="FLUSHALL deletes ALL keys in ALL databases",
        severity=Severity.CRITICAL,
        description="Every key in every database is deleted immediately.",
        suggestions=(
            PatternSuggestion(command="redis-cli --scan --pattern '*' | head -20", description="Preview keys in all databases first"),
            PatternSuggestion(command="redis-cli DBSIZE", description="Check number of keys before flushing"),
        ),
    ),
    DestructivePattern(
        name="redis-flushdb",
        regex=r"(?i)\bFLUSHDB\b",
        reason="FLUSHDB deletes ALL keys in the current database",
        severity=Severity.HIGH,
        description="All keys in the selected database deleted.",
        suggestions=(
            PatternSuggestion(command="redis-cli --scan --pattern '*' | head -20", description="Preview keys first"),
            PatternSuggestion(command="redis-cli DBSIZE", description="Check key count before flushing"),
        ),
    ),
    DestructivePattern(
        name="redis-mass-delete-pipeline",
        regex=r"(?i)\bredis-cli\b.*\b(?:KEYS\b|--scan\b|SCAN\b).*\|\s*xargs\s+(?:-\S+(?:\s+\S+)?\s+)*redis-cli(?:\s+\S+)*\s+(?:DEL|UNLINK)\b",
        reason="KEYS/SCAN piped through xargs to DEL/UNLINK mass-deletes many keys",
        severity=Severity.HIGH,
        description="Mass key deletion via pipe. Can affect many keys at once.",
        suggestions=(
            PatternSuggestion(command="redis-cli --bigkeys", description="Check key distribution first"),
            PatternSuggestion(command="redis-cli --scan --pattern 'prefix:*'", description="Use a specific prefix for targeted deletion"),
        ),
    ),
    DestructivePattern(
        name="redis-debug-crash",
        regex=r"(?i)\bDEBUG\s+(?:SEGFAULT|CRASH)\b",
        reason="DEBUG SEGFAULT/CRASH crashes the Redis server",
        severity=Severity.CRITICAL,
        description="Redis server process crashes. Data loss may occur.",
        suggestions=(
            PatternSuggestion(command="redis-cli PING", description="Check server health instead"),
            PatternSuggestion(command="redis-cli INFO server", description="Check server info instead"),
        ),
    ),
    DestructivePattern(
        name="redis-debug-sleep",
        regex=r"(?i)\bDEBUG\s+SLEEP\b",
        reason="DEBUG SLEEP blocks the Redis server",
        severity=Severity.HIGH,
        description="Redis blocked for N seconds. All clients time out.",
        suggestions=(
            PatternSuggestion(command="redis-cli PING", description="Check server health instead"),
            PatternSuggestion(command="redis-cli LATENCY LATEST", description="Check latency first"),
        ),
    ),
    DestructivePattern(
        name="redis-shutdown",
        regex=r"(?i)\bSHUTDOWN\b",
        reason="SHUTDOWN stops the Redis server (SHUTDOWN NOSAVE loses data)",
        severity=Severity.HIGH,
        description="Redis server shut down gracefully (or with NOSAVE, losing data).",
        suggestions=(
            PatternSuggestion(command="redis-cli SAVE", description="Save RDB before shutdown"),
            PatternSuggestion(command="redis-cli BGSAVE", description="Background save before shutdown"),
        ),
    ),
    DestructivePattern(
        name="redis-config-dangerous",
        regex=r"(?i)\bCONFIG\s+SET\s+(?:dir|dbfilename|slaveof|replicaof)\b",
        reason="CONFIG SET dir/dbfilename/slaveof can enable RCE or data exfiltration",
        severity=Severity.CRITICAL,
        description="Changing dir+dbfilename writes key data outside data dir. "
        "slaveof/replicaof can leak keys to attacker.",
        suggestions=(
            PatternSuggestion(command="redis-cli CONFIG GET dir", description="Check current directory first"),
            PatternSuggestion(command="redis-cli CONFIG GET dbfilename", description="Check current filename first"),
        ),
    ),
    DestructivePattern(
        name="redis-config-set-maxmemory",
        regex=r"(?i)\bCONFIG\s+SET\s+maxmemory\b(?:\s|$)",
        reason="CONFIG SET maxmemory can trigger mass key eviction",
        severity=Severity.CRITICAL,
        description="Setting maxmemory too low causes Redis to evict keys aggressively.",
        suggestions=(
            PatternSuggestion(command="redis-cli INFO memory", description="Check current memory usage first"),
            PatternSuggestion(command="redis-cli MEMORY STATS", description="Detailed memory analysis"),
        ),
    ),
    DestructivePattern(
        name="redis-config-set-maxmemory-policy",
        regex=r"(?i)\bCONFIG\s+SET\s+maxmemory-policy\b",
        reason="CONFIG SET maxmemory-policy changes eviction policy — risk of data loss",
        severity=Severity.CRITICAL,
        description="Changing to allkeys-lru or volatile-ttl can evict any key.",
        suggestions=(
            PatternSuggestion(command="redis-cli CONFIG GET maxmemory-policy", description="Check current policy first"),
            PatternSuggestion(command="redis-cli INFO stats", description="Check eviction stats first"),
        ),
    ),
    DestructivePattern(
        name="redis-config-set-save",
        regex=r"(?i)\bCONFIG\s+SET\s+save\b",
        reason="CONFIG SET save can disable RDB persistence entirely",
        severity=Severity.HIGH,
        description="Setting save to empty disables snapshots. Data lost on restart.",
        suggestions=(
            PatternSuggestion(command="redis-cli CONFIG GET save", description="Check current save config first"),
            PatternSuggestion(command="redis-cli LASTSAVE", description="Check last save timestamp"),
        ),
    ),
    DestructivePattern(
        name="redis-config-set-appendonly",
        regex=r"(?i)\bCONFIG\s+SET\s+appendonly\b",
        reason="CONFIG SET appendonly can disable AOF persistence",
        severity=Severity.HIGH,
        description="Disabling AOF removes append-only log. Data may be lost on restart.",
        suggestions=(
            PatternSuggestion(command="redis-cli CONFIG GET appendonly", description="Check current AOF status first"),
            PatternSuggestion(command="redis-cli INFO persistence", description="Check persistence status"),
        ),
    ),
    DestructivePattern(
        name="redis-config-rewrite",
        regex=r"(?i)\bCONFIG\s+REWRITE\b",
        reason="CONFIG REWRITE saves runtime changes to redis.conf permanently",
        severity=Severity.HIGH,
        description="Runtime CONFIG SET changes are persisted to disk.",
        suggestions=(
            PatternSuggestion(command="redis-cli CONFIG GET *", description="Review current config before persisting"),
            PatternSuggestion(command="redis-cli INFO server", description="Check server info before rewriting config"),
        ),
    ),
)


def build_database_pack() -> Pack:
    return Pack(id="database", name="Database destructive patterns",
        destructive_patterns=DATABASE_PATTERNS,
        keywords=("psql", "mysql", "sqlite3", "mongosh", "redis-cli",
                  "drop", "truncate", "delete", "flushall", "flushdb",
                  "config", "debug", "shutdown", "reset", "dropdb",
                  "mysqldump", "mysqladmin", "grant", "vacuum", "mongorestore",
                  "pipeline", "slaveof", "replicaof", "dbfilename",
                  "maxmemory", "appendonly", "pg_dump", "remove", "db."),
    )
