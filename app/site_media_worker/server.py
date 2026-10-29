"""Private HTTP endpoint for the isolated image converter (no workspace mount).

Run only in the opt-in, network-isolated OCI worker with an operator token.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

from app.site_media_worker.engine import MAX_BYTES, MediaError, convert


def handler_class(secret: str) -> type[BaseHTTPRequestHandler]:
    if len(secret) < 32:
        raise RuntimeError("SITE_MEDIA_WORKER_TOKEN must be at least 32 characters")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path != "/healthz":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/v1/convert":
                self.send_error(404)
                return
            token = self.headers.get("X-Media-Token", "")
            if not hmac.compare_digest(token, secret):
                self.send_error(403)
                return
            length = self.headers.get("Content-Length", "")
            if not length.isdecimal() or not 1 <= int(length) <= MAX_BYTES:
                self.send_error(413)
                return
            try:
                source = self.rfile.read(int(length))
                if len(source) != int(length):
                    raise MediaError("Incomplete source")
                digest = hashlib.sha256(source).hexdigest()
                if not hmac.compare_digest(digest, self.headers.get("X-Media-Source-Sha256", "")):
                    raise MediaError("Source checksum mismatch")
                result = convert(
                    source,
                    codec=self.headers.get("X-Media-Codec", ""),
                    quality=int(self.headers.get("X-Media-Quality", "95")),
                    max_width=int(self.headers.get("X-Media-Max-Width", "0")),
                    min_psnr=float(self.headers.get("X-Media-Min-PSNR", "36")),
                )
            except (MediaError, ValueError, OSError):
                self.send_error(422, "Conversion refused")
                return
            self.send_response(200)
            self.send_header("Content-Type", {"avif": "image/avif", "webp": "image/webp", "png": "image/png"}[result.codec])
            self.send_header("Content-Length", str(len(result.content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Media-Source-Sha256", result.source_sha256)
            self.send_header("X-Media-Output-Sha256", result.output_sha256)
            self.send_header("X-Media-Width", str(result.width))
            self.send_header("X-Media-Height", str(result.height))
            self.send_header("X-Media-PSNR", str(result.psnr))
            self.send_header("X-Media-Lossless", str(result.lossless).lower())
            self.end_headers()
            self.wfile.write(result.content)

        def log_message(self, _format: str, *_args: object) -> None:
            # Avoid accidentally recording request headers, source pixels
            # or operator secrets in container logs.
            return

    return Handler


def main() -> None:
    secret = os.environ.get("SITE_MEDIA_WORKER_TOKEN", "")
    server = HTTPServer(("0.0.0.0", 8091), handler_class(secret))
    server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    main()
