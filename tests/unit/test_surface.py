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

import copy

import pytest

from app import config
from app.studio import surface
from app.studio.critique import Critique
from app.studio.filters import GAUGES, neutral_params


@pytest.fixture
def critique() -> Critique:
    return Critique(
        subject_type="product",
        summary="Clean shot, but the shadow on the right is harsh.",
        quick_fixes=[
            {
                "label": "Soften shadow",
                "instruction": "soften the hard shadow on the right",
            }
        ],
        questions=[
            {
                "id": "background",
                "question": "Which background?",
                "options": [
                    {"label": "Studio", "instruction": "seamless white studio"},
                    {"label": "Kitchen", "instruction": "sunlit kitchen counter"},
                ],
            }
        ],
        open_question="What should the mug say?",
        gauges=["warmth", "exposure", "vignette"],
        motion_ideas=["steam rises", "slow orbit"],
    )


def _view(critique, **kw) -> surface.StudioView:
    defaults = {
        "asset_id": "image-abc",
        "kind": "image",
        "url": "https://x/a.png",
        "title": "Mug",
        "lineage": "v1 · Nano Banana 2",
        "critique": critique,
        "filters": neutral_params(),
    }
    return surface.StudioView(**{**defaults, **kw})


def _components(messages):
    return next(
        m["updateComponents"]["components"] for m in messages if "updateComponents" in m
    )


@pytest.mark.parametrize(
    "kw",
    [
        {},
        {"before_url": "https://x/b.png"},
        {"kind": "video"},
        {"badge": "Flash fallback"},
        {"mode": "questions", "intro": "A few more questions."},
    ],
)
def test_rendered_surfaces_validate_against_ge_catalog(critique, kw):
    messages = surface.render(_view(critique, **kw))
    assert [next(k for k in m if k != "version") for m in messages] == [
        "createSurface",
        "updateComponents",
        "updateDataModel",
    ]
    assert all(m["version"] == "v0.9" for m in messages)


def test_canvas_layout_validates(critique, monkeypatch):
    monkeypatch.setattr(config, "STUDIO_LAYOUT", "canvas")
    root = next(
        c for c in _components(surface.render(_view(critique))) if c["id"] == "root"
    )
    assert root["component"] == "Canvas"


def test_every_response_gets_a_new_surface_id(critique):
    ids = {
        surface.render(_view(critique))[0]["createSurface"]["surfaceId"]
        for _ in range(5)
    }
    assert len(ids) == 5


def test_controls_follow_the_critique(critique):
    comps = {c["id"]: c for c in _components(surface.render(_view(critique)))}
    assert {"g-warmth", "g-exposure", "g-vignette"} <= comps.keys()
    assert "g-contrast" not in comps
    assert (
        comps["fix-0"]["action"]["event"]["context"]["instruction"]
        == "soften the hard shadow on the right"
    )
    chips = comps["q0"]
    assert [o["label"] for o in chips["options"]] == ["Studio", "Kitchen"]
    assert chips["value"] == {"path": "/answers/background"}


def test_studio_questions_wait_for_apply_but_quick_fixes_are_instant(critique):
    comps = {c["id"]: c for c in _components(surface.render(_view(critique)))}
    assert "action" not in comps["q0"]  # picking a chip only records it
    assert comps["fix-0"]["action"]["event"]["name"] == surface.REFINE
    for button in ("apply-answers", "note-send"):  # both send every pick and the note
        event = comps[button]["action"]["event"]
        assert event["name"] == surface.ANSWERS
        assert event["context"]["answer_background"] == {"path": "/answers/background"}
        assert event["context"]["note"] == {"path": "/note"}
        assert event["context"]["warmth"] == {"path": "/filters/warmth"}
    assert comps["more-questions"]["action"]["event"]["name"] == surface.QUESTIONS


def test_every_action_carries_a_prompt(critique):
    # GE shows context.prompt as the user's chat message.
    for kw in ({}, {"kind": "video"}):
        for c in _components(surface.render(_view(critique, **kw))):
            if "event" in c.get("action", {}):  # openUrl calls stay client-side
                assert c["action"]["event"]["context"]["prompt"]


def test_open_full_size_is_always_offered(critique):
    comps = {c["id"]: c for c in _components(surface.render(_view(critique)))}
    url = comps["open-full"]["action"]["functionCall"]["args"]["url"]
    assert url == comps["img-current"]["url"]


def test_gauges_sit_beside_the_image(critique):
    comps = {c["id"]: c for c in _components(surface.render(_view(critique)))}
    assert comps["editor"]["children"] == ["media", "gauges"]
    assert "apply-filters" in comps["gauges"]["children"]
    assert comps["editor"]["style"]["flexWrap"] == "wrap"
    # The sizing lands on the media column, with or without the Before tab.
    for view in (_view(critique), _view(critique, before_url="https://x/b.png")):
        comps = {c["id"]: c for c in _components(surface.render(view))}
        assert comps["media"]["component"] == "MaterialColumn"
        assert comps["media"]["weight"] == 3
        assert comps["media"]["style"]["minWidth"] == "300px"


def test_apply_and_answers_carry_every_gauge_shown_or_not(critique):
    # adjust_image takes absolute values: a hidden gauge left out would reset to 0.
    comps = {c["id"]: c for c in _components(surface.render(_view(critique)))}
    ctx = comps["apply-filters"]["action"]["event"]["context"]
    assert {k for k in ctx if k != "prompt"} == set(GAUGES)
    answers = comps["apply-answers"]["action"]["event"]["context"]
    assert set(GAUGES) <= set(answers)


def test_gauges_keep_registry_order_whatever_the_critique_lists(critique):
    shuffled = critique.model_copy(
        update={"gauges": ["vignette", "warmth", "exposure"]}
    )
    comps = {c["id"]: c for c in _components(surface.render(_view(shuffled)))}
    rows = [c for c in comps["gauges"]["children"] if c.endswith("-row")]
    assert rows == ["g-warmth-row", "g-exposure-row", "g-vignette-row"]


def test_render_settings_carry_model_and_aspect(critique):
    view = _view(critique, aspect="4:5", tier="pro")
    messages = surface.render(view)
    comps = {c["id"]: c for c in _components(messages)}
    ctx = comps["reframe"]["action"]["event"]["context"]
    assert ctx["aspect"] == {"path": "/render/aspect"}
    assert ctx["model"] == {"path": "/render/model"}
    assert comps["fix-0"]["action"]["event"]["context"]["model"] == {
        "path": "/render/model"
    }
    assert messages[2]["updateDataModel"]["value"]["render"] == {
        "model": "pro",
        "aspect": "4:5",
    }


def test_validation_rejects_unknown_components(critique):
    messages = copy.deepcopy(surface.render(_view(critique)))
    _components(messages)[0]["component"] = "FancyImage"
    with pytest.raises(surface.InvalidSurface):
        surface.validate(messages)


def test_validation_rejects_duplicate_ids(critique):
    messages = copy.deepcopy(surface.render(_view(critique)))
    comps = _components(messages)
    comps.append(dict(comps[-1]))
    with pytest.raises(surface.InvalidSurface, match="Duplicate"):
        surface.validate(messages)


def test_live_preview_url_drives_the_current_image(critique):
    live = {"call": "formatString", "args": {"value": "data:image/svg+xml;utf8,x"}}
    messages = surface.render(
        _view(critique, live_url=live, live_data={"base": "QUJD"})
    )
    comps = {c["id"]: c for c in _components(messages)}
    assert comps["img-current"]["url"] == live
    assert messages[2]["updateDataModel"]["value"]["live"] == {"base": "QUJD"}
    # The open-full button still opens the real file, not the SVG.
    assert comps["open-full"]["action"]["functionCall"]["args"]["url"].startswith(
        "https://"
    )


def test_questions_mode_records_choices_and_applies_them_together(critique):
    comps = {
        c["id"]: c
        for c in _components(surface.render(_view(critique, mode="questions")))
    }
    assert "action" not in comps["q0"]  # picking a chip doesn't render yet
    ctx = comps["apply-answers"]["action"]["event"]["context"]
    assert ctx["answer_background"] == {"path": "/answers/background"}
    assert ctx["note"] == {"path": "/note"}
    assert "g-warmth" not in comps


def test_images_use_the_basic_image_component(critique):
    """GE's MaterialImage blanks signed GCS URLs; the basic Image loads them."""
    comps = {
        c["id"]: c
        for c in _components(surface.render(_view(critique, before_url="https://x/b")))
    }
    assert comps["img-current"]["component"] == "Image"
    assert comps["img-before"]["component"] == "Image"
    assert not [c for c in comps.values() if c["component"] == "MaterialImage"]


def test_download_opens_the_full_file(critique):
    messages = surface.render(_view(critique, full_url="https://x/full.png"))
    comps = {c["id"]: c for c in _components(messages)}
    assert comps["download"]["action"]["functionCall"] == {
        "call": "openUrl",
        "args": {"url": "https://x/full.png"},
    }
