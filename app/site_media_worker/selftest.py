"""Build-time codec evidence for the isolated media worker image."""

from __future__ import annotations

import io

from PIL import Image, features

from app.site_media_worker.engine import convert


def main() -> None:
    if not features.check("webp") or not features.check("avif"):
        raise RuntimeError("Pillow wheel lacks required WebP / AVIF codec")
    image = Image.new("RGB", (128, 64), (23, 63, 144))
    raw = io.BytesIO()
    image.save(raw, format="PNG")
    source = raw.getvalue()
    for codec, quality in (("webp", 100), ("avif", 95)):
        result = convert(source, codec=codec, quality=quality, min_psnr=36)
        assert result.codec == codec
        assert (result.width, result.height) == (128, 64)
        assert result.psnr >= 36
        assert result.source_sha256 != result.output_sha256
        with Image.open(io.BytesIO(result.content)) as decoded:
            assert decoded.size == image.size
            assert decoded.format == codec.upper()


if __name__ == "__main__":
    main()
