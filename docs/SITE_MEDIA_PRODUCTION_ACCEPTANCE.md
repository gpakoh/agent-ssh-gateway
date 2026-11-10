# Gateway → Quart Media API: opt-in production activation

Issue: agent-ssh-gateway #733, infra-quart #586.

## Stable topology

The media API is deployed by infra-quart only as a separate immutable-image
Compose project (`quart-media-processing`), with no published host port.
`quart-media-internal` is a shared-by-authorized-clients Docker bridge with
`internal: true`. It is **not** an exclusive Gateway network. Existing
`internal_net` is a distinct external bridge (`internal: false` and many
members); never attach the media server to it as a deployment shortcut.

The Gateway overlay `docker/site-media.compose.yml` attaches **only**
`mcp-oauth` to both its existing `internal_net` and the external
`quart-media-internal`. Keep all other Gateway services/networks unchanged.

## Operator-owned opt-in (no secrets in Git)

The live Gateway deployment uses the exact Gitea Actions checkout; Docker
labels can point at an ephemeral runner's `/workspace/...` directory. The host
checkout at `/media/1TB/Python/web_ssh/web-ssh-gateway` may be stale/dirty:
**never reset/replace its `projects.yaml`** merely to activate media.

The canonical CI deploy mounts the host secret file
`/media/1TB/Python/web_ssh/web-ssh-gateway/docker/.env` read-only. Keep its
contents private and permissions no broader than needed by the trusted CI
runner. The qualified host operator configuration uses owner UID 1000, group GID
1001 (trusted CI runner), and mode 0640; default runner UID/GID is 1001.
Do not set world-readable mode 0666. If CI identity changes, reassess the
narrow group permission or use a verified per-runner read-only ACL, without
printing env contents. Mode 0600 alone prevented CI deployment for UID 1001.

The authorized root operator provisions these two entries in that existing
private Gateway config **without changing any other assignments**:

- `SITE_MEDIA_WORKER_TOKEN`: the exact existing legacy token from the root-only
  `/root/.config/quart-media/runtime.env`; do not copy `MEDIA_API_KEYS`.
- `SITE_MEDIA_OVERLAY_ENABLED=true`: explicit opt-in. Default is `false`.

Do not put tokens in command arguments, workflow inputs, Git, issue comments
or logs. Do not enable shell xtrace. If the operator config already defines a
different media token or a custom `COMPOSE`, stop and reconcile; do not blindly
append a second variable. Source-controlled deployment logic, not host custom
`COMPOSE`, selects the reviewed overlay.

On each opt-in CI deployment, `scripts/deploy-from-registry.sh`:

1. Runs `scripts/site_media_preflight.py` against the combined redacted
   Compose config **before pulling or recreating containers**.
2. Deploys exact immutable CI images with existing Gateway LKG discipline,
   preserving `internal_net` and adding `quart-media-internal` only to
   `mcp-oauth`.
3. Requires authenticated Media `/healthz`, `/ready`, capabilities and
   invalid-credential rejection via `site_media_runtime_smoke.py` executed
   **inside the freshly deployed mcp-oauth image**.
4. If the first attachment fails, restores the base-only Compose config
   during image rollback (no persistent Media network on OAuth). If already
   attached before this release, rollback retains that prior topology.

This smoke verifies OAuth client → Media API connectivity, **not** the
candidate data plane. Full approval also requires a real authorized candidate
`site_asset_transfer(begin/chunk/finish) → site_media_pipeline → inspect/read`,
source immutability, output hash/MIME/PSNR, a negative CAS test and a reversible
overlay rollback. Record only SHA/PSNR/status, not confidential bytes.

## Production acceptance and retirement evidence (2026-10-31)

Gateway `master@d546f0ae7d136ac9ea1f1877504f06a7260c3a09` was
qualified by post-merge Gitea Actions #21623 (build, digest-pinned deploy,
LKG state and independent host-smoke all SUCCESS). The dedicated Media API
remains an infra-quart-owned deployment, not a Gateway image or Compose
service. Actual candidate-scoped Gateway tools converted the same 320×240
PNG to WebP (220 bytes, PSNR 52.90 dB) and AVIF (344 bytes, PSNR 48.13 dB).
Original, WebP and AVIF all passed independent inspect/readback SHA-256,
correct MIME and EOF. Wrong source SHA and duplicate target without CAS
were denied, without source or output corruption.

### Historical codec acceptance outputs (archival reconciliation, 2026-11-10)

The separate historical branch `archive/candidate-gateway-media-acceptance-733-20261031`
(commit `3f15b78298fb42946e677d2e8b5f0caf79e5b00f`) held only
three generated codec outputs, with **no source-code or test references**.
Their archived content hashes and sizes were:

| Historical output | Bytes | Archived SHA-256 |
| --- | ---: | --- |
| `media-acceptance-20261031.png` | 170 | `ffc1787e338c93e1526842bcc265854ed21506e6636f319cc06b542bf7110770` |
| `media-acceptance-20261031.webp` | 220 | `d4e2609f2009772253d9c40d221a481ee9c7320530bb76086e5be7216ea51f00` |
| `media-acceptance-20261031.avif` | 344 | `6d03460f4e3c824406b44b39d421a906c987af4773b8f52002f71fc604d06cfa` |

These fingerprints preserve the historical **archive inventory**, not proof that
the archived bytes are identical to the images used in the live conversion
described above: its 2026-10-31 receipt did not record those SHA-256 values.
Neither the generated binary outputs nor the old branch's unrelated parent
history are production source changes or approved reusable test fixtures.
Do not publish possible candidate data bytes to the main branch or count this
metadata-only reconciliation as a new Media API end-to-end acceptance (#733).

The old `app/site_media_worker/`, `docker/Dockerfile.site-media-worker`
and Gateway codec-image CI build/push are retired after the verified live
acceptance. The Gateway continues to test the standalone API contract and
private network overlay; codec source, release and quality qualification
belong to `media-processing-service` and infra-quart.

Rollback is tested in an unprivileged Bash harness that executes the exact
first-attachment detection and topology restoration fragments from the
production deploy script with a **fake** Docker inspector. It verifies the
previous base-only network is restored on initial opt-in failure, while a
pre-existing private attachment is retained; LKG image references are
preserved. This does **not** claim an intentional production rollback or
DB schema downgrade: avoid deliberately disrupting a healthy release.

## Rollback

To intentionally disable the overlay, set
`SITE_MEDIA_OVERLAY_ENABLED=false` in the operator-owned Gateway env and
redeploy the reviewed exact image through the same release process. The base
Compose retains `mcp-oauth` on `internal_net` and detaches the optional
media network. Do not run `docker compose down`, remove shared volumes, or
modify the infra-quart media server during a Gateway rollback.
