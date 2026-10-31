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
runner. POSIX ACL for the exact runner UID is preferable to world-readability;
mode 0666 is forbidden. Note that chmod 0600 alone may block a runner with
a different UID, resulting in `docker/.env: Permission denied`. Diagnose the
runner UID and use a narrow read-only ACL, without printing env contents.

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

## Rollback

To intentionally disable the overlay, set
`SITE_MEDIA_OVERLAY_ENABLED=false` in the operator-owned Gateway env and
redeploy the reviewed exact image through the same release process. The base
Compose retains `mcp-oauth` on `internal_net` and detaches the optional
media network. Do not run `docker compose down`, remove shared volumes, or
modify the infra-quart media server during a Gateway rollback.
