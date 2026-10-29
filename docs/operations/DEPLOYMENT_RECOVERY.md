# Governed deployment recovery

Deployment recovery is a separate operation from deploying a new revision.
After a deployment fails or times out, the previous application's image, its
durable journal, and the running containers may no longer match. Retrying
deployment or deleting the journal is **not** a safe recovery strategy.

This feature provides a common MCP interface for registered, operator-reviewed
recovery procedures. The gateway is an infrastructure control plane; it does
not automatically know how to roll back every application that uses Docker.

## Common tools

| Tool | What it does | Privilege |
| --- | --- | --- |
| `docker_recovery_catalog` | Lists supported contract IDs, expected parameters, and confirmation policy. Never modifies Docker. | `mcp:docker` |
| `docker_recover_deployment` | Validates one allowlisted contract against the current state and creates a high-risk confirmation action. | `mcp:docker:admin` |
| `confirm_operation` | After explicit approval, rechecks state and executes the contract's pinned recovery routine. | Existing privileged confirmation policy |

These tools are independent of application branding. Clients should call the
catalog before choosing a contract; they must not assume a particular contract
is installed. The returned contract IDs describe **specific registered
recovery procedures**, not arbitrary Docker image names or shell scripts.

## Example: recover a previous version of a two-service website

The bundled compatibility adapter currently exposes the contract
`static-site-legacy-v1`. It is deliberately restricted to **one
preconfigured deployment**, consisting of a web container and a companion
intake container. It is *not* a generic rollback command for other sites.

The catalog describes six required parameters; its entry indicates that
operator configuration is required. A listed contract does not prove that a
matching deployment or recovery journal exists in a fresh installation.
The following example illustrates the parameters' shape; values are **placeholders**, not valid live release identifiers:

```json
{
  "contract": "static-site-legacy-v1",
  "parameters": {
    "candidate_project": "<registered-release-candidate>",
    "expected_head_sha": "<git-commit-sha40>",
    "expected_script_blob_sha": "<git-blob-sha40>",
    "expected_script_sha256": "<script-sha256-hex64>",
    "source_revision": "<same-git-commit-sha40>",
    "image_namespace": "<trusted-container-registry>"
  },
  "timeout": 300
}
```

The caller supplies references **only**. The gateway resolves the registered
candidate and verifies its Git tree, approved deployment script, OCI image
identities, private journal volume, networks, and operator credentials.
Neither a custom command nor an arbitrary filesystem path is accepted.

The preparation step mounts the journal volume **read-only**, with no network
and no Docker socket. It checks for an unfinished deployment and verifies
that the previous web container is healthy and the companion container
matches the failed revision. It refuses missing or contradictory journal
evidence, unexpected containers, an accepted generation, or changed images.

After user confirmation, the gateway performs the same checks **again**,
including a fingerprint of the pending journal and the live topology, before
calling the fixed repository-owned recovery routine. Success must establish
a stable healthy previous site, removal of the failed companion container,
a cleared pending journal, and preserved historical failure evidence.
An ambiguous result is reported as such; it is never retried automatically.

## Registering a recovery procedure for another application

Operators and developers should treat each recovery contract as a
**versioned policy**, not a reusable shell endpoint:

1. Define a descriptive contract ID and exact, typed parameter names in
   `_DEPLOYMENT_RECOVERY_CONTRACTS`.
2. Register reviewed `prepare` and `execute` functions in
   `_recovery_contract_handlers()`. The preparation handler must read only
   bounded state; the execution handler must check confirmation-pinned state
   again before any mutation.
3. Bind the contract to canonical projects, immutable source and image
   digests, bounded service identities, private state, and explicit success
   evidence. Never take untrusted container names, scripts, paths, or shell
   commands from callers.
4. Add negative tests for mismatched revisions, missing state, foreign
   containers, confirmation races, partial rollback, timeouts, and failure
   evidence preservation.
5. Verify the external MCP tool schema and update the published catalog.
   No new MCP tool is necessary; clients still use the two common operations.

A fleet can register multiple recovery policies without a separate public
MCP function for each website. A future declarative profile mechanism could
share common strategies across many deployments, but a profile must not
silently inherit authority over unrelated Docker resources.

## Operational boundaries

Merging changes to the gateway repository does not install them into an
already-running gateway service; an independently verified gateway release
is necessary. The recovery tool also does **not** publish a new application
version. Once the old state has been reconciled, any new application rollout
must pass its own separate build, tests, approvals, and health checks.

An operator should consult a contract's own runbook for site-specific
implementation details and credentials. No private infrastructure address
or project-specific release incident is required to understand this API.
