# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Pixel-level adjustments. No model call: these are the "cheap" controls.

Filters are always applied to the last model-generated image (the *base*), never
stacked on a previous filter result, so any slider state is reproducible.
"""

import io
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps


@dataclass(frozen=True)
class Gauge:
    label: str
    min: int
    max: int
    step: int
    neutral: int


GAUGES: dict[str, Gauge] = {
    "warmth": Gauge("Warmth", -100, 100, 5, 0),
    "exposure": Gauge("Exposure", -100, 100, 5, 0),
    "contrast": Gauge("Contrast", -100, 100, 5, 0),
    "saturation": Gauge("Saturation", -100, 100, 5, 0),
    "sharpness": Gauge("Sharpness", 0, 100, 5, 0),
    "vignette": Gauge("Vignette", 0, 100, 5, 0),
    "grain": Gauge("Film grain", 0, 100, 5, 0),
    "blur": Gauge("Soft focus", 0, 100, 5, 0),
}


def neutral_params() -> dict[str, int]:
    return {k: g.neutral for k, g in GAUGES.items()}


def normalize(params: dict) -> dict[str, int]:
    """Clamp and coerce values coming back from the UI (they arrive as JSON)."""
    out = neutral_params()
    for name, gauge in GAUGES.items():
        if name in params and params[name] not in (None, ""):
            out[name] = max(gauge.min, min(gauge.max, round(float(params[name]))))
    return out


def is_neutral(params: dict) -> bool:
    return normalize(params) == neutral_params()


def _warmth(img: Image.Image, amount: int) -> Image.Image:
    # Shift red up / blue down (or the reverse) by up to ~12%.
    k = amount / 100 * 0.12
    r, g, b = img.split()
    r = r.point(lambda v: min(255, max(0, round(v * (1 + k)))))
    b = b.point(lambda v: min(255, max(0, round(v * (1 - k)))))
    return Image.merge("RGB", (r, g, b))


def _vignette(img: Image.Image, amount: int) -> Image.Image:
    w, h = img.size
    mask = Image.new("L", (w, h), 0)
    inset = int(min(w, h) * 0.08)
    ImageDraw.Draw(mask).ellipse((inset, inset, w - inset, h - inset), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(min(w, h) * 0.15))
    dark = ImageEnhance.Brightness(img).enhance(1 - amount / 100 * 0.7)
    return Image.composite(img, dark, mask)


def _grain(img: Image.Image, amount: int) -> Image.Image:
    noise = Image.effect_noise(img.size, 20 + amount * 0.6).convert("RGB")
    return Image.blend(img, noise, amount / 100 * 0.18)


def apply(image_bytes: bytes, params: dict) -> bytes:
    p = normalize(params)
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes))).convert("RGB")

    if p["warmth"]:
        img = _warmth(img, p["warmth"])
    if p["exposure"]:
        img = ImageEnhance.Brightness(img).enhance(1 + p["exposure"] / 100 * 0.6)
    if p["contrast"]:
        img = ImageEnhance.Contrast(img).enhance(1 + p["contrast"] / 100 * 0.6)
    if p["saturation"]:
        img = ImageEnhance.Color(img).enhance(1 + p["saturation"] / 100)
    if p["sharpness"]:
        img = ImageEnhance.Sharpness(img).enhance(1 + p["sharpness"] / 100 * 2)
    if p["blur"]:
        img = img.filter(ImageFilter.GaussianBlur(p["blur"] / 100 * 6))
    if p["vignette"]:
        img = _vignette(img, p["vignette"])
    if p["grain"]:
        img = _grain(img, p["grain"])

    out = io.BytesIO()
    img.save(out, "JPEG", quality=92)
    return out.getvalue()
