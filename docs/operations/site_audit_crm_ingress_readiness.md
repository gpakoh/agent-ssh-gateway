# Site Audit CRM ingress — read-only Gateway readiness

The MCP tool `docker_site_audit_crm_ingress_readiness` is a **diagnostic**.
It never launches, removes, restarts, or alters Docker containers, never issues
a confirmation token, and never reports `GO` or an accepted deployment.

It checks a fixed set of six existing Site Audit/CRM dependencies:

- `site-audit-db`, `site-audit-egress-proxy`, `site-audit-pdf`
- `site-audit-crm-db`, `site-audit-crm-web`, `site-audit-crm-daemon`

and five Site Audit targets (`api`, `crawler`, `browser`, `security`,
`performance`). Each is inspected twice. The collector compares container ID,
image ID, StartedAt, Compose config hash, running/health state, Compose project
identity, and host-port publication evidence from HostConfig.PortBindings, NetworkSettings.Ports and PublishAllPorts. Host publication is disallowed for the six dependencies; an existing audit API control port is not a CRM ingress bypass. It also checks twice whether
`site-audit-crm-ingress` already exists. The tool accepts no paths, container
names, image references, networks, or privileged configuration from callers.
Raw Docker errors are not returned.

## Interpretation

The result always has `status: no_go` and `authorizes_mutation: false`,
even if both snapshots match. A matching snapshot proves only that the observed
container state was stable during those two reads. It does **not** prove there
was no race after sampling or that the source, proxy route, host port, image,
WAN/DNAT/IPv6 exposure, or deployment state is safe.

The separate pure function `evaluate_ingress_admission` checks the shape and
consistency of a candidate evidence object. It is not an authorization engine:
**the caller-supplied fields have no trust provenance** and are never accepted
by a deployment operation. In particular, setting `wan_ipv4_port_forward_absent_verified: true` or
`wan_ipv6_nonexposure_verified: true` in arbitrary JSON is not an
independent router or host-network verification.

## Requirements for an actual ingress-only deploy operation

An eventual guarded deployment must obtain independent, authenticated evidence
from the Site Audit operator checkout and its versioned
`scripts/check_crm_ingress_preflight.py`:

1. The exact approved Gitea commit and Git blobs for Compose, Nginx config, and
   preflight must match a clean operator source checkout.
2. The resolved Nginx image must be approved, digest-pinned, locally available
   and bound to a verified image ID.
3. Any legacy CRM Compose hash or network drift must have an explicit reviewed
   compatibility attestation. Do not recreate DB/Web/Daemon to make hashes match.
4. The operator-approved topology has two **private** entry paths:
   an authenticated TLS/Authelia terminator reaches a dedicated internal
   service host, and the operator's private LAN may connect directly using
   EspoCRM's own login. It does not permit direct WAN entry. Exact interface
   addresses, private CIDRs and proxy identity belong to the **operator-owned
   deployment attestation**, not a public repository. Validate both observed
   endpoints against independently approved addresses within the approved
   narrow service CIDR, and separately validate the authorized LAN CIDR.
   Require absence of WAN IPv4 router/NAT forwarding and public IPv6 ingress.
   Do not require a proxy-only host allowlist when private-LAN access is
   explicitly approved. TCP reachability and proxy trust are separate:
   only the specifically approved TLS terminator may supply forwarded identity;
   do not trust the entire service or home LAN subnet.
5. The existing protected and target snapshots must remain unchanged under a
   single exclusive operation lease, including revalidation at confirmation.
6. Only the fixed `crm-ingress` service may be created, with `--no-deps` and
   `--no-build`; no other Compose resource may be changed.
7. Health at `/healthz` is not sufficient. The operation must additionally
   prove the upstream `crm-web:80` route, trusted forwarded-header handling,
   bounded failure recovery, and a durable ingress-only receipt.
8. The historical Site Audit LKG is separate and must not be fabricated or
   manually overwritten. Its accepted status requires a dedicated recovery
   contract after genuine live evidence exists.

Until all these properties are implemented and independently verified, use
the read-only tool to investigate blockers and leave CRM ingress unstarted.