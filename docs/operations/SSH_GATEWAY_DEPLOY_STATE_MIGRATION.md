# SSH Gateway deploy-state trust migration

**Status:** operator prerequisite for adopting the hardened deploy receipt.
This procedure is not executed automatically by CI, Gateway or the candidate
test suite. Do not delete/rewrite the existing host receipt during rollout.

## Why the old state cannot be accepted

The historical source checkout uses `web-ssh-gateway/.state` with a 0777
directory and `web-ssh-gateway-deploy.json` with 0666 permissions. The host
checkout also has writable ancestors. A syntactically valid digest-pinned
rollback target is not enough to prove who authored that JSON. No safe program
can retroactively establish that the old LKG content was not changed.

`scripts/deploy_state.py` refuses symlinks, permissive ancestors and files,
wrong ownership, corrupt JSON, multiple hardlinks and unexpected keys.
A healthy running container is not itself proof of the exact Git release SHA.

## Before the first deploy with the new version

1. Obtain an operator-approved trusted image set: exact SHA, three
   repository-qualified image digests from the registry, and running-container
   evidence. Verify the selected gateway/MCP images against the same release
   SHA **independently**; do not trust the legacy JSON for this step.
2. Provision the dedicated Docker volume `ssh-gateway-deploy-state` used by
   the CI deployment job. Mount it in a controlled operator context, identify
   the actual **effective UID inside the deploy job**, and create its
   `/deploy/web-ssh-gateway-state` directory owned by that UID with mode 0700.
   The path and all ancestors must be non-symlinks and not group/world writable
   (a root-owned sticky /tmp-style ancestor is permitted). Do not guess
   ownership from host usernames; the runner and container may map UIDs.
3. From the exact trusted checkout in that environment, verify the new
   directory with `python3 scripts/deploy_state.py preflight
   /deploy/web-ssh-gateway-state`. Bootstrap its receipt with the helper's
   `write` action and the independently verified gateway/MCP/SSHD
   digest-pinned refs plus SHA. It writes mode 0600 atomically. Repeat
   `preflight` and read-only `read deploy_sha`; compare to operator
   verification before enabling deploy.
4. Do **not** fix the legacy 0666 receipt in place and then treat it as
   authenticated. Keep its original bytes and ownership for forensic audit.
   Preserve its location as a historical artifact, not as the deployment
   authority. Record the migration/change in the operations log.
5. Only after the trust migration, allow a CI run to deploy. Verify
   `build-and-push`, `deploy` and `host-smoke` for the **same exact SHA**;
   independently compare running repository digests and BUILD_SHA. Failures
   require investigation, not an unguarded retry or automatic rollback
   fallback to historical state.

CI now mounts the dedicated named volume and sets
`DEPLOY_STATE_DIR=/deploy/web-ssh-gateway-state`. Manual/local deploys can
pass a corresponding explicitly approved `DEPLOY_STATE_DIR`; defaulting
to the checkout's `.state` is intentionally fail-closed in the current
world-writable installation.

The helper validates before any Docker pull and on every state read. It writes
0600 through an O_EXCL temporary file in the private directory, fsyncs the
file, atomically renames it, fsyncs the directory and rereads the committed
content. Ambiguous commit/fsync errors do not report success. This is an
integrity safeguard, not an authorization service or complete release receipt
system.

## Delivery gate

This security PR may be reviewed and tested in isolation, but deployment must
not proceed until the trusted volume is provisioned and the independent
source-of-truth validation has completed. The current CI red `deploy` run
#19021 and the published SHA tags must not be reinterpreted as a successful
host-smoke or authenticated rollback receipt.
