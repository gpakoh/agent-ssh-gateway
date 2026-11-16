# Astra site-source named root for mcp-oauth

**Scope:** Gateway Issue #881. Existing Astra website sources, not Astro Builder's immutable publications.

## Authoritative root and environment

- Host path: `/media/1TB/Python/astra/sites`.
- `projects.yaml`: `registry_roots.astro-sites` points to that exact absolute path.
- `astra/docker-compose.yml`: Compose project `astro-sites`; builds Astro from `./sites/zalesskiy-sup` and Lana from `./sites/lana.xloud`. `astra/deploy.sh` validates the reviewed site source.
- `MCP_OAUTH_ASTRO_SITES_ROOT` is a non-secret host environment override. Its default in `docker/docker-compose.yml` is `/media/1TB/Python/astra/sites`. The host path, container path and named registry root must remain identical. Any other value requires a reviewed, coordinated registry change before deploy.

Only the OAuth/admin `mcp-oauth` service receives this dedicated read-write bind, with long-form Compose `bind.create_host_path: false` (YAML key `create_host_path: false`). Neither `mcp-server` (bearer-only) nor Gateway receives this additional bind. A `rw` bind **does not** override POSIX ownership.

## Preflight, fail closed

The host source must already exist, on the Docker daemon's host. If it does not, stop deployment. Never make a new empty `/media/1TB/Python/astro-sites` root as a fallback.

Read-only checks:

```sh
test -d /media/1TB/Python/astra/sites
stat -c '%u:%g %a %n' /media/1TB/Python/astra/sites \
  /media/1TB/Python/astra/sites/lana.xloud \
  /media/1TB/Python/astra/sites/zalesskiy-sup
```

Host status observed 2026-11-16: `astra/sites` and `lana.xloud` are `root:root 755`; `zalesskiy-sup` and `xloud` are UID 1000-owned. The `mcp-oauth` image runs as `appuser` UID 1000. It cannot create a new sibling at the root or change root-owned Lana merely because the mount is `rw`. Do not recursively `chown`, `chmod`, or change permissions on unrelated sites. New site creation requires a separately reviewed, narrow writable subdirectory or owner-approved ACL.

## Deploy acceptance

1. Verify protected Gitea commit, tests, and image provenance. Use `docker compose config -q` for syntax; never print the fully expanded environment or secrets into logs.
2. Check bind identity `source=target=/media/1TB/Python/astra/sites` and `RW=true`, and prove no host path was auto-created.
3. After deployment, independently read `mcp-oauth` Docker mount metadata and live `BUILD_SHA`; ensure the named root points to existing Astra site sources. Confirm no new site bind on the bearer-only `mcp-server` or Gateway.
4. Require the usual Gateway host-smoke: matching SSH/OAuth/MCP build IDs, key-only SSH, readiness and success. This change does not deploy an Astra site or modify Directus.

## Rollback

If mount acceptance fails, preserve the previous verified Gateway image and use the reviewed source revert/LKG path. Never modify Astra's site trees, database, volumes or checkout to make the Gateway deploy pass.
