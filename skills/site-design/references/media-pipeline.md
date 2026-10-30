# Site Media Pipeline — isolated image conversion

Status: **source implementation; opt-in worker overlay**, not automatically
active in the existing Gateway production deployment. No decoder is imported
by the Gateway/MCP server. The worker is the only process running Pillow.

## Pipeline

`site_asset_transfer` uploads an original PNG/JPEG/WebP/AVIF into a
registered candidate. Keep this original unchanged, record its SHA-256.

Use the `site_media_pipeline` MCP tool in the **sites** group only with a
registered candidate project and source/target paths in allowed static asset
directories:

- `project_id`: candidate ID from `prepare_candidate_clone`
- `source_path`: e.g. `public/visuals/xloud-mosaic-hero-original.png`
- `source_sha256`: exact lowercase SHA-256 from `site_asset_transfer.inspect`
- `target_path`: e.g. `public/visuals/xloud-mosaic-hero.avif`; must match codec
- `codec`: `avif` / `webp` / `png`
- `quality`: integer 1–100, default 95; WebP 100 is lossless
- `max_width`: 0 preserves full resolution, or 320–4096 to resize with Lanczos
- `min_psnr`: bounded quality gate, default 36 dB (against the resized input)
- `previous_sha256`: required when replacing a pre-existing output

Response includes source SHA, output SHA, byte size, dimensions, quality
parameters and measured PSNR. It has no external URL, no production write
and no automatic commit, merge or deployment. Decoded pixels never return
to MCP or get written by Gateway. A failed quality gate or missing worker
returns a typed error and leaves the candidate untouched.

**High-fidelity XLOUD defaults:** source PNG kept, AVIF quality 95 in 4:4:4
with bounded encoding speed 9, WebP quality 100 (lossless) using encoder
method 3, full 1983 × 793 native dimensions;
visual acceptance against the supplied original remains mandatory. Do not
vectorize the mosaic hero; SVG is suitable only for genuine vector artwork.
The encoder strips EXIF by default, preserves bounded ICC profiles and
retains alpha from indexed (P-mode) PNG transparency.

## Isolated worker boundary

The image build runs both a small and a synthetic 1983 × 793 tiled sample.
Each full-size conversion must stay under 50 seconds of CPU time: on the
original XLOUD PNG, slower AVIF speed 6 and lossless WebP method 6 exceeded
practical timeouts, while AVIF speed 9 and WebP method 3 preserved quality.

A separate optional media worker image is defined by
`docker/Dockerfile.site-media-worker`, with a real build-time PNG→WebP
and PNG→AVIF encoder self-test. `docker/site-media.compose.yml` is
**not included in the normal release path**. Operators must explicitly
qualify and deploy the worker in a separately reviewed rollout.

### Safe operator activation (no automatic deployment)

1. Qualify the actual media image in exact-SHA CI, confirm it was published,
   and obtain its immutable registry **manifest digest**, not just a mutable
   tag. Set `SITE_MEDIA_WORKER_IMAGE` to
   `192.168.1.103:3005/gpakoh/site-media-worker@sha256:<verified digest>`.
   Do not use `:latest`, a local build, or the docker image ID in place of
   the manifest digest.
2. Provision a **separate operator-owned random token** (32–512 printable
   characters) through the existing private runtime secret channel as
   `SITE_MEDIA_WORKER_TOKEN`. Never commit it, put it in an Issue, or print
   interpolated Compose configuration in CI logs.
3. Run `python scripts/site_media_preflight.py` from the exact checkout
   with the operator's original Compose runtime environment. Its read-only
   `docker compose config --format json` inspection checks that `mcp-oauth`
   still has `internal_net`, the worker belongs only to `media_net`, the
   media network is internal, both services receive the same secret, and the
   worker's image, permissions and budgets are pinned. Any absence or drift
   returns **NO-GO** without changing a container.
4. After an approved maintenance/rollout window, invoke Docker Compose with
   the base `docker-compose.yml`, `site-media.compose.yml` and `--profile
   media`. Limit the controlled recreation to **mcp-oauth and
   site-media-worker**; do not update unrelated services. Both must be able
   to see `media_net`, while MCP retains the ordinary internal Gateway network.
   Use the normal pinned release/rollback procedure.
5. Inspect running container digest, networks, read-only/non-root profile,
   worker health, and MCP health. Execute a **real candidate-only upload →
   WebP/AVIF conversion → inspect/readback → SHA/quality check**. Never claim
   operational readiness on the strength of the build smoke alone. On failure
   stop the worker and revert only the media-network attachment of MCP.

The overlay is deliberately **image-only**: it cannot build a new worker
during production activation and cannot silently replace approved source.

The overlay uses private `media_net` with `internal: true`, no host ports,
no workspace bind, no Docker socket, non-root UID, read-only root filesystem,
dropped capabilities, no-new-privileges, 512 MiB memory, 1 CPU, limited
PIDs and a bounded tmpfs. The only allowed exchange is a token-authenticated
fixed `http://site-media-worker:8091/v1/convert` request with original
bytes. `SITE_MEDIA_WORKER_TOKEN` is operator-owned (at least 32
characters); absent secret makes processing unavailable without degrading
Gateway readiness. No arbitrary URL fetching, command execution, or
untrusted text-to-SVG rendering.

The worker rejects inputs over 8 MiB, more than 16 million decoded pixels,
dimensions over 8192, animations and unsupported formats. It redecodes the
output, computes PSNR, and refuses outputs below the requested quality
floor. Main Gateway independently verifies MIME, magic, checksum, dimensions,
PSNR and source SHA again before an atomic candidate-only write.

A **worker image build smoke is not a live deployment proof**. Before
production activation, verify isolated network, secret injection, exact
worker image digest, health, user, limits, an end-to-end candidate upload,
readback and rollback. Never enable an image codec inside `mcp-oauth`.

## Further scope

- P1: responsive source sets, preserved color-profile validation,
  independent browser screenshots with human visual acceptance.
- P2: experimental vectorization (VTracer) only for suitable source icons,
  with side-by-side preview, node/count limits and no replacement of an
  approved raster original.
- P2: FFmpeg for video/animation with a separate worker and input limits.

Neither VERT (AGPL) nor File Converter Pro (GPL/Windows UI) is vendored
or used as a backend dependency here.
