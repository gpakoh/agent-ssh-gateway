# Site Audit: protected historical LKG forensic archive

The live historical Site Audit deploy-state lacks the original CRM ingress identity.
This cannot be repaired by adding a current identity to the old record.

## Dedicated operator action

`docker_prepare_site_audit_lkg_recovery` is an ADMIN-scoped, confirmation-only
operation. The requesting operator binds a registered **clean exact-head Site Audit
candidate**, its script Git blob and SHA-256, and the exact historical source revision.
Gateway resolves the fixed deploy-state volume and fixed source-owned script path.
It rejects mismatched Git state, untrusted volumes and helper images.

The first read-only Docker helper run checks 12 live containers and the old
deployment receipt. It reports a forensic snapshot, **not** an accepted LKG.
The high-risk confirmation binds the historical SHA-256 and an exact fingerprint
of the observed inventory. During confirmation, Gateway revalidates the exact
Git/source/volume and repeats the read-only check; a changed fingerprint stops
the operation before any archival write.

Only a validated confirmation launches `--archive` in the pinned helper. The
script acquires the deploy transaction flock, rechecks absence of pending state,
repeats live inspection and writes original bytes and a distinct forensic
receipt under `recovery-evidence/<sha256>/`. The archived receipt explicitly
states `accepted_lkg=false` and `authorizes_deployment=false`. Old
`deploy.json` is not rewritten; missing historical evidence is never backfilled.

The helper container has the fixed named state volume, candidate bind and
Docker socket; while the script issues inspect-only calls, the Docker socket is
privileged, so **never run an unreviewed candidate**. The code is pinned by
full source SHA, Git blob and file SHA-256. The caller cannot supply a Docker
target name, arbitrary executable, volume, or host path.

## Remaining acceptance gates

Archival evidence is not permission to deploy. Independently qualify immutable
five-image release artifacts, confirm the guarded Site Audit deploy transaction,
record a *new* complete seven-protected-container snapshot, and verify the
new accepted-LKG read-only status and CRM/PDF end-to-end acceptance.

No arbitrary `docker exec`, generic Compose-up, shell edits to volumes,
or guessed historical protected identities are acceptable substitutes.

Tests:
- `tests/test_site_audit_lkg_operator.py`
- `tests/test_site_audit_deploy_contract.py`
- `tests/test_tool_surface_parity_invariants.py`
