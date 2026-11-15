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

"""Renders a studio state into A2UI v0.9 messages for the Gemini Enterprise catalog.

The LLM never writes A2UI JSON. The critique picks the controls (structured output),
and this module renders them deterministically, then validates the result against
the GE composite catalog before anything is sent. A surface that fails validation
is a bug in this file, not a model hallucination to retry.
"""

import functools
import pathlib
import uuid
from dataclasses import dataclass, field
from typing import Any

from a2ui.schema.catalog import A2uiCatalog, CatalogConfig

from app import config
from app.studio import live_preview
from app.studio.critique import Critique
from app.studio.filters import GAUGES

CATALOG_PATH = (
    pathlib.Path(__file__).parent.parent
    / "catalog"
    / "gemini_enterprise_composite_catalog.json"
)
VERSION = "v0.9"

# Action names. The executor turns each into an agent turn; see app/executor.py.
# refine and answers also carry the slider values (context.<gauge>) so filters moved
# but not applied yet go along with the edit; see PENDING_FILTER_KEYS. Every action
# that calls the image model carries context.model, the tier picked in the toggle.
REFINE = "refine"  # meaning-level edit: context.instruction
ADJUST = "adjust"  # pixel filters: context.<gauge>
REFRAME = "reframe"  # same image, new frame: context.aspect
REGENERATE = "regenerate"  # new take from the prompt: context.aspect
UPSCALE = "upscale"  # context.size, context.model
ANIMATE = "animate"  # context.motion
BACK_TO_IMAGE = "back_to_image"
START_OVER = "start_over"
REVERT = "revert"  # context.version
QUESTIONS = "questions"  # more guiding questions, no render
ANSWERS = "answers"  # picked chips + note: context.answer_<id>, context.note

PENDING_FILTER_KEYS = tuple(GAUGES)
TIER_LABELS = {"lite": "NB2 Lite", "nb2": "Nano Banana 2", "pro": "Pro"}


@functools.cache
def catalog() -> A2uiCatalog:
    return A2uiCatalog.from_config(
        CatalogConfig.from_path("gemini_enterprise", str(CATALOG_PATH)), version="0.9"
    )


class InvalidSurface(ValueError):
    pass


@dataclass
class StudioView:
    """Everything the renderer needs. Built by app/tools.py from session state."""

    asset_id: str
    kind: str  # "image" | "video"
    url: str  # what the card shows (a preview for 2K/4K images)
    title: str
    lineage: str  # e.g. "v3 · Nano Banana 2 · edit: warmer light"
    critique: Critique | None = None
    before_url: str | None = None
    full_url: str | None = None  # the full-resolution file, when url is a preview
    filters: dict[str, Any] = field(default_factory=dict)
    badge: str | None = None  # e.g. "Pro was busy, rendered with Flash"
    can_upscale: bool = True
    live_url: dict | None = None  # previews the gauges in img-current (live_preview.py)
    live_data: dict[str, str] = field(default_factory=dict)  # the images it reads
    version: int = 1
    versions: list[tuple[int, str]] = field(default_factory=list)  # (n, "edit")
    aspect: str = "1:1"  # of the image on screen; preselects the aspect toggle
    tier: str = config.DEFAULT_IMAGE_TIER  # preselects the model toggle
    mode: str = "studio"  # "studio" | "questions" (a lighter card: questions only)
    intro: str | None = None  # questions mode: one line framing the questions


class _Builder:
    def __init__(self) -> None:
        self.components: list[dict[str, Any]] = []

    def add(self, id_: str, component: str, **props: Any) -> str:
        self.components.append({"id": id_, "component": component, **props})
        return id_

    def get(self, id_: str) -> dict[str, Any]:
        return next(c for c in self.components if c["id"] == id_)

    def text(self, id_: str, text: str, hint: str = "body") -> str:
        return self.add(id_, "MaterialText", text=text, usageHint=hint)

    def button(
        self,
        id_: str,
        label: str,
        name: str,
        prompt: str,
        variant: str = "stroked",
        icon: str | None = None,
        **context: Any,
    ) -> str:
        props: dict[str, Any] = {
            "label": label,
            "variant": variant,
            "action": {
                "event": {"name": name, "context": {"prompt": prompt, **context}}
            },
        }
        if variant == "flat":
            props["color"] = "primary"
        if icon:
            props["leadingIcon"] = icon
        return self.add(id_, "MaterialButton", **props)

    def column(self, id_: str, children: list[str], gap: str = "12px") -> str:
        return self.add(
            id_,
            "MaterialColumn",
            children=children,
            align="stretch",
            style={"gap": gap},
        )

    def row(self, id_: str, children: list[str], gap: str = "8px") -> str:
        return self.add(
            id_,
            "MaterialRow",
            children=children,
            align="center",
            style={"gap": gap, "flexWrap": "wrap"},
        )


def _image(b: _Builder, id_: str, url: str | dict, alt: str) -> str:
    # Basic Image, not MaterialImage: GE's MaterialImage only loads allowlisted hosts
    # and blanks signed GCS URLs (about:blank); Image and Video load them.
    return b.add(
        id_, "Image", url=url, description=alt, fit="contain", variant="largeFeature"
    )


def _media(b: _Builder, view: StudioView) -> str:
    if view.kind == "video":
        return b.add("media", "Video", url=view.url)
    media = _image(b, "img-current", view.live_url or view.url, view.title)
    if view.before_url:
        before = _image(b, "img-before", view.before_url, "Previous version")
        media = b.add(
            "media-tabs",
            "MaterialTabs",
            tabs=[
                {"label": "After", "content": media},
                {"label": "Before", "content": before},
            ],
        )
    # Images take no action in GE, so a button under them opens the full file.
    full = b.add(
        "open-full",
        "MaterialButton",
        label="Open full size",
        variant="basic",
        leadingIcon="open_in_new",
        action={
            "functionCall": {
                "call": "openUrl",
                "args": {"url": view.full_url or view.url},
            }
        },
    )
    return b.column("media", [media, full], gap="4px")


def _pending(critique: Critique) -> dict[str, dict[str, str]]:
    """Binds the sliders and the model toggle into an action, so unapplied changes go
    along with it. Every gauge, shown or not: a hidden one keeps its applied value."""
    return {
        **{k: {"path": f"/filters/{k}"} for k in GAUGES},
        "model": {"path": "/render/model"},
    }


def _versions(b: _Builder, view: StudioView) -> list[str]:
    if len(view.versions) < 2:
        return []
    chips = b.add(
        "versions",
        "MaterialChips",
        value={"path": "/version"},
        options=[{"label": f"v{n} {op}", "value": str(n)} for n, op in view.versions],
        action={
            "event": {
                "name": REVERT,
                "context": {
                    "prompt": "Show that version",
                    "version": {"path": "/version"},
                },
            }
        },
    )
    return [chips]


def _quick_fixes(b: _Builder, critique: Critique) -> list[str]:
    if not critique.quick_fixes:
        return []
    buttons = [
        b.button(
            f"fix-{i}",
            fix.label,
            REFINE,
            prompt=fix.label,
            icon="auto_fix_high",
            instruction=fix.instruction,
            **_pending(critique),
        )
        for i, fix in enumerate(critique.quick_fixes)
    ]
    return [b.text("fixes-title", "Quick fixes", "subtitle2"), b.row("fixes", buttons)]


def _questions(b: _Builder, critique: Critique) -> list[str]:
    """Question chips. Picking one only records the choice; an "Apply" button sends
    every pick (and the note) as one edit, so several questions can be answered."""
    ids = []
    for i, q in enumerate(critique.questions):
        ids.append(b.text(f"q{i}-label", q.question, "subtitle2"))
        ids.append(
            b.add(
                f"q{i}",
                "MaterialChips",
                value={"path": f"/answers/{q.id}"},
                options=[{"label": o.label, "value": o.instruction} for o in q.options],
            )
        )
    return ids


def _answers(critique: Critique) -> dict[str, Any]:
    """Context for ANSWERS: the note, every question's pick and unapplied sliders."""
    return {
        "note": {"path": "/note"},
        **{f"answer_{q.id}": {"path": f"/answers/{q.id}"} for q in critique.questions},
        **_pending(critique),
    }


def _more_questions(b: _Builder, label: str, prompt: str) -> str:
    return b.button(
        "more-questions", label, QUESTIONS, prompt=prompt, variant="basic", icon="quiz"
    )


def _questions_card(b: _Builder, view: StudioView) -> list[str]:
    critique = view.critique
    assert critique is not None
    thumb = _image(b, "img-current", view.url, view.title)
    field_ = b.add(
        "note",
        "MaterialInput",
        label=critique.open_question,
        placeholder="Anything else? (optional)",
        value={"path": "/note"},
    )
    apply = b.button(
        "apply-answers",
        "Apply my answers",
        ANSWERS,
        prompt="Apply my answers",
        variant="flat",
        icon="check",
        **_answers(critique),
    )
    more = _more_questions(b, "Different questions", "Ask me different questions")
    back = b.button(
        "back",
        "Back to the studio",
        BACK_TO_IMAGE,
        prompt="Show the studio",
        variant="basic",
        icon="tune",
    )
    return [
        thumb,
        *([b.text("intro", view.intro, "body2")] if view.intro else []),
        *_questions(b, critique),
        field_,
        b.row("answer-actions", [apply, more, back]),
    ]


def _gauges(b: _Builder, critique: Critique) -> list[str]:
    rows = []
    # Registry order, not the critique's: a gauge keeps its place from card to card.
    for name in [n for n in GAUGES if n in critique.gauges]:
        g = GAUGES[name]
        label = b.add(
            f"g-{name}-label", "MaterialText", text=g.label, usageHint="body2", weight=1
        )
        slider = b.add(
            f"g-{name}",
            "MaterialSlider",
            value={"path": f"/filters/{name}"},
            min=g.min,
            max=g.max,
            step=g.step,
            ariaLabel=g.label,
            weight=3,
        )
        rows.append(
            b.add(
                f"g-{name}-row", "MaterialRow", children=[label, slider], align="center"
            )
        )
    apply = b.button(
        "apply-filters",
        "Apply adjustments",
        ADJUST,
        prompt="Apply these adjustments",
        icon="tune",
        # All gauges: adjust_image takes absolute values, so a hidden one left out
        # would be reset to neutral.
        **{k: {"path": f"/filters/{k}"} for k in GAUGES},
    )
    return [
        b.text("gauges-title", "Adjust (instant, no model call)", "subtitle2"),
        *rows,
        apply,
    ]


def _labeled(b: _Builder, id_: str, label: str, control: str) -> str:
    text = b.add(
        f"{id_}-label", "MaterialText", text=label, usageHint="body2", weight=1
    )
    return b.add(f"{id_}-row", "MaterialRow", children=[text, control], align="center")


def _render_settings(b: _Builder) -> list[str]:
    """Model tier and aspect ratio. These cost a model call, so they only take
    effect through Reframe / New take (and the model through any later edit)."""
    model = b.add(
        "r-model",
        "MaterialButtonToggle",
        value={"path": "/render/model"},
        options=[{"label": v, "value": k} for k, v in TIER_LABELS.items()],
        weight=3,
    )
    aspect = b.add(
        "r-aspect",
        "MaterialButtonToggle",
        value={"path": "/render/aspect"},
        options=[{"label": a, "value": a} for a in config.ASPECT_RATIOS],
        weight=3,
    )
    context = {"aspect": {"path": "/render/aspect"}, "model": {"path": "/render/model"}}
    reframe = b.button(
        "reframe",
        "Reframe",
        REFRAME,
        prompt="Reframe this image to the selected ratio",
        icon="crop",
        **context,
    )
    regenerate = b.button(
        "regenerate",
        "New take",
        REGENERATE,
        prompt="Generate a new take",
        icon="autorenew",
        **context,
    )
    return [
        b.text("render-title", "Render (uses the image model)", "subtitle2"),
        _labeled(b, "r-model", "Model", model),
        _labeled(b, "r-aspect", "Aspect", aspect),
        b.row("render-actions", [reframe, regenerate]),
    ]


def _open_question(b: _Builder, critique: Critique) -> list[str]:
    field_ = b.add(
        "note",
        "MaterialInput",
        label=critique.open_question,
        placeholder="Describe a change...",
        value={"path": "/note"},
    )
    # Sends any picked answers too, so a chip choice is never lost.
    send = b.button(
        "note-send",
        "Refine",
        ANSWERS,
        prompt="Refine with my note",
        variant="flat",
        icon="edit",
        **_answers(critique),
    )
    return [field_, send]


def _next_steps(b: _Builder, view: StudioView) -> str:
    buttons = []
    if view.kind == "image":
        if view.can_upscale:
            # Uses the model picked under Render (Lite upscales with Nano Banana 2).
            buttons += [
                b.button(
                    f"upscale-{size.lower()}",
                    f"Upscale {size}",
                    UPSCALE,
                    prompt=f"Upscale to {size}",
                    icon="high_quality",
                    size=size,
                    model={"path": "/render/model"},
                )
                for size in reversed(config.UPSCALE_SIZES)
            ]
        motion = (
            view.critique.motion_ideas[0]
            if view.critique and view.critique.motion_ideas
            else "subtle camera push-in"
        )
        buttons.append(
            b.button(
                "animate",
                "Animate with Omni",
                ANIMATE,
                prompt=f"Animate it: {motion}",
                icon="movie",
                motion=motion,
            )
        )
    else:
        buttons.append(
            b.button(
                "back",
                "Back to the image",
                BACK_TO_IMAGE,
                prompt="Back to the still image",
                icon="image",
            )
        )
    # Client-side function: a signed link to the full file, no round trip.
    buttons.append(
        b.add(
            "download",
            "MaterialButton",
            label="Download",
            variant="stroked",
            leadingIcon="download",
            action={
                "functionCall": {
                    "call": "openUrl",
                    "args": {"url": view.full_url or view.url},
                }
            },
        )
    )
    buttons.append(
        b.button(
            "restart",
            "Start over",
            START_OVER,
            prompt="Start over",
            variant="basic",
            icon="restart_alt",
        )
    )
    return b.row("next", buttons)


def _motion_chips(b: _Builder, view: StudioView) -> list[str]:
    ideas = view.critique.motion_ideas if view.critique else []
    if not ideas:
        return []
    chips = b.add(
        "motion",
        "MaterialChips",
        value={"path": "/motion"},
        options=[{"label": m, "value": m} for m in ideas],
        action={
            "event": {
                "name": ANIMATE,
                "context": {
                    "prompt": "Animate it differently",
                    "motion": {"path": "/motion"},
                },
            }
        },
    )
    return [b.text("motion-title", "Try another motion", "subtitle2"), chips]


def _editor(b: _Builder, view: StudioView) -> str:
    """The image with its gauges beside it, so slider moves and results stay in view.

    GE's style allowlist has no `position` or `overflow`, so nothing can stick or
    scroll on its own. Side by side, the gauges are about as tall as the image; on a
    card too narrow for both, they wrap to sit right under it.
    """
    media = _media(b, view)
    if view.kind != "image" or not view.critique:
        return media
    b.get(media).update(weight=3, style={"gap": "4px", "minWidth": "300px"})
    gauges = b.add(
        "gauges",
        "MaterialColumn",
        children=_gauges(b, view.critique),
        align="stretch",
        weight=2,
        style={"gap": "8px", "minWidth": "240px"},
    )
    return b.add(
        "editor",
        "MaterialRow",
        children=[media, gauges],
        align="start",
        style={"gap": "16px", "flexWrap": "wrap"},
    )


def _studio(b: _Builder, view: StudioView) -> list[str]:
    body: list[str] = []
    if view.critique:
        body.append(b.text("summary", view.critique.summary, "body2"))
    if view.kind == "image" and view.critique:
        body += [
            b.add("d1", "MaterialDivider"),
            *_quick_fixes(b, view.critique),
            *_questions(b, view.critique),
            b.row(
                "answer-actions",
                [
                    b.button(
                        "apply-answers",
                        "Apply answers",
                        ANSWERS,
                        prompt="Apply my answers",
                        variant="flat",
                        icon="check",
                        **_answers(view.critique),
                    ),
                    _more_questions(
                        b, "Ask me more", "Ask me more questions about this image"
                    ),
                ],
            ),
            b.add("d5", "MaterialDivider"),
            *_render_settings(b),
            b.add("d3", "MaterialDivider"),
            *_open_question(b, view.critique),
        ]
    elif view.kind == "video":
        body += _motion_chips(b, view)
    body += [b.add("d4", "MaterialDivider"), _next_steps(b, view)]
    return body


def render(view: StudioView) -> list[dict[str, Any]]:
    """Returns createSurface + updateComponents + updateDataModel for one response."""
    # Gemini Enterprise needs a fresh surfaceId per response; reusing one confuses it.
    surface_id = f"studio-{view.asset_id}-{uuid.uuid4().hex[:6]}"
    b = _Builder()

    header = [
        b.text("title", view.title, "h5"),
        b.text("lineage", view.lineage, "caption"),
    ]
    if view.badge:
        header.append(b.text("badge", view.badge, "caption"))
    if view.mode == "questions" and view.critique:
        body = [*header, *_questions_card(b, view)]
    else:
        body = [*header, *_versions(b, view), _editor(b, view), *_studio(b, view)]

    content = b.column("content", body)
    if config.STUDIO_LAYOUT == "canvas":
        b.add(
            "root",
            "Canvas",
            children=[content],
            cardTitle=view.title,
            cardDescription=view.lineage,
            cardIcon="palette",
            autoOpen=True,
        )
    else:
        b.add("root", "MaterialCard", children=[content], appearance="outlined")

    data: dict[str, Any] = {
        "filters": view.filters,
        "answers": {},
        "note": "",
        "motion": "",
        "version": str(view.version),
        "render": {"model": view.tier, "aspect": view.aspect},
    }
    if view.live_url:
        data[live_preview.DATA_KEY] = view.live_data
    messages = [
        {
            "version": VERSION,
            "createSurface": {
                "surfaceId": surface_id,
                "catalogId": catalog().catalog_id,
            },
        },
        {
            "version": VERSION,
            "updateComponents": {"surfaceId": surface_id, "components": b.components},
        },
        {
            "version": VERSION,
            "updateDataModel": {"surfaceId": surface_id, "path": "/", "value": data},
        },
    ]
    validate(messages)
    return messages


def validate(messages: list[dict[str, Any]]) -> None:
    # validate_components() returns errors instead of raising; don't ignore them.
    if errors := catalog().validate_components(messages):
        raise InvalidSurface("; ".join(f"{e.path}: {e.message}" for e in errors))
    ids = [
        c["id"]
        for m in messages
        for c in m.get("updateComponents", {}).get("components", [])
    ]
    if len(ids) != len(set(ids)):
        raise InvalidSurface(
            f"Duplicate component ids: {sorted({i for i in ids if ids.count(i) > 1})}"
        )
