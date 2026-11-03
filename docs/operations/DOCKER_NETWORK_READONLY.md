# Read-only Docker network evidence (Gateway #650)

The MCP tools `docker_network_ls(name=None, limit=50)` and
`docker_network_inspect(name)` query **only the Gateway-configured Docker
endpoint** with a fixed read-only CLI argv. They require `mcp:docker`, not
`mcp:docker:admin`. Neither tool removes, disconnects, prunes, reconfigures,
or creates Docker resources.

## Evidence and completeness

- An exact `name` searches only that network name. Names are validated
  ASCII Docker identifiers (no slashes, shell metacharacters or arbitrary
  regex); the internally constructed Docker name filter is anchored and
  regex-escaped. Responses use only **full 64-hex IDs**, never shortened
  display IDs. A malformed or mismatched response returns an error.
- `docker_network_ls` returns `networks`, `count`, `query_name`,
  `query_status` and `inventory`. `query_status=absent` is possible
  only after a complete, well-formed exact-name query returns no networks.
  An unfiltered or truncated inventory reports `unknown` instead.
  `limit` is 1..100; the result is not a host-wide inventory.
- `docker_network_inspect` returns `status=present` with exact network
  ID/name/driver/scope, `internal`, and full IDs/count of attached
  containers (`attachments_complete=true`), or `status=absent` only
  for an exact Docker `No such network: <name>` response. Other daemon,
  timeout, malformed data, identity drift, or unexpectedly large/invalid
  attachment inventories fail rather than silently claiming an empty network.
- Output **does not contain** network IPs, MACs, subnet/gateway addresses,
  container names, endpoints, driver options, secrets, or host paths.
  The only exposed network label values are the bounded, safe
  `com.docker.compose.project` and `com.docker.compose.network` values;
  other values are redacted. Count semantics are for the configured daemon.
- `inventory.endpoint.identity` hashes the configured endpoint selector,
  with `host_completeness=unknown`; this cannot establish the absence
  of networks on *other* Docker daemons. A result is not an authorization
  for Docker cleanup.

## Site Audit #189 acceptance

Query each historical exact name separately, for example:
`docker_network_inspect(name="security-smoke-jobs-047b4a7ebfff")`.
Record the full network ID, `inventory.endpoint.identity`, status,
`attached_container_ids`, `attachments_complete`, and any validated
Compose labels. Use the corresponding exact-name list lookup to corroborate.
An `absent` conclusion is scoped to that endpoint only. If the configured
endpoint differs from the original CI daemon, or inspection returns an
error/unknown, record the unresolved network rather than attempting a prune.

No production mutation is performed by these tools.
