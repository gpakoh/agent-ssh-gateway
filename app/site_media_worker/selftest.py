"""Build-time codec evidence for the isolated media worker image."""

from __future__ import annotations

import io
import time

from PIL import Image, ImageDraw, features

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

    # Production-like 1983x793 synthetic tile edges: a tiny image alone
    # cannot catch libaom encoder profiles that stall on real-sized banners.
    large = Image.new("RGB", (1983, 793), (15, 50, 89))
    draw = ImageDraw.Draw(large)
    for y in range(0, 793, 17):
        draw.line((0, y, 1983, y), fill=(239, 216, 171), width=2)
    for x in range(0, 1983, 17):
        draw.line((x, 0, x, 793), fill=(239, 216, 171), width=2)
    for index in range(500):
        x, y = (index * 107) % 1970, (index * 53) % 778
        draw.rectangle((x, y, x + 11, y + 8), fill=((index * 17) % 256, (index * 29) % 256, (index * 31) % 256))
    raw = io.BytesIO()
    large.save(raw, format="PNG")
    source = raw.getvalue()
    for codec, quality in (("webp", 100), ("avif", 95)):
        started = time.process_time()
        result = convert(source, codec=codec, quality=quality, min_psnr=36)
        cpu_seconds = time.process_time() - started
        assert cpu_seconds < 50, f"{codec} full-size encoding exceeds CPU budget: {cpu_seconds:.1f}s"
        assert result.width == 1983 and result.height == 793
        assert result.psnr >= 36


if __name__ == "__main__":
    main()
