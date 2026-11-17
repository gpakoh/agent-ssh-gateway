# Bounded service probes (P3-02, scoped delivery)

The MCP tools `service_http_probe` and `service_tcp_probe` observe **only**
two fixed peers in the MCP server's Docker Compose service network:

| Target | HTTP GET | TCP endpoint |
| --- | --- | --- |
| `gateway` | `web-ssh-gateway:8085/health` | `web-ssh-gateway:8085` |
| `mcp` | `mcp-server:8087/healthz` | `mcp-server:8087` |

The caller chooses `target=gateway|mcp`, with an explicit 200–5000 ms
timeout. HTTP may sample 0–4096 *raw wire* response bytes to detect
truncation; with a zero-byte budget, truncation is explicitly unknown
(`response_truncated=null`). It **never returns the body or its hash**. Requests are GET-only,
without credentials/cookies or ambient proxy configuration. Redirects are
not followed and the `Location` header is never returned. TCP connect
sends no application bytes and closes the socket after observation.

Results include a fixed endpoint profile, `network_namespace=mcp_container`,
`host_port_owner_known=false`, time of observation, status/connection and
typed `failure_class`. Failed DNS lookup, refused connection, timeout or
other transport failure is returned with a bounded, redacted error; no raw
exception, address from the resolver, request/response header, URL from a
redirect or process command line is returned. An HTTP non-2xx status is an
**observed, unsuccessful application response**, not a successful health
claim. TCP connectivity alone leaves `application_health=unknown`.

These tools are read-only under `mcp:read`, with no shell/SSH, Docker socket,
host PID namespace, `/proc`, file read or container mutation capability.
The fixed profiles live in the version-controlled source and may only change
through a reviewed deployment. This is deliberately **not** a generic
HTTP-client/SSRF oracle and **not** the host-port-owner observer of #742.
Unknown target, invalid integer/bool/timeout and oversized body budget are
rejected before any network operation.

P3-02 remains open for a separately reviewed allowlisted public-external
target contract and broader probes. Do not promote an MCP-network success
to a host-level or public ingress claim. The probes are not a substitute for
authenticated release smoke, post-deploy CI and rollback qualification.
