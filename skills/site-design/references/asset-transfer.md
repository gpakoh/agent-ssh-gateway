# Site asset workflow — MCP chunks and raw-byte REST

This workflow belongs to the **sites** skill in `gateway_catalog`/`gateway_skill`
and to the HTTP/OpenAPI `workspace-assets` group. The single **`site_asset_transfer`**
MCP capability is discoverable in the `sites` catalog group and available only
in the explicitly enabled `mcp_client_write` mode. Existing text tools remain
UTF-8-only, and the REST endpoint continues to accept binary streams directly.

## Upload through the MCP site-assets capability

When the MCP client has **the original bytes**, use `site_asset_transfer` with
`project_id` from `prepare_candidate_clone`, and a permitted project-relative
path such as `public/visuals/banner.png`. No generic SSH command is needed.

1. `action=begin` with `sha256` (full file, lowercase hex), `total_bytes`
   (maximum 8 MiB), and optionally `previous_sha256` when replacing an existing
   file. Save returned unpredictable `upload_id`.
2. Split the original bytes into at most 65,536-byte chunks. For each chunk,
   call `action=chunk` with `upload_id`, `offset`, RFC4648 `chunk_b64` and
   the chunk's own `chunk_sha256`. Use the returned `next_offset` for the next
   chunk. Replaying the identical chunk at the same offset is idempotent.
3. `action=status` reports progress after an ambiguous response. `action=finish`
   verifies all bytes, format, full SHA-256 and exact old revision, then writes
   atomically using the existing workspace policy. `action=abort` discards a
   partial transfer.
4. Verify with `action=inspect` and, where necessary, `action=read` using
   exact SHA and offset. `read` returns a base64 chunk plus chunk digest; it
   cannot silently mix versions of the asset.

Staging is bounded to four concurrent uploads and expires after 15 minutes
without traffic. A Gateway MCP restart drops incomplete transfers; begin
again, or inspect the destination if the result of `finish` was uncertain.

**Attachment boundary:** this protocol accepts real base64 bytes provided by
an MCP client. The ChatGPT tool layer does *not* automatically inject uploaded
ChatGPT Files content or resolve `sandbox:/` paths into MCP arguments. Do not
claim a ChatGPT attachment has been delivered until a client has supplied its
bytes, all parts have been accepted, and `finish` returns verified metadata.
A native attachment binding in the client is a separate capability from MCP.

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

The MCP tool and raw-byte REST endpoint share the same checked candidate-write
contract; neither imports arbitrary URLs or assumes ChatGPT sandbox files are
mounted on the Gateway host. ChatGPT Files are not exposed to the MCP server
unless the client actively transmits their content through tool arguments.
The server cannot access a private user attachment by a string file ID alone.
Asset inspection is not a browser screenshot or proof of production delivery.
