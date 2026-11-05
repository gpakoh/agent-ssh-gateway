# Gitea private Actions ZIP transport — internal acceptance contract (#792)

Status: **internal staging primitives only**. This code is not an MCP ZIP read
tool, does not transmit a ZIP to a chat client, and does not publish a preview.

## Trust boundary and preflight

- Do not accept arbitrary URLs, shell commands, filesystem paths, or destination
  directories from an MCP caller. The operator configures and pre-provisions a
  dedicated storage parent and one direct private child for the MCP process.
  Both must be **real directories**, owned by the service effective UID, with
  mode **0700**. No implicit mkdir, chmod, or chown and no symlink.
- The parent must be an approved isolated mount or private service directory.
  `ArtifactSpool(trusted_parent, root)` fails closed on permission drift and
  rechecks before each download. Each ZIP resides in an unnamed temporary
  inode (`0600`, `st_nlink == 0`), never in the Git checkout, a shared
  project/candidate, a chat attachment, or the host's public web directory.
  Every cancellation/failure revokes the descriptor; process restart closes
  all descriptors and invalidates the process-local handle registry.
- Limit reservations plus retained handles to at most **2** by default.
  Successful handles expire after **120 seconds** using a scheduled callback;
  revocation closes the file even without a later request. No filesystem
  inventory or global cleanup is necessary.
- Never treat `gitea_list_action_run_artifacts` metadata as download
  authorization. For every download the trusted caller must prove, using live
  Gitea reads, an **open, same-repository PR** and its exact lower-case 40-hex
  HEAD, a **completed successful `pull_request` run** with the exact run ID
  and attempt, and an artifact from that run whose own
  `workflow_run.run_attempt` is explicitly present and matches. Missing
  attempt, expired/unknown expiry, incomplete inventory, or drift is denial.
  Repeat the proof after the private byte transfer, before inspection, and
  after inspection. Do not infer run provenance from a similar name or nearby
  push event.
- Before requesting signed URLs, `GiteaClient` reads a bounded `/version`
  response and requires stable Gitea >= **1.26.2** for the known Actions V4
  signed URL issue (GHSA-hg5r-vq93-9fv6). Unproven/patched-backport versions
  are not whitelisted without an additional reviewed attestation contract.
  Live server version and its hardening are **not yet proven**.
- ZIP download is a bounded raw API request for one `artifact_id`; an
  optional 302 redirect may only target an operator-configured **exact HTTPS
  origin**. A separate credential-free client fetches the signed URL with
  no redirected Authorization header, no environment proxy, and no further
  redirect. URLs/headers/PATs/ZIP bytes must not enter MCP outputs or logs.
  A 20 MiB transfer cap is enforced. SHA-256 is computed on received bytes
  and independently re-computed from the unnamed inode. ZIP directory
  validation rejects unsafe traversal, duplicates, symlinks, encrypted
  members, excessive members/expansion. It does **not** extract files.
- `expected_sha256` should come from an independently verified source-owned
  digest artifact or equivalent signed manifest, **not from the untrusted ZIP
  metadata itself**. Without that sidecar proof the transfer is only a
  transport integrity observation, not an authenticated site-content claim.

## Consumption boundary

`ArtifactSpool.stage()` returns only an opaque, random, process-local handle
and bounded non-secret SHA/size/TTL metadata. It is not exposed as MCP.
`inspect_once()` consumes the handle before awaiting any external reads.
The callback is **trusted, side-effect-free, in-process only**; no output from
the callback is returned. The file closes on success, exception, timeout, or
cancellation. Callback execution is bounded by remaining TTL; the PR/run
provenance is rechecked afterward before an integrity report is returned.

**No side-effecting or external publisher may call `inspect_once()`.** Once
a real Design/XLOUD consumer is specified, implement a separate reviewed,
atomic staged/commit authorization contract with independent recipient identity,
ingress credentials and rollback. Until then, private ZIP material is never
made accessible to the consumer, and hosted-preview GO is blocked.

## Required release gates still open

1. Qualify current Gitea version and whether the signed ZIP endpoint uses the
   expected redirect, from an operator-controlled trusted observation. Check
   actual storage origin(s); bind and approve each explicit HTTPS origin.
2. Select and provision the dedicated storage parent/child for the real
   container UID and volume. Verify both 0700 ownership, mount isolation and
   cleanup/restart semantics in isolated staging. Do not mutate production
   directories or unrelated Docker volumes as a shortcut.
3. Establish a **new live open XLOUD PR** with exact retained two preview
   artifacts, confirmed per-artifact attempt, independent source digest and
   stable artifact expiry. There is currently no open XLOUD PR for that proof.
4. Build a reviewed, authenticated Design consumer that can accept an opaque
   isolated artifact only after final lease verification, with atomic commit
   and revoke/rollback; never return ZIP/PAT/host path through MCP.
5. Run negative staging E2E for expired/closed/changed PR, run retry, artifact
   missing/size mismatch, wrong digest, malicious redirect, cancellation,
   storage permissions, TTL expiry, and post-deploy exact-host smoke. Only
   then expose a public handle-based MCP workflow and request production GO.

Focused tests before this public integration:
`tests/test_gitea_artifact_spool.py`,
`tests/test_gitea_artifact_zip_transport.py`, and
`tests/test_gitea_artifact_metadata_read.py`.
