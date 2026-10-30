# Site Media Pipeline — independent Media API adapter

The actual raster processor now lives in independent repository
\`gpakoh/media-processing-service\`, main
\`ca8aa9741fa88211c3ff8d72e02bb81535c7cb08\`.
It has its own HTTP API, codecs, CI, image qualification and release ownership.
The Gateway only owns candidate ACLs, transferring bytes, SHA/CAS and tool
responses. No media service source, Docker image or persistent data belongs in
the Gateway's deploy topology.

## Candidate conversion

1. \`site_asset_transfer\` uploads an original PNG/JPEG/WebP/AVIF in bounded
   chunks, verifies its SHA-256 and atomically writes to registered candidate
   static assets. Keep the original unchanged.
2. \`site_media_pipeline\` accepts candidate project ID, source path + SHA-256,
   target path with \`.avif\`, \`.webp\` or \`.png\`, codec, quality (1..100),
   optional max_width (0 or 320..4096), min_psnr (0..99) and CAS
   \`previous_sha256\` for output replacements.
3. Gateway re-verifies source bytes before HTTP transmission; only the approved
   private endpoint \`http://media-processing:8091/v1/convert\` is accepted.
   The service receives raw bytes and cannot access files, Git, Gitea or
   project names.
4. Gateway re-verifies response MIME, magic, SHA-256 of original/output,
   bounded dimensions, PSNR and source immutability before saving output via
   candidate-only CAS. The original stays intact. On failure no target
   file is changed.
5. Temporary \`429\`, \`503\`, \`504\` errors are marked retryable in MCP;
   rejected auth/parameters and policy errors fail closed.

The HTTP protocol is compatible with the legacy worker's
\`X-Media-Token\` / \`X-Media-*\` headers. \`/v1/jobs*\` in the independent service
is contract-only and returns \`501\`; Gateway uses synchronous v1 conversion
and does not invent a background queue. PNG/WebP/AVIF codecs and quality gates
are owned and tested by media-processing-service.

## Isolated network and deployment

\`infra-quart\` owns \`deploy/media-processing.compose.yml\`, the pinned
container image, standalone security preflight and Docker network
\`quart-media-internal\` (\`internal:true\`). Media API has no host ports,
workspace mounts or Docker socket.

Gateway's optional \`docker/site-media.compose.yml\` overlay attaches
**only mcp-oauth** to that external media network and preserves its original
\`internal_net\` for Gateway access. It injects the operator-owned
\`SITE_MEDIA_WORKER_TOKEN\` (32..512 printable non-space characters) into the
MCP process and sets \`MEDIA_API_BASE_URL=http://media-processing:8091\`.
The same token must be configured privately in the media service as its
legacy Gateway client. Neither token is stored in Git.

The read-only \`python scripts/site_media_preflight.py\` rejects missing
secrets, changed client URL, missing \`internal_net\`, unexpected media
network or an embedded worker service. It never prints secrets.

**Runtime activation is an independent guarded rollout.** Qualify and start
Media API in infra first, then activate only Gateway OAuth's optional media
attachment. Verify \`/ready\` with auth and real candidate PNG -> WebP/AVIF ->
inspect/readback -> SHA/quality. If checks fail, disconnect the optional
media network and retain Gateway's existing internal network. Production
readiness is never inferred from a successful container image build.

The previous embedded worker files may remain as deprecated reference code
until live acceptance and rollback are proven; its image is not deployed by
the new overlay. Do not modify or build codecs inside mcp-oauth.

## Further development

- Expand client contract (capabilities, explicit error taxonomy, request IDs)
  while keeping fixed private host/network and no arbitrary URL fetching.
- Use independent browser render + human comparison for visual acceptance;
  the original XLOUD mosaic hero is raster, not suitable for auto-SVG.
- Future vector/video codecs and durable jobs are separate service work,
  not additions to Gateway.
