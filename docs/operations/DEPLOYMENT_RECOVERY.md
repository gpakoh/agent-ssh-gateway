# Governed deployment recovery

Recovering an incomplete deploy is a **separate workflow** from deploying the
next revision. Never retry a rollout merely because the previous attempt
timed out: its source image, durable state, and live topology may have changed.

## Two tools, not one tool per website

- `docker_recovery_catalog()`: read-only discovery. Returns registered recovery
  contract IDs, expected parameter names, scope, and confirmation requirement.
- `docker_recover_deployment(contract, parameters, timeout=300)`: prepares
  the chosen contract using **only** an allowlisted, reviewed handler. The
  response contains a high-risk confirmation action. The user must confirm it
  through the existing `confirm_operation` workflow. The confirmation is
  single-use and short-lived.

The public API stays unchanged when more projects add recovery contracts.
Registry entries specify a fixed contract ID, typed parameter names, a
server-controlled preparation function, a server-controlled executor, and
the required scope. A user cannot provide an executable, shell command,
container name, host path, or script path through this interface.

### First supported contract: `xloud`

The adapter expects these parameters:

```json
{
  "contract": "xloud",
  "parameters": {
    "candidate_project": "candidate-xloud-<verified-release>",
    "expected_head_sha": "<40-lowercase-hex>",
    "expected_script_blob_sha": "<40-lowercase-hex>",
    "expected_script_sha256": "<64-lowercase-hex>",
    "source_revision": "<same-40-lowercase-hex-as-head>",
    "image_namespace": "<trusted-registry-namespace>"
  }
}
```

Pass values taken from the exact verified release candidate. The gateway
verifies source ownership, branch/tree cleanliness, script blob/bytes, pinned
two-image revisions, state volume, operator secret fingerprint, fixed network
metadata, Site Audit API identity, and the healthy legacy site.

**Prepare is non-mutating.** Its `inspect` helper mounts the deployment
journal volume read-only and never executes a deploy shell script. The old
site must be healthy and still use the exact historical image reference; the
failed-generation intake must match the originally pinned image and networks.
The legacy-only contract refuses a mixed accepted generation, a missing
pending journal, a prior intake, or foreign sidecars.

Before confirming, the gateway re-reads the journal and repeats identity
checks. Only then may the fixed repository-owned `deploy-xloud.sh recover`
execute. Success requires a cleared pending journal, preserved
`last-failed.state`, removed failed-generation intake, and stable/healthy
legacy site after a second observation. On timeout or ambiguous evidence,
**do not retry automatically**; inspect and reconcile the durable evidence.
Neither operation deploys a new version or claims that the application is
already released.

### Adding the next site

Add an entry to `_DEPLOYMENT_RECOVERY_CONTRACTS` and two reviewed handlers to
`_recovery_contract_handlers()` in the Docker adapter. Bind the handler to
its own canonical project, immutable script and images, volumes, service
identities, and expected recovery evidence. Add negative tests for foreign
projects, changed confirmation pins, missing pending state, and uncertain
outcomes. Do not widen the existing XLOUD adapter to accept arbitrary paths or
services; that would turn a recovery operation into unrestricted remote
execution.

## Non-goals and deployment caveat

Merging the Gateway PR does **not** automatically activate the tool in an
already running Gateway container: its own verified release and MCP schema
reload are separate. XLOUD `PR #34` (Nginx config / rollback fixes) and a
later XLOUD image rollout remain independent. No production data or
container is modified by this code change.