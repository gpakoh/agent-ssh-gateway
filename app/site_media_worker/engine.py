"""Pure image conversion core, loaded ONLY in isolated site-media worker.

The main Gateway process never imports Pillow or decodes untrusted pixels.
"""

from __future__ import annotations

import hashlib
import io
import math
from dataclasses import dataclass

MAX_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 16_000_000
MAX_DIMENSION = 8192


class MediaError(ValueError):
    """A refused conversion, unsuitable source or quality failure."""


@dataclass(frozen=True)
class Conversion:
    content: bytes
    codec: str
    width: int
    height: int
    source_width: int
    source_height: int
    psnr: float
    source_sha256: str
    output_sha256: str
    lossless: bool
    quality: int


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def convert(
    source: bytes, *, codec: str, quality: int = 95,
    max_width: int = 0, min_psnr: float = 36.0,
) -> Conversion:
    """Decode under container limits, encode, then verify a decoded round-trip.

    Original pixels are never modified on disk. Metadata apart from a bounded
    ICC profile is intentionally stripped (including EXIF/GPS). PSNR is
    measured against the resized source, never falsely against full-size pixels.
    """
    if not source or len(source) > MAX_BYTES:
        raise MediaError("Input must contain 1..8 MiB")
    if codec not in {"webp", "avif", "png"}:
        raise MediaError("Only png/webp/avif outputs are allowed")
    if type(quality) is not int or not 1 <= quality <= 100:
        raise MediaError("Quality must be an integer in 1..100")
    if type(max_width) is not int or max_width != 0 and not 320 <= max_width <= 4096:
        raise MediaError("max_width must be 0 or between 320 and 4096")
    if not isinstance(min_psnr, (int, float)) or not math.isfinite(min_psnr) or not 0 <= min_psnr <= 99:
        raise MediaError("min_psnr must be finite in 0..99")

    from PIL import Image, ImageChops, ImageOps, ImageStat, UnidentifiedImageError

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with Image.open(io.BytesIO(source)) as probe:
            if probe.format not in {"PNG", "JPEG", "WEBP", "AVIF"}:
                raise MediaError("Input must be a PNG/JPEG/WebP/AVIF image")
            if getattr(probe, "n_frames", 1) != 1:
                raise MediaError("Animated images require a separate pipeline")
            width, height = probe.size
            if (
                width < 1 or height < 1 or width > MAX_DIMENSION
                or height > MAX_DIMENSION or width * height > MAX_PIXELS
            ):
                raise MediaError("Input dimensions exceed the media-worker limit")
            probe.verify()
        with Image.open(io.BytesIO(source)) as decoded:
            icc = decoded.info.get("icc_profile")
            if not isinstance(icc, bytes) or len(icc) > 128 * 1024:
                icc = None
            image = ImageOps.exif_transpose(decoded)
            image.load()
            image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        if isinstance(exc, MediaError):
            raise
        raise MediaError("Invalid image or unsafe decoder input") from exc
    width, height = image.size
    if max_width and width > max_width:
        height = max(1, round(height * max_width / width))
        width = max_width
        image = image.resize((width, height), Image.Resampling.LANCZOS)

    options: dict = {}
    if icc:
        options["icc_profile"] = icc
    if codec == "webp":
        options.update(format="WEBP", lossless=(quality == 100), quality=quality, method=6)
    elif codec == "avif":
        options.update(format="AVIF", quality=quality, subsampling="4:4:4", speed=6, max_threads=2)
    else:
        options.update(format="PNG", optimize=True)
    if codec == "avif" and image.mode == "RGBA" and image.getchannel("A").getextrema() == (255, 255):
        image = image.convert("RGB")

    buffer = io.BytesIO()
    try:
        image.save(buffer, **options)
        output = buffer.getvalue()
        if not output or len(output) > MAX_BYTES:
            raise MediaError("Converted output exceeds the 8 MiB asset boundary")
        with Image.open(io.BytesIO(output)) as check:
            if check.size != (width, height) or check.format != codec.upper():
                raise MediaError("Encoder round-trip format or dimensions mismatch")
            check.load()
            result = check.convert(image.mode)
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        if isinstance(exc, MediaError):
            raise
        raise MediaError("Encoder or output decoder failed") from exc
    delta = ImageStat.Stat(ImageChops.difference(image, result))
    rms = math.sqrt(sum(value * value for value in delta.rms) / len(delta.rms))
    psnr = round(20.0 * math.log10(255.0 / rms), 3) if rms else 99.0
    if psnr < min_psnr:
        raise MediaError(f"Quality gate rejected output: PSNR {psnr:.2f} below {min_psnr:.2f}")
    return Conversion(
        content=output, codec=codec, width=width, height=height,
        source_width=probe.size[0], source_height=probe.size[1],
        psnr=psnr, source_sha256=_digest(source), output_sha256=_digest(output),
        lossless=codec == "png" or (codec == "webp" and quality == 100),
        quality=quality,
    )
