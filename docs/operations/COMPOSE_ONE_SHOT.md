# Operator-controlled Compose one-shot contracts (P1-05 / #879)

The `docker_compose_one_shot` MCP tool is **disabled by default**. It does
not accept a project directory, container image, Docker options, environment
values, SQL, or command argv from its caller. Activation requires an approved
operator-controlled `MCP_COMPOSE_ONE_SHOT_CONTRACTS` JSON mapping in the
Gateway process environment. Enabling it is an operator change, not an
automatic consequence of a repository PR.

## Authorization and contract format

A caller specifies only `contract`, `mode` (`readiness`, `migration`,
`catalog_schemas` or `catalog_tables`) and a bounded timeout. The tool
first validates the operator mapping, resolves `project_id` through the
registered workspace catalog, verifies the allowed project directory, and
checks a **complete interpolated Compose configuration** for exact project,
service, digest-pinned image, internal physical network, inherited environment
**names** and approved named volume sources/targets. Unapproved bind mounts,
published ports, extra environment names, arbitrary Compose entrypoints,
privilege escalation, nested secrets/configs and service builds are denied.

A minimal example of the configuration schema is shown below. It is
illustrative, not an authorization to deploy or an existing production policy:

```json
{
  "version": 1,
  "contracts": {
    "approved-postgres": {
      "project_id": "approved-workspace-id",
      "compose_project": "approved-compose-project",
      "service": "postgres",
      "image": "registry.example/approved/postgres@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "network": "internal",
      "network_name": "approved-compose-project_internal",
      "env_names": ["PGUSER", "PGPASSWORD", "PGDATABASE"],
      "volume_sources": {"/var/lib/postgresql/data": "pgdata"},
      "operations": {
        "readiness": ["pg_isready", "--timeout=3"],
        "migration": ["alembic", "upgrade", "head"],
        "catalog_schemas": "postgres_catalog",
        "catalog_tables": "postgres_catalog"
      }
    }
  }
}
```

`image` must be an immutable local digest; actual image ID is attested
independently. The real operator catalog must use reviewed commands and
verified network/volume identities, not these examples. The operator must
ensure the contract environment is not writable by tool callers.

## Confirmation and execution

Preflight creates an **`mcp:docker:admin`, high-risk, one-use** confirmation.
Only a process-keyed HMAC of the interpolated config, immutable image ID and
expected registered root are retained as the identity lease. Secret values
are never serialized in confirmation kwargs, audit metadata, tool responses
or command-line arguments. On confirmation, the entire preflight is repeated;
any change fails closed **before** running a container.

Execution uses `docker compose run --rm --no-deps -T` with a fixed
operator-approved service and argv. It does not publish service ports.
Compose inherits the service's already-reviewed environment names and
volumes. The tool never returns migration/readiness stdout or stderr.
A one-shot timeout, failed transport, or ambiguous Docker status is
non-retryable; even a nonzero exit code is **not rollback evidence**.
An operator must reconcile the actual migration effects and target service
state before approving any subsequent attempt.

## PostgreSQL catalog inspection

Catalog modes substitute fixed `psql` argv and a transaction explicitly
marked `READ ONLY`. The SQL uses `pg_catalog` only and a hard query
`LIMIT 100`; no caller SQL is accepted. Output is parsed as bounded
identifiers. Unknown, oversized, or malformed data is rejected without
echoing raw output. Catalog reads are not a substitute for audited database
role permissions or backups.

## Boundaries and acceptance

The new tool intentionally does not provision root-only secrets (see #880),
modify Compose service definitions, expose arbitrary environment values,
grant raw Docker shell access, or certify that a timed-out database migration
was rolled back. No privileged production one-shot run is part of the
standard CI. Before activating in a real environment, the operator must
validate the actual Compose config and expected network, image, volume and
SQL role, then qualify the workflow using an isolated staging service.

PR and post-merge CI prove code quality and Gateway deployment; they do not
prove migrations against an unconfigured production service. Any remaining
staging prerequisites must be recorded in Gitea Issue #879 rather than
treated as implicit success.
