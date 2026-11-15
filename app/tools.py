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

"""ADK tools. Each one does the media work, then returns the next studio surface.

Session state holds the asset lineage:
  studio:assets   {asset_id: {kind, uri, mime, parent, base, op, params, model, prompt}}
  studio:current  asset_id on screen
  studio:critique {base_asset_id: Critique as dict}
  studio:asked            {base_asset_id: [question, ...]} already shown, so "ask me
                          more" never repeats itself
  studio:image_tier       "lite" | "nb2" | "pro", the model picked by the user
  studio:gauges           gauges the user asked for, replacing the critique's picks
                          on every card until reset (change_gauges)
  studio:pending_filters  slider values the user moved but didn't apply, sent along
                          with a refine/answer click (set by app/executor.py)

Assets are immutable: every tool writes a new file under a new id and never touches
an old one, so any version can be put back on screen (revert_to_version) for free.

Every tool ends the turn (skip_summarization): the surface *is* the answer, so there
is no second LLM call to paraphrase it.
"""

import logging
import time
from typing import Any

from google.adk.tools import ToolContext

from app import config
from app.studio import filters, live_preview, media
from app.studio.assets import (
    model_input,
    new_asset_id,
    object_name,
    preview,
    store,
)
from app.studio.critique import (
    Critique,
    GuidingQuestions,
    critique,
    guiding_questions,
)
from app.studio.surface import StudioView, render

logger = logging.getLogger(__name__)

A2UI_KEY = "a2ui"
ASSETS = "studio:assets"
CURRENT = "studio:current"
CRITIQUES = "studio:critique"
PENDING = "studio:pending_filters"
TIER = "studio:image_tier"
GAUGE_PICK = "studio:gauges"
ASKED = "studio:asked"

# Short chip labels for the version picker.
_OP_LABEL = {
    "generate": "original",
    "edit": "edit",
    "adjust": "filters",
    "upscale": "upscale",
    "reframe": "reframe",
    "regenerate": "new take",
    "animate": "clip",
}
MAX_VERSION_CHIPS = 10

_MODEL_LABEL = {
    "gemini-3.1-flash-lite-image": "Nano Banana 2 Lite",
    "gemini-3.1-flash-image": "Nano Banana 2",
    "gemini-3-pro-image": "Nano Banana Pro",
    "gemini-omni-1.1-flash-preview": "Omni",
    "pillow": "Pillow filters",
}


def _assets(ctx: ToolContext) -> dict[str, dict]:
    return dict(ctx.state.get(ASSETS) or {})


def _current(ctx: ToolContext) -> dict | None:
    assets = _assets(ctx)
    return assets.get(ctx.state.get(CURRENT) or "")


def _record(ctx: ToolContext, kind: str, data: bytes, mime: str, **meta: Any) -> dict:
    asset_id = new_asset_id(kind)
    assets = _assets(ctx)
    version = len(assets) + 1
    uri = store().save(
        object_name(ctx.session.id, version, meta["op"], asset_id), data, mime
    )
    entry = {
        "id": asset_id,
        "kind": kind,
        "uri": uri,
        "mime": mime,
        "version": version,
        **meta,
    }
    if kind == "image" and (small := preview(data)):
        name = object_name(ctx.session.id, version, meta["op"], asset_id)
        entry["preview_uri"] = store().save(f"{name}-preview", small, "image/jpeg")
    entry.setdefault("base", asset_id)
    assets[asset_id] = entry
    ctx.state[ASSETS] = assets
    ctx.state[CURRENT] = asset_id
    return entry


def _image_model(ctx: ToolContext, requested: str = "") -> str:
    """The model for the user's tier; a valid `requested` tier becomes the new pick."""
    if requested in config.IMAGE_TIERS:
        ctx.state[TIER] = requested
    tier = ctx.state.get(TIER) or config.DEFAULT_IMAGE_TIER
    return config.IMAGE_TIERS.get(tier, config.IMAGE_TIERS[config.DEFAULT_IMAGE_TIER])


def _aspect_error(aspect: str) -> dict | None:
    if aspect in config.ASPECT_RATIOS:
        return None
    return {
        "status": "error",
        "message": f"Unsupported aspect ratio {aspect}; use one of "
        + ", ".join(config.ASPECT_RATIOS),
    }


def _lineage(entry: dict) -> str:
    parts = [f"v{entry['version']}", _MODEL_LABEL.get(entry["model"], entry["model"])]
    if entry.get("op") and entry["op"] != "generate":
        detail = (
            entry.get("instruction") or entry.get("motion") or entry.get("size") or ""
        )
        detail = detail if len(detail) <= 48 else detail[:47].rstrip() + "…"
        parts.append(f"{entry['op']}: {detail}" if detail else entry["op"])
    if entry.get("seconds") is not None:
        parts.append(f"{entry['seconds']:.1f}s")
    return " · ".join(parts)


def _shown(entry: dict) -> str:
    """URL for the card: the light preview when there is one."""
    return store().url(entry.get("preview_uri") or entry["uri"])


def _for_model(entry: dict, full: bool = True) -> bytes:
    """Image bytes for a model call. Critique and questions only need the preview."""
    if not full and entry.get("preview_uri"):
        return store().load(entry["preview_uri"])
    return model_input(store().load(entry["uri"]))


def _live_preview(entry: dict, base: dict) -> dict[str, Any]:
    """StudioView fields for an image that previews slider moves (live_preview.py)."""
    if entry["kind"] != "image":
        return {}

    def encoded(e: dict) -> tuple[str, int, int, float]:
        data = store().load(e.get("preview_uri") or e["uri"])
        return live_preview.encode(data, config.LIVE_PREVIEW_SIDE)

    applied = filters.normalize(entry.get("params") or {})
    base_img = encoded(base)
    current = encoded(entry) if entry["id"] != base["id"] else None
    return {
        "live_url": live_preview.live_url(base_img, applied, current),
        "live_data": live_preview.data(base_img, current),
    }


def _no_asset() -> dict:
    return {
        "status": "error",
        "message": "There is no asset yet. Ask the user what to create.",
    }


async def _critique_for(ctx: ToolContext, base: dict) -> Critique:
    critiques = dict(ctx.state.get(CRITIQUES) or {})
    if base["id"] in critiques:
        return Critique.model_validate(critiques[base["id"]])
    result = await critique(_for_model(base, full=False), base.get("prompt", ""))
    critiques[base["id"]] = result.model_dump()
    ctx.state[CRITIQUES] = critiques
    return result


async def _respond(
    ctx: ToolContext,
    entry: dict,
    summary: str,
    badge: str | None = None,
    mode: str = "studio",
    questions: GuidingQuestions | None = None,
) -> dict:
    ctx.actions.skip_summarization = True
    assets = _assets(ctx)
    base = (
        assets[entry["base"]] if entry["kind"] == "image" else assets[entry["parent"]]
    )
    crit = await _critique_for(ctx, base)
    if questions:
        # model_copy skips validation: the questions card may show more than the studio.
        crit = crit.model_copy(
            update={
                "questions": questions.questions,
                "open_question": questions.open_question,
            }
        )
    if picked := ctx.state.get(GAUGE_PICK):
        crit = crit.model_copy(update={"gauges": picked})
    parent = assets.get(entry.get("parent") or "")
    history = sorted(assets.values(), key=lambda a: a["version"])[-MAX_VERSION_CHIPS:]
    live = _live_preview(entry, base) if mode == "studio" else {}
    view = StudioView(
        **live,
        asset_id=entry["id"],
        kind=entry["kind"],
        url=_shown(entry),
        full_url=store().url(entry["uri"]) if entry.get("preview_uri") else None,
        title=entry.get("title") or crit.subject_type.title(),
        lineage=_lineage(entry),
        critique=crit,
        before_url=_shown(parent)
        if parent and parent["kind"] == "image" and entry["kind"] == "image"
        else None,
        filters=filters.normalize(entry.get("params") or {}),
        badge=badge,
        can_upscale=entry.get("size") != config.UPSCALE_SIZES[-1],
        version=entry["version"],
        aspect=entry.get("aspect") or config.ASPECT_RATIOS[0],
        tier=ctx.state.get(TIER) or config.DEFAULT_IMAGE_TIER,
        mode=mode,
        intro=questions.intro if questions else None,
        versions=[
            (a["version"], a.get("size") or _OP_LABEL.get(a["op"], a["op"]))
            for a in history
        ],
    )
    return {
        "status": "ok",
        "summary": summary,
        "asset_id": entry["id"],
        A2UI_KEY: render(view),
    }


def _adjust(ctx: ToolContext, current: dict, params: dict) -> dict:
    """Renders `params` on the current image's base and records it as a new version."""
    base = _assets(ctx)[current["base"]]
    t = time.monotonic()
    data = filters.apply(store().load(base["uri"]), params)
    return _record(
        ctx,
        "image",
        data,
        "image/jpeg",
        op="adjust",
        model="pillow",
        params=params,
        prompt=base.get("prompt"),
        title=base.get("title"),
        aspect=base.get("aspect"),
        parent=base["id"],
        base=base["id"],
        seconds=time.monotonic() - t,
    )


def _apply_pending(ctx: ToolContext, current: dict) -> dict | None:
    """Applies slider values sent along with a refine/answer click, if they changed.

    Returns the new adjusted version, or None when there was nothing to apply.
    """
    pending = ctx.state.get(PENDING)
    ctx.state[PENDING] = None
    if not pending or current["kind"] != "image":
        return None
    applied = filters.normalize(current.get("params") or {})
    params = filters.normalize({**applied, **pending})
    if params == applied:
        return None
    return _adjust(ctx, current, params)


async def create_image(
    prompt: str,
    aspect_ratio: str,
    title: str,
    tool_context: ToolContext,
    model: str = "",
) -> dict:
    """Generates a new image from a detailed prompt and opens the studio for it.

    Args:
        prompt: A detailed, visual image prompt (subject, setting, lighting, lens, style).
        aspect_ratio: One of "1:1", "4:5", "3:4", "16:9", "9:16".
        title: A 2-4 word title for the asset, e.g. "Ceramic mug hero shot".
        model: Only if the user names one: "lite" (fast), "nb2" (Nano Banana 2) or
            "pro" (Nano Banana Pro, best quality). Leave empty otherwise.
    """
    if err := _aspect_error(aspect_ratio):
        return err
    t = time.monotonic()
    data, mime, model = await media.generate(
        prompt, aspect_ratio, _image_model(tool_context, model)
    )
    entry = _record(
        tool_context,
        "image",
        data,
        mime,
        op="generate",
        model=model,
        prompt=prompt,
        title=title,
        aspect=aspect_ratio,
        parent=None,
        seconds=time.monotonic() - t,
    )
    return await _respond(
        tool_context,
        entry,
        f"Here's a first take on {title.lower()}. I had a look at it "
        "and put the controls that matter for this image below.",
    )


async def refine_image(
    instruction: str, tool_context: ToolContext, model: str = ""
) -> dict:
    """Applies a meaning-level edit to the current image with the image model.

    Use for anything that changes content: objects, background, wardrobe, lighting setup,
    composition, style, text in the image.

    Args:
        instruction: A concrete edit instruction, e.g. "replace the background with a sunlit kitchen".
        model: Only if the user names one: "lite", "nb2" or "pro". Leave empty otherwise.
    """
    current = _current(tool_context)
    if not current:
        return _no_asset()
    adjusted = _apply_pending(tool_context, current)
    current = adjusted or current
    # Edit what the user is looking at, so any filters they applied are kept.
    source = (
        current
        if current["kind"] == "image"
        else _assets(tool_context)[current["parent"]]
    )
    t = time.monotonic()
    data, mime, model = await media.edit(
        _for_model(source), instruction, _image_model(tool_context, model)
    )
    entry = _record(
        tool_context,
        "image",
        data,
        mime,
        op="edit",
        model=model,
        instruction=instruction,
        prompt=f"{source.get('prompt', '')}. Edit: {instruction}",
        title=source.get("title"),
        aspect=source.get("aspect"),
        parent=source["id"],
        seconds=time.monotonic() - t,
    )
    note = (
        f" Your pending slider changes were applied first (v{adjusted['version']})."
        if adjusted
        else ""
    )
    return await _respond(
        tool_context,
        entry,
        f"Done: {instruction.rstrip('.')}.{note} Compare with Before, and the controls "
        "have been updated for the new version.",
    )


async def adjust_image(
    tool_context: ToolContext,
    warmth: int = 0,
    exposure: int = 0,
    contrast: int = 0,
    saturation: int = 0,
    sharpness: int = 0,
    vignette: int = 0,
    grain: int = 0,
    blur: int = 0,
) -> dict:
    """Applies instant pixel adjustments (no image model call) to the current image.

    Use for tone only: warmth, exposure, contrast, saturation, sharpness, vignette,
    grain, blur (-100..100 or 0..100). Values are absolute, not deltas; 0 means
    unchanged. Aspect ratio changes are not filters: use reframe_image.
    """
    current = _current(tool_context)
    if not current or current["kind"] != "image":
        return _no_asset()
    params = filters.normalize(
        {
            "warmth": warmth,
            "exposure": exposure,
            "contrast": contrast,
            "saturation": saturation,
            "sharpness": sharpness,
            "vignette": vignette,
            "grain": grain,
            "blur": blur,
        }
    )
    entry = _adjust(tool_context, current, params)
    return await _respond(
        tool_context,
        entry,
        "Adjusted. Filters always start from the last generated "
        "version, so you can move the sliders freely.",
    )


async def upscale_image(
    tool_context: ToolContext, size: str = "4K", model: str = ""
) -> dict:
    """Re-renders the current image at 2K or 4K with the selected image model
    (Nano Banana 2 or Pro; Lite can't upscale, so it uses Nano Banana 2).

    Args:
        size: "4K" (default) or "2K".
        model: Only when the user names one: "nb2" or "pro".
    """
    current = _current(tool_context)
    if not current or current["kind"] != "image":
        return _no_asset()
    if size.upper() not in config.UPSCALE_SIZES:
        return {
            "status": "error",
            "message": f"Upscale sizes are {' and '.join(config.UPSCALE_SIZES)}.",
        }
    size = size.upper()
    chosen = _image_model(tool_context, model)
    if chosen == config.IMAGE_LITE_MODEL:
        chosen = config.IMAGE_MODEL
    t = time.monotonic()
    data, mime, used, degraded = await media.upscale(
        _for_model(current), current.get("prompt", ""), chosen, size
    )
    entry = _record(
        tool_context,
        "image",
        data,
        mime,
        op="upscale",
        model=used,
        size=size,
        prompt=current.get("prompt"),
        title=current.get("title"),
        aspect=current.get("aspect"),
        parent=current["id"],
        seconds=time.monotonic() - t,
    )
    badge = (
        "Nano Banana Pro was at capacity, so this was rendered with Nano Banana 2."
        if degraded
        else None
    )
    return await _respond(tool_context, entry, f"Upscaled to {size}.", badge=badge)


async def reframe_image(
    aspect: str, tool_context: ToolContext, model: str = ""
) -> dict:
    """Recomposes the current image for another aspect ratio with the image model,
    keeping the same content (extends or trims the scene; not a crop).

    Args:
        aspect: One of "1:1", "4:5", "3:4", "16:9", "9:16".
        model: Only if the user names one: "lite", "nb2" or "pro". Leave empty otherwise.
    """
    current = _current(tool_context)
    if not current or current["kind"] != "image":
        return _no_asset()
    if err := _aspect_error(aspect):
        return err
    current = _apply_pending(tool_context, current) or current
    t = time.monotonic()
    data, mime, used = await media.reframe(
        _for_model(current), aspect, _image_model(tool_context, model)
    )
    entry = _record(
        tool_context,
        "image",
        data,
        mime,
        op="reframe",
        model=used,
        instruction=aspect,
        prompt=current.get("prompt"),
        title=current.get("title"),
        aspect=aspect,
        parent=current["id"],
        seconds=time.monotonic() - t,
    )
    return await _respond(tool_context, entry, f"Reframed to {aspect}.")


async def regenerate_image(
    tool_context: ToolContext, aspect: str = "", model: str = ""
) -> dict:
    """Generates a new take from the current image's prompt (a different image).

    Args:
        aspect: Optional new aspect ratio ("1:1", "4:5", "3:4", "16:9", "9:16");
            empty keeps the current one.
        model: Only if the user names one: "lite", "nb2" or "pro". Leave empty otherwise.
    """
    current = _current(tool_context)
    if not current:
        return _no_asset()
    aspect = aspect or current.get("aspect") or config.ASPECT_RATIOS[0]
    if err := _aspect_error(aspect):
        return err
    t = time.monotonic()
    data, mime, used = await media.generate(
        current.get("prompt") or "", aspect, _image_model(tool_context, model)
    )
    entry = _record(
        tool_context,
        "image",
        data,
        mime,
        op="regenerate",
        model=used,
        prompt=current.get("prompt"),
        title=current.get("title"),
        aspect=aspect,
        parent=current["id"],
        seconds=time.monotonic() - t,
    )
    return await _respond(
        tool_context,
        entry,
        "Here's a new take on the same brief. The previous one is still in the "
        "version list.",
    )


async def animate_image(motion: str, tool_context: ToolContext) -> dict:
    """Turns the current image into a ~4 second clip with Omni (takes about 30 seconds).

    Args:
        motion: Camera or subject motion, e.g. "steam rises, slow push-in".
    """
    current = _current(tool_context)
    if not current:
        return _no_asset()
    source = (
        current
        if current["kind"] == "image"
        else _assets(tool_context)[current["parent"]]
    )
    t = time.monotonic()
    data, mime, model = await media.animate(_for_model(source), motion)
    entry = _record(
        tool_context,
        "video",
        data,
        mime,
        op="animate",
        model=model,
        motion=motion,
        prompt=source.get("prompt"),
        title=source.get("title"),
        parent=source["id"],
        base=source["id"],
        seconds=time.monotonic() - t,
    )
    return await _respond(tool_context, entry, f"Here's the clip ({motion}).")


async def show_image(tool_context: ToolContext) -> dict:
    """Shows the studio for the current still image again (e.g. after a clip)."""
    current = _current(tool_context)
    if not current:
        return _no_asset()
    if current["kind"] == "video":
        tool_context.state[CURRENT] = current["parent"]
        current = _assets(tool_context)[current["parent"]]
    return await _respond(tool_context, current, "Back to the still.")


async def change_gauges(
    tool_context: ToolContext,
    show: list[str] | None = None,
    hide: list[str] | None = None,
    only: bool = False,
    reset: bool = False,
) -> dict:
    """Changes which filter gauges the studio shows, without rendering anything.

    Gauges: warmth, exposure, contrast, saturation, sharpness, vignette, grain, blur.
    show adds gauges and hide removes them, starting from the ones on screen; with
    only=True, show is the whole set. reset=True goes back to the gauges picked for
    each image. The choice sticks for this chat, new images included.
    """
    current = _current(tool_context)
    if not current:
        return _no_asset()
    unknown = sorted((set(show or []) | set(hide or [])) - set(filters.GAUGES))
    if unknown:
        return {
            "status": "error",
            "message": f"Unknown gauges: {', '.join(unknown)}. Available: "
            f"{', '.join(filters.GAUGES)}.",
        }
    image = (
        current
        if current["kind"] == "image"
        else _assets(tool_context)[current["parent"]]
    )
    if reset:
        tool_context.state[GAUGE_PICK] = None
        tool_context.state[CURRENT] = image["id"]
        return await _respond(
            tool_context, image, "Back to the gauges picked for this image."
        )
    if only:
        shown = set(show or [])
    else:
        on_screen = (
            tool_context.state.get(GAUGE_PICK)
            or (
                await _critique_for(tool_context, _assets(tool_context)[image["base"]])
            ).gauges
        )
        shown = (set(on_screen) | set(show or [])) - set(hide or [])
    if not shown:
        return {
            "status": "error",
            "message": "That would leave no gauges. Ask which ones to keep.",
        }
    tool_context.state[GAUGE_PICK] = [g for g in filters.GAUGES if g in shown]
    tool_context.state[CURRENT] = image["id"]
    return await _respond(tool_context, image, "Gauges updated.")


async def ask_guiding_questions(tool_context: ToolContext, focus: str = "") -> dict:
    """Asks the user new guiding questions about the current image, without
    rendering anything. Use when the user wants help deciding what to change
    ("ask me more questions", "help me think about the lighting", "what should I
    consider?"), not when they already said what they want.

    Args:
        focus: Optional area to explore, e.g. "lighting", "background", "mood".
    """
    current = _current(tool_context)
    if not current:
        return _no_asset()
    assets = _assets(tool_context)
    image = current if current["kind"] == "image" else assets[current["parent"]]
    base = assets[image["base"]]
    crit = await _critique_for(tool_context, base)
    asked = dict(tool_context.state.get(ASKED) or {})
    seen = asked.get(base["id"]) or [q.question for q in crit.questions]
    fresh = await guiding_questions(
        _for_model(image, full=False), base.get("prompt") or "", focus, seen
    )
    asked[base["id"]] = seen + [q.question for q in fresh.questions]
    tool_context.state[ASKED] = asked
    # The studio shows the latest questions from now on too, as many as it has room for.
    crit = crit.model_copy(
        update={
            "questions": fresh.questions[: len(crit.questions)],
            "open_question": fresh.open_question,
        }
    )
    critiques = dict(tool_context.state.get(CRITIQUES) or {})
    critiques[base["id"]] = crit.model_dump()
    tool_context.state[CRITIQUES] = critiques
    if image is not current:
        tool_context.state[CURRENT] = image["id"]
    return await _respond(
        tool_context,
        image,
        "Pick what you like (no need to answer everything), then apply them together.",
        mode="questions",
        questions=fresh,
    )


async def revert_to_version(tool_context: ToolContext, version: int = 0) -> dict:
    """Puts an earlier version back on screen (nothing is regenerated or lost).

    Later edits branch from it; newer versions stay available.

    Args:
        version: The version number, e.g. 2 for "go back to v2". Use 0 for "undo",
            which returns to the version the current one was made from.
    """
    current = _current(tool_context)
    if not current:
        return _no_asset()
    assets = _assets(tool_context)
    if version:
        target = next((a for a in assets.values() if a["version"] == version), None)
        if not target:
            return {
                "status": "error",
                "message": f"There is no v{version}; versions go from v1 to "
                f"v{max(a['version'] for a in assets.values())}.",
            }
    else:
        target = assets.get(current.get("parent") or "")
        if not target:
            return {"status": "error", "message": "This is already the first version."}
    tool_context.state[CURRENT] = target["id"]
    return await _respond(
        tool_context,
        target,
        f"Back to v{target['version']}. New changes will build on this version; "
        "the later ones are still in the version list.",
    )


STUDIO_TOOLS = [
    create_image,
    refine_image,
    adjust_image,
    upscale_image,
    animate_image,
    show_image,
    revert_to_version,
    change_gauges,
    reframe_image,
    regenerate_image,
    ask_guiding_questions,
]
