"""Live filter preview that runs in the host, Gemini Enterprise included.

GE has no CSS `filter` style and runs no custom client code, but it does evaluate
`formatString` in an Image url, again on every data model change. So the card's image
is an SVG data URL whose CSS reads the slider values (`${/filters/exposure}`): drag a
gauge and the host re-renders it, with no round trip. Apply still renders with Pillow.

The SVG mirrors app/studio/filters.py on the unfiltered base image:
- warmth and sharpness are linear blends in Pillow, so a pre-filtered copy faded in
  with `opacity` matches them;
- exposure, contrast, saturation and blur are CSS filter functions;
- vignette and grain are overlays, as in the old local preview.
When the sliders sit at the applied values, the real render is shown on top, so what
you see after Apply is the Pillow output, not the approximation.

The images themselves go in the data model (`${/live/base}`), not in the template:
the A2UI reference renderer caps formatString templates at 10,000 characters.
"""

import base64
import io
from typing import Any

from PIL import Image, ImageOps

from app.studio.filters import GAUGES

DATA_KEY = "live"  # data model key for the embedded images


def encode(image_bytes: bytes, side: int) -> tuple[str, int, int, float]:
    """A JPEG of at most `side` px as base64, with its size and mean grey (0-1)."""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes))).convert("RGB")
    img.thumbnail((side, side))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    # The same mean ImageEnhance.Contrast pivots on.
    hist = img.convert("L").histogram()
    mean = sum(i * n for i, n in enumerate(hist)) / (img.width * img.height)
    return (
        base64.b64encode(buf.getvalue()).decode(),
        img.width,
        img.height,
        max(int(mean + 0.5), 1) / 255,
    )


def _sharpen_kernel(amount: int) -> str:
    # PIL's ImageEnhance.Sharpness blends with ImageFilter.SMOOTH; as one 3x3 kernel.
    f = 1 + amount / 100 * 2
    edge, center = (1 - f) / 13, f + (1 - f) * 5 / 13
    return " ".join(f"{v:.4f}" for v in [edge] * 4 + [center] + [edge] * 4)


def _v(name: str) -> str:
    return "${/filters/" + name + "}"


def _unchanged(applied: dict[str, Any]) -> str:
    """CSS opacity: 1 while every slider is at its applied value, else 0."""
    diffs = " + ".join(
        f"max({_v(k)} - {applied[k]}, {applied[k]} - {_v(k)})" for k in GAUGES
    )
    return f"calc(1 - min(1, {diffs}))"


def live_url(
    base: tuple[str, int, int, float],
    applied: dict[str, Any],
    current: tuple[str, int, int, float] | None = None,
) -> dict[str, Any]:
    """formatString call for the Image url. `current` is the applied render, if any;
    put data(base, current) under DATA_KEY in the surface's data model."""
    _, w, h, mean = base
    warm = 0.12  # filters._warmth at full strength
    jpeg = "data:image/jpeg;base64,"
    # Pillow's contrast pivots on the image's mean grey m, CSS's on 0.5;
    # brightness(1/2m) contrast(k) brightness(2m) is contrast k around m. Browsers clip
    # between filter steps, so the pivot shift only switches on while contrast is moved
    # (`on` is 0 or 1). Exposure commutes with it, so it folds into the last step.
    c = _v("contrast")
    on = f"min(1, max({c}, -1 * {c}))"  # not -{c}: "--50" is not a number
    chain = (
        f"brightness(calc(1 + {1 / (2 * mean) - 1:.4f} * {on})) "
        f"contrast(calc(1 + {c} * 0.006)) "
        f"brightness(calc((1 + {2 * mean - 1:.4f} * {on}) * (1 + {_v('exposure')} * 0.006))) "
        f"saturate(calc(1 + {_v('saturation')} * 0.01)) "
        f"blur(calc({_v('blur')} * 0.06px))"
    )
    svg = (
        f"<svg xmlns='http://www.w3.org/2000/svg' width='{w}' height='{h}' viewBox='0 0 {w} {h}'>"
        "<defs>"
        f"<image id='b' href='{jpeg}${{/{DATA_KEY}/base}}' width='{w}' height='{h}'/>"
        "<filter id='warm' color-interpolation-filters='sRGB'><feColorMatrix type='matrix' "
        f"values='{1 + warm} 0 0 0 0 0 1 0 0 0 0 0 {1 - warm} 0 0 0 0 0 1 0'/></filter>"
        "<filter id='cool' color-interpolation-filters='sRGB'><feColorMatrix type='matrix' "
        f"values='{1 - warm} 0 0 0 0 0 1 0 0 0 0 0 {1 + warm} 0 0 0 0 0 1 0'/></filter>"
        "<filter id='sharp' color-interpolation-filters='sRGB'><feConvolveMatrix order='3' "
        f"kernelMatrix='{_sharpen_kernel(100)}' divisor='1' edgeMode='duplicate' preserveAlpha='true'/></filter>"
        "<filter id='noise'><feTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2' "
        "stitchTiles='stitch'/><feColorMatrix type='saturate' values='0'/></filter>"
        "<radialGradient id='vig'><stop offset='65%' stop-opacity='0'/>"
        "<stop offset='100%' stop-opacity='0.7'/></radialGradient>"
        "<g id='t'><use href='#b'/>"
        f"<use href='#b' filter='url(#warm)' style='opacity:clamp(0, calc({_v('warmth')} / 100), 1)'/>"
        f"<use href='#b' filter='url(#cool)' style='opacity:clamp(0, calc({_v('warmth')} / -100), 1)'/>"
        "</g></defs>"
        f"<g style='filter:{chain}'><use href='#t'/>"
        f"<use href='#t' filter='url(#sharp)' style='opacity:calc({_v('sharpness')} / 100)'/></g>"
        f"<rect width='{w}' height='{h}' fill='url(#vig)' style='opacity:calc({_v('vignette')} / 100)'/>"
        f"<rect width='{w}' height='{h}' filter='url(#noise)' "
        f"style='mix-blend-mode:overlay;opacity:calc({_v('grain')} * 0.0036)'/>"
    )
    if current:
        svg += (
            f"<image href='{jpeg}${{/{DATA_KEY}/current}}' width='{w}' height='{h}' "
            f"preserveAspectRatio='none' style='opacity:{_unchanged(applied)}'/>"
        )
    svg += "</svg>"
    url = "data:image/svg+xml;utf8," + svg.replace("%", "%25").replace("#", "%23")
    return {"call": "formatString", "args": {"value": url}}


def data(
    base: tuple[str, int, int, float],
    current: tuple[str, int, int, float] | None = None,
) -> dict[str, str]:
    return {"base": base[0], **({"current": current[0]} if current else {})}
