# Site asset workflow — raw bytes, no extra MCP tools

This workflow belongs to the **sites** skill in `gateway_catalog`/`gateway_skill`
and to the HTTP/OpenAPI `workspace-assets` group. It deliberately DOES NOT
register an extra flat MCP tool. The REST control plane accepts binary streams
directly; `workspace_file_write` remains limited to UTF-8 text.

## Upload an existing image without changing a pixel

1. Use the **user-supplied file** as source. Never generate/reconstruct a
   replacement when the user asked to preserve the reference. Check ownership,
   dimensions and desired browser format locally.
2. Call `prepare_candidate_clone` on the correct project/repo, record its
   `project_id`, branch and base SHA. Do not write into the primary checkout.
3. Put original files in `public/visuals`, `public/images`, `public/icons`,
   `public/fonts` or `src/assets`. Extensions: PNG, JPEG, WebP, AVIF, ICO,
   TTF, WOFF2. Arbitrary SVG/HTML/JavaScript are not accepted by the binary
   route; edit text assets through reviewed `workspace_file_write`.
4. Upload the file through authenticated HTTP, providing its SHA-256. The API
   checks the digest, binary signature, authorized path, per-asset 8 MiB limit
   and read/write scope. Existing files require the exact previous digest.
   It does not fetch arbitrary URLs or perform Git, CI or Docker operations.

```bash
# Run on a workstation able to reach the authenticated Gateway API.
# Avoid putting API keys on the command line or committing them.
export GATEWAY_API_URL='http://127.0.0.1:8085'
export CANDIDATE_PROJECT='candidate-...'
export API_KEY='...'  # load from an existing authorized secret source

sha="$(sha256sum banner.png | cut -d' ' -f1)"
curl --fail-with-body -sS -X PUT \
  -H "X-API-Key: $API_KEY" -H "Content-Type: image/png" \
  --data-binary @banner.png \
  "$GATEWAY_API_URL/api/workspace/projects/$CANDIDATE_PROJECT/assets/upload?path=public%2Fvisuals%2Fbanner.png&sha256=$sha"
```

For a replacement, first call the metadata endpoint and append
`&previous_sha256=<exact-current-file-sha256>`. A stale digest gets HTTP
409 rather than silently discarding another agent's edit. With a missing
parent folder, append `&create_parents=true` (bounded and symlink-checked).

Read-only inspection and authorized source download:

```text
GET /api/workspace/projects/{candidate_id}/assets/inspect?path=public/visuals/banner.png
GET /api/workspace/projects/{candidate_id}/assets/download?path=public/visuals/banner.png
```

Both are authenticated; download responds with `private, no-store` and
`nosniff`. Response metadata includes SHA-256, MIME type and exact byte
length. A CI test or visual review should prove downloaded bytes match the
original before committing.

## Visual delivery checklist

- Keep original PNG as immutable reference when license/size allow; produce
  separately optimized high-resolution WebP/AVIF without resizing or
  cropping unless approved. Compare edges, thin mosaic seams, characters
  and mobile crop against the supplied file, not against a generated image.
- Update `<picture>` source order, intrinsic dimensions and responsive CSS.
  Never fall back to an unrelated stand-in SVG when an exact original is
  required. Avoid accidentally serving a lossy 20 KiB 1200 px version to
  full-width displays.
- Verify `npm run check`, `npm run build`, design regression tests, network
  content-type and actual browser screenshots for desktop/mobile.
- Diff only the candidate, commit, verified push, PR, exact-head CI, guarded
  merge, post-merge CI and guarded deployment. Production and its secrets stay
  outside asset upload. Preserve the original artifact SHA in the PR.

## Transport boundaries

The upload API needs an HTTP client with access to the local asset bytes and
the authenticated private Gateway endpoint. A ChatGPT attachment visible
only in the ChatGPT sandbox is *not* automatically mounted inside Gateway.
Do not claim that a gateway path such as `/mnt/data/banner.png` is available
on the remote host. A future attachment-to-HTTP bridge must explicitly
transfer the bytes through this API, with the same SHA and candidate guards.

There is no server-side arbitrary URL importer and no general-purpose remote
file write. Asset inspection is not a production image preview or a visual
browser acceptance.
