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

"""Model-backed media operations: the "meaning-level" controls."""

import asyncio
import base64
import functools
import io
import logging

from google import genai
from google.genai import errors, types
from PIL import Image

from app import config

logger = logging.getLogger(__name__)


@functools.cache
def client() -> genai.Client:
    # Transient 429/5xx retries are handled by the SDK, not by hand-rolled backoff.
    return genai.Client(
        http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=3))
    )


class NoImageReturned(RuntimeError):
    pass


def _first_image(response: types.GenerateContentResponse) -> tuple[bytes, str]:
    for candidate in response.candidates or []:
        for part in (candidate.content.parts if candidate.content else None) or []:
            if part.inline_data and part.inline_data.data:
                return part.inline_data.data, part.inline_data.mime_type or "image/png"
    reason = (
        response.candidates[0].finish_reason if response.candidates else "no candidates"
    )
    raise NoImageReturned(f"Image model returned no image ({reason}).")


async def _image_call(
    model: str, contents: list, aspect_ratio: str | None, image_size: str | None = None
):
    config_ = types.GenerateContentConfig(
        response_modalities=["IMAGE"],
        image_config=types.ImageConfig(
            aspect_ratio=aspect_ratio, image_size=image_size
        ),
    )
    response = await client().aio.models.generate_content(
        model=model, contents=contents, config=config_
    )
    return _first_image(response)


async def generate(
    prompt: str, aspect_ratio: str = "1:1", model: str = config.IMAGE_MODEL
) -> tuple[bytes, str, str]:
    data, mime = await _image_call(model, [prompt], aspect_ratio)
    return data, mime, model


async def edit(
    image: bytes, instruction: str, model: str = config.IMAGE_MODEL
) -> tuple[bytes, str, str]:
    """Image-conditioned edit that keeps the subject and composition."""
    contents = [
        types.Part.from_bytes(data=image, mime_type=_sniff(image)),
        f"Edit this image: {instruction}. Keep the subject, identity, composition and "
        "everything not mentioned exactly as it is.",
    ]
    data, mime = await _image_call(model, contents, None)
    return data, mime, model


async def reframe(
    image: bytes, aspect_ratio: str, model: str = config.IMAGE_MODEL
) -> tuple[bytes, str, str]:
    """Recomposes the same image for another frame (extends or trims the scene)."""
    contents = [
        types.Part.from_bytes(data=image, mime_type=_sniff(image)),
        f"Recompose this exact image for a {aspect_ratio} frame. Keep the same subject, "
        "identity, style, lighting and colors. Extend or trim the surroundings "
        "naturally; never crop into or distort the subject.",
    ]
    data, mime = await _image_call(model, contents, aspect_ratio)
    return data, mime, model


async def upscale(
    image: bytes, prompt: str, model: str, size: str
) -> tuple[bytes, str, str, bool]:
    """Re-render at `size` (2K or 4K). Pro falls back to Nano Banana 2 on quota errors.

    Returns (bytes, mime, model_used, degraded).
    """
    contents = [
        types.Part.from_bytes(data=image, mime_type=_sniff(image)),
        "Re-render this exact image at higher fidelity: same subject, composition, "
        f"lighting and colors, with finer detail and texture. Original brief: {prompt}",
    ]
    try:
        data, mime = await _image_call(model, contents, None, image_size=size)
        return data, mime, model, False
    except errors.APIError as e:
        if e.code != 429 or model != config.IMAGE_PRO_MODEL:
            raise
        logger.warning(
            "Pro image model rate-limited; falling back to %s", config.IMAGE_MODEL
        )
        data, mime = await _image_call(
            config.IMAGE_MODEL, contents, None, image_size=size
        )
        return data, mime, config.IMAGE_MODEL, True


def _animate_sync(image: bytes, motion: str) -> bytes:
    jpeg = io.BytesIO()
    Image.open(io.BytesIO(image)).convert("RGB").save(jpeg, "JPEG", quality=90)
    interaction = client().interactions.create(
        model=config.VIDEO_MODEL,
        input=[
            {
                "type": "image",
                "data": base64.b64encode(jpeg.getvalue()).decode(),
                "mime_type": "image/jpeg",
            },
            {
                "type": "text",
                "text": f"Animate this image into a short video (about 4 seconds): {motion}. "
                "Keep the subject identical to the image. No text overlays.",
            },
        ],
    )
    for step in interaction.model_dump(mode="json", exclude_none=True).get("steps", []):
        if step.get("type") != "model_output":
            continue
        for content in step.get("content", []):
            if content.get("type") == "video" and content.get("data"):
                return base64.b64decode(content["data"])
    raise RuntimeError(f"Omni returned no video (status={interaction.status}).")


async def animate(image: bytes, motion: str) -> tuple[bytes, str, str]:
    data = await asyncio.to_thread(_animate_sync, image, motion)
    return data, "video/mp4", config.VIDEO_MODEL


def _sniff(data: bytes) -> str:
    return "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"


def thumbnail_jpeg(image: bytes, max_side: int = 768) -> bytes:
    img = Image.open(io.BytesIO(image)).convert("RGB")
    img.thumbnail((max_side, max_side))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    return out.getvalue()
