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

    # Indexed PNG (P mode) stores alpha in tRNS; preserve this for logos
    # instead of silently converting transparent pixels to opaque RGB.
    palette = Image.new("P", (32, 24), 0)
    palette.putpalette([0, 0, 0, 245, 44, 28] + [0, 0, 0] * 254)
    palette.info["transparency"] = 0
    for x in range(8, 24):
        for y in range(6, 18):
            palette.putpixel((x, y), 1)
    transparent_bytes = io.BytesIO()
    palette.save(transparent_bytes, format="PNG")
    preserved = convert(transparent_bytes.getvalue(), codec="webp", quality=100, min_psnr=36)
    with Image.open(io.BytesIO(preserved.content)) as check:
        rgba = check.convert("RGBA")
        assert rgba.getpixel((0, 0))[3] == 0
        assert rgba.getpixel((12, 12))[3] == 255

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
