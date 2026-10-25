# SSH Gateway deploy-state trust migration

**Status:** production migration qualified on 2026-10-25 by exact-master
CI #19743. One-time bootstrap remains restricted to the attested baseline.
Do not delete/rewrite the original historical host receipt.

## Exact-release qualified CI bootstrap

Before deploying, CI calls scripts/ci-bootstrap-deploy-state.sh in the actual
Docker deploy job. Only a completely empty dedicated named volume may be
initialized. The helper verifies five running containers against three
immutable image digests, requires healthy status and exact BUILD_SHA for
gateway/MCP/OAuth, and uses the successful build/deploy/host-smoke #19659
as independently verified baseline SHA
b4c6c9c6d268f8db638f22d4c9256832dddcd875. It cannot trust a different
running release or the historical world-writable receipt. The helper provisions
ownership via a short-lived root helper container before the non-root deploy
job runs the attestation bootstrap. The root helper mounts only
ssh-gateway-deploy-state and changes ownership/mode only if it is completely
empty, originally root-owned or owned by the effective deploy UID; it leaves
existing receipts untouched. This split is necessary because a non-root job
cannot chown the default root-owned Docker volume (as observed in failed
post-merge CI #19716). An error from either helper is a release stop, not an
invitation to reuse the world-writable checkout receipt. It then provisions
the actual effective UID with mode 0700 and writes a verified 0600 atomic
receipt. Missing/mismatched evidence and unknown volume contents fail before
deployment. Existing receipts are preflight-checked and never modified.

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

If the exact-release automatic bootstrap does not qualify, use these
operator-controlled steps instead. Never bypass the failed CI gate by
unconditionally trusting the current running stack:
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

**Accepted production evidence, 2026-10-25:** PRs #631 (fail-closed atomic
receipt), #661 (dedicated-volume ownership) and #662 (Python 3.10 support)
were merged and their branches removed. Gitea Actions run #19743 completed
SUCCESS at master SHA `9f7baa48760fba85f52c77fe4e055a5f7ac42a34`,
with `build-and-push`, `deploy`, and `host-smoke` all SUCCESS. The deploy job
first logged `Seeded trusted baseline LKG for attested release
b4c6c9c6d268f8db638f22d4c9256832dddcd875` (previous successful
build/deploy/host-smoke #19659), then `Deploy OK — recorded as last known
good.` The host-smoke verified exact `BUILD_SHA` for gateway/MCP/OAuth
and completed 16 selected tests (16 passed; 1 collection skip). Independently
inspected five live healthy containers (zero restarts) and verified immutable
refs for gateway `sha256:7599a69010161431931d1638853761023aaab7687e524bc567c0be9d3965902d`,
MCP/OAuth `sha256:4c7ec7ad668c86820a70b94bf95c5e691ec70f67f4dff1b74d540426cee57d8e`,
and SSHD `sha256:4a4bd538003b4bc80e9e40380f01852d84d63c2ef26b58d28d308bdae387123f`.
This qualifies the initial trusted migration and release, not an exercised
disaster-recovery rollback. Retain fail-closed behavior and independent
qualification requirements for any new baseline. Historical failed deploys
#19021, #19716, and #19730 remain failures, not alternative evidence.
