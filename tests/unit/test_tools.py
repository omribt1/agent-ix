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

"""Asset lineage and turn-ending behaviour, with the models stubbed out."""

import io
from types import SimpleNamespace

import pytest
from PIL import Image

from app import tools
from app.studio import assets
from app.studio.critique import Critique, GuidingQuestions

CRITIQUE = Critique(
    subject_type="product",
    summary="ok",
    quick_fixes=[],
    questions=[
        {
            "id": "bg",
            "question": "Background?",
            "options": [
                {"label": "A", "instruction": "a"},
                {"label": "B", "instruction": "b"},
            ],
        }
    ],
    open_question="Anything else?",
    gauges=["warmth", "exposure", "contrast"],
    motion_ideas=["orbit", "push-in"],
)


def _versions(out: dict) -> list:
    """(version, label) pairs from the version chips in a rendered surface."""
    comps = next(m for m in out["a2ui"] if "updateComponents" in m)
    chips = next(
        c for c in comps["updateComponents"]["components"] if c["id"] == "versions"
    )
    return [(int(o["value"]), o["label"].split(" ", 1)[1]) for o in chips["options"]]


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (100, 150, 200)).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    local = assets.LocalAssetStore(root=str(tmp_path), base_url="http://test/assets")
    monkeypatch.setattr(tools, "store", lambda: local)
    calls = {"critique": 0}

    async def fake_generate(prompt, aspect_ratio, model):
        calls.setdefault("models", []).append(model)
        return _png(), "image/png", model

    async def fake_edit(image, instruction, model):
        calls.setdefault("models", []).append(model)
        return _png(), "image/png", model

    async def fake_reframe(image, aspect_ratio, model):
        calls.setdefault("models", []).append(model)
        return _png(), "image/png", model

    async def fake_critique(image, brief):
        calls["critique"] += 1
        calls.setdefault("critique_px", []).append(Image.open(io.BytesIO(image)).size)
        return CRITIQUE

    monkeypatch.setattr(tools.media, "generate", fake_generate)
    monkeypatch.setattr(tools.media, "edit", fake_edit)
    monkeypatch.setattr(tools.media, "reframe", fake_reframe)
    monkeypatch.setattr(tools, "critique", fake_critique)
    return SimpleNamespace(
        state={},
        actions=SimpleNamespace(skip_summarization=False),
        session=SimpleNamespace(id="chat-1"),
        calls=calls,
    )


async def test_create_ends_turn_with_a2ui(ctx):
    out = await tools.create_image("a mug", "1:1", "Mug", ctx)
    assert ctx.actions.skip_summarization is True
    assert out["a2ui"][0]["createSurface"]
    assert ctx.state[tools.CURRENT] == out["asset_id"]


async def test_filters_start_from_base_and_reuse_its_critique(ctx):
    first = await tools.create_image("a mug", "1:1", "Mug", ctx)
    a = await tools.adjust_image(ctx, warmth=40)
    b = await tools.adjust_image(ctx, warmth=-20)
    entries = ctx.state[tools.ASSETS]
    assert entries[a["asset_id"]]["base"] == first["asset_id"]
    assert entries[b["asset_id"]]["parent"] == first["asset_id"]  # not stacked on `a`
    assert entries[b["asset_id"]]["params"]["warmth"] == -20
    assert ctx.calls["critique"] == 1  # pixel tweaks don't trigger a new critique


async def test_edit_creates_new_base_and_new_critique(ctx):
    await tools.create_image("a mug", "1:1", "Mug", ctx)
    adjusted = await tools.adjust_image(ctx, warmth=40)
    edited = await tools.refine_image("add a saucer", ctx)
    entry = ctx.state[tools.ASSETS][edited["asset_id"]]
    assert entry["parent"] == adjusted["asset_id"]  # edits what the user saw
    assert entry["base"] == edited["asset_id"]
    assert ctx.calls["critique"] == 2


async def test_tools_without_an_asset_ask_for_one(ctx):
    out = await tools.refine_image("brighter", ctx)
    assert out["status"] == "error"
    assert "a2ui" not in out


async def test_revert_puts_an_old_version_back_and_edits_branch_from_it(ctx):
    first = await tools.create_image("a mug", "1:1", "Mug", ctx)
    await tools.adjust_image(ctx, warmth=40)
    await tools.refine_image("add a saucer", ctx)
    out = await tools.revert_to_version(ctx, version=1)
    assert out["asset_id"] == first["asset_id"]
    assert ctx.state[tools.CURRENT] == first["asset_id"]
    assert len(ctx.state[tools.ASSETS]) == 3  # nothing deleted
    edited = await tools.refine_image("make it blue", ctx)
    entry = ctx.state[tools.ASSETS][edited["asset_id"]]
    assert entry["parent"] == first["asset_id"] and entry["version"] == 4


async def test_undo_returns_to_the_parent(ctx):
    first = await tools.create_image("a mug", "1:1", "Mug", ctx)
    await tools.adjust_image(ctx, warmth=40)
    assert (await tools.revert_to_version(ctx))["asset_id"] == first["asset_id"]
    assert (await tools.revert_to_version(ctx))["status"] == "error"
    assert (await tools.revert_to_version(ctx, version=9))["status"] == "error"


async def test_refine_applies_pending_sliders_first(ctx):
    await tools.create_image("a mug", "1:1", "Mug", ctx)
    ctx.state[tools.PENDING] = {"warmth": 30, "vignette": 20}
    edited = await tools.refine_image("add a saucer", ctx)
    assets = ctx.state[tools.ASSETS]
    adjusted = assets[assets[edited["asset_id"]]["parent"]]
    assert adjusted["op"] == "adjust"
    assert adjusted["params"]["warmth"] == 30 and adjusted["params"]["vignette"] == 20
    assert ctx.state[tools.PENDING] is None
    assert "pending slider" in edited["summary"]


async def test_unchanged_sliders_add_no_version(ctx):
    await tools.create_image("a mug", "1:1", "Mug", ctx)
    await tools.adjust_image(ctx, warmth=40)
    ctx.state[tools.PENDING] = {"warmth": 40}
    await tools.refine_image("add a saucer", ctx)
    assert len(ctx.state[tools.ASSETS]) == 3


async def test_surface_lists_versions(ctx):
    await tools.create_image("a mug", "1:1", "Mug", ctx)
    out = await tools.adjust_image(ctx, warmth=40)
    comps = {
        c["id"]: c
        for m in out["a2ui"]
        for c in m.get("updateComponents", {}).get("components", [])
    }
    assert [o["value"] for o in comps["versions"]["options"]] == ["1", "2"]
    assert out["a2ui"][2]["updateDataModel"]["value"]["version"] == "2"


async def test_storage_names_are_readable_per_chat(ctx):
    out = await tools.create_image("a mug", "1:1", "Mug", ctx)
    uri = ctx.state[tools.ASSETS][out["asset_id"]]["uri"]
    assert uri == f"chats/chat-1/v001-generate-{out['asset_id']}.png"


@pytest.fixture
def fake_drive(monkeypatch):
    calls = []

    def export(token, folder, files):
        calls.append((token, folder, files))
        return {
            "folder_url": "https://drive.google.com/drive/folders/F",
            "files": [
                {"name": f.name, "url": f"https://drive/{f.name}"} for f in files
            ],
        }

    monkeypatch.setattr(tools.drive, "export", export)
    return calls


async def test_export_sends_the_current_version_with_the_user_token(ctx, fake_drive):
    await tools.create_image("a mug", "1:1", "Mug", ctx)
    adjusted = await tools.adjust_image(ctx, warmth=40)
    ctx.state[tools.USER_TOKEN] = "user-token"
    out = await tools.export_to_drive(ctx)
    token, folder, files = fake_drive[0]
    assert token == "user-token" and folder == "Mug"
    assert [f.name for f in files] == ["Mug v2 filters.jpg"]
    assert "folders/F" in out["summary"]
    entry = ctx.state[tools.ASSETS][adjusted["asset_id"]]
    assert entry["drive_url"] == "https://drive/Mug v2 filters.jpg"
    comps = {
        c["id"]: c
        for m in out["a2ui"]
        for c in m.get("updateComponents", {}).get("components", [])
    }
    assert comps["open-drive"]["action"]["functionCall"]["args"]["url"].endswith("/F")


async def test_export_all_versions(ctx, fake_drive):
    await tools.create_image("a mug", "1:1", "Mug", ctx)
    await tools.adjust_image(ctx, warmth=40)
    await tools.export_to_drive(ctx, all_versions=True)
    assert [f.name for f in fake_drive[0][2]] == [
        "Mug v1 original.png",
        "Mug v2 filters.jpg",
    ]


async def test_export_without_authorization_explains(ctx, monkeypatch):
    await tools.create_image("a mug", "1:1", "Mug", ctx)
    monkeypatch.setattr(tools.config, "DRIVE_USE_ADC", False)
    out = await tools.export_to_drive(ctx)
    assert out["status"] == "error" and "Drive" in out["message"]


async def test_export_with_drive_off_points_to_the_download(ctx, monkeypatch):
    first = await tools.create_image("a mug", "1:1", "Mug", ctx)
    monkeypatch.setattr(tools.config, "DRIVE_EXPORT", False)
    out = await tools.export_to_drive(ctx)
    uri = ctx.state[tools.ASSETS][first["asset_id"]]["uri"]
    assert out["status"] == "error" and out["message"].endswith(uri)


async def test_lite_is_the_default_and_a_named_tier_sticks(ctx):
    await tools.create_image("a mug", "1:1", "Mug", ctx)
    await tools.refine_image("add a saucer", ctx, model="pro")
    await tools.refine_image("add steam", ctx)
    assert ctx.calls["models"] == [
        "gemini-3.1-flash-lite-image",
        "gemini-3-pro-image",
        "gemini-3-pro-image",
    ]
    assert ctx.state[tools.TIER] == "pro"


async def test_reframe_keeps_content_and_records_the_new_aspect(ctx):
    first = await tools.create_image("a mug", "1:1", "Mug", ctx)
    out = await tools.reframe_image("16:9", ctx)
    entry = ctx.state[tools.ASSETS][out["asset_id"]]
    assert entry["op"] == "reframe" and entry["aspect"] == "16:9"
    assert entry["parent"] == first["asset_id"] and entry["base"] == entry["id"]
    data = out["a2ui"][2]["updateDataModel"]["value"]
    assert data["render"] == {"model": "lite", "aspect": "16:9"}
    assert "aspect" not in data["filters"]
    assert (await tools.reframe_image("2:1", ctx))["status"] == "error"


async def test_regenerate_reuses_the_prompt_and_keeps_the_aspect(ctx):
    await tools.create_image("a mug", "4:5", "Mug", ctx)
    out = await tools.regenerate_image(ctx)
    entry = ctx.state[tools.ASSETS][out["asset_id"]]
    assert entry["op"] == "regenerate" and entry["aspect"] == "4:5"
    assert entry["prompt"] == "a mug"


async def test_guiding_questions_render_without_a_new_version(ctx, monkeypatch):
    seen = {}

    async def fake_questions(image, brief, focus, asked):
        seen.update(focus=focus, asked=list(asked))
        return GuidingQuestions(
            intro="Let's pin down the light.",
            questions=[
                {
                    "id": "light",
                    "question": "Light?",
                    "options": [
                        {"label": "Soft", "instruction": "soft window light"},
                        {"label": "Hard", "instruction": "hard noon sun"},
                    ],
                },
                {
                    "id": "time",
                    "question": "Time of day?",
                    "options": [
                        {"label": "Dawn", "instruction": "at dawn"},
                        {"label": "Dusk", "instruction": "at dusk"},
                    ],
                },
                {
                    "id": "shadow",
                    "question": "Shadows?",
                    "options": [
                        {"label": "Long", "instruction": "long shadows"},
                        {"label": "None", "instruction": "shadowless"},
                    ],
                },
            ],
            open_question="Anything about the shadows?",
        )

    monkeypatch.setattr(tools, "guiding_questions", fake_questions)
    first = await tools.create_image("a mug", "1:1", "Mug", ctx)
    out = await tools.ask_guiding_questions(ctx, focus="lighting")
    assert out["asset_id"] == first["asset_id"]
    assert len(ctx.state[tools.ASSETS]) == 1  # questions never render
    assert seen == {"focus": "lighting", "asked": ["Background?"]}
    comps = {
        c["id"]: c
        for m in out["a2ui"]
        if "updateComponents" in m
        for c in m["updateComponents"]["components"]
    }
    assert "apply-answers" in comps and "g-warmth" not in comps
    assert {"q0", "q1", "q2"} <= comps.keys()  # more than the studio has room for
    # The studio shows the new questions too, and asking again skips all of them.
    studio = await tools.show_image(ctx)
    assert "Light?" in str(studio["a2ui"])
    await tools.ask_guiding_questions(ctx)
    assert seen["asked"] == ["Background?", "Light?", "Time of day?", "Shadows?"]


async def test_upscale_uses_the_picked_model_and_lite_goes_to_nb2(ctx, monkeypatch):
    calls = []

    async def fake_upscale(image, prompt, model, size):
        calls.append((model, size))
        return _png(), "image/png", model, False

    monkeypatch.setattr(tools.media, "upscale", fake_upscale)
    await tools.create_image("a mug", "4:5", "Mug", ctx)  # Lite by default
    out = await tools.upscale_image(ctx)
    entry = ctx.state[tools.ASSETS][out["asset_id"]]
    assert calls[-1] == ("gemini-3.1-flash-image", "4K")  # Lite can't upscale
    assert (entry["op"], entry["size"], entry["aspect"]) == ("upscale", "4K", "4:5")
    assert "upscale: 4K" in tools._lineage(entry)
    assert "Upscale 4K" not in str(out["a2ui"])  # already at the maximum
    assert (2, "4K") in [tuple(v) for v in _versions(out)]
    await tools.revert_to_version(ctx, version=1)
    await tools.upscale_image(ctx, size="2k", model="pro")
    assert calls[-1] == ("gemini-3-pro-image", "2K")
    assert ctx.state[tools.TIER] == "pro"  # a named model sticks
    bad = await tools.upscale_image(ctx, size="8K")
    assert bad["status"] == "error" and len(calls) == 2


async def test_upscale_badges_a_pro_fallback(ctx, monkeypatch):
    async def fake_upscale(image, prompt, model, size):
        return _png(), "image/png", "gemini-3.1-flash-image", True

    monkeypatch.setattr(tools.media, "upscale", fake_upscale)
    await tools.create_image("a mug", "1:1", "Mug", ctx, model="pro")
    out = await tools.upscale_image(ctx)
    assert "at capacity" in str(out["a2ui"])


async def test_animate_makes_a_clip_and_back_returns_to_its_still(ctx, monkeypatch):
    async def fake_animate(image, motion):
        return b"MP4", "video/mp4", "omni"

    monkeypatch.setattr(tools.media, "animate", fake_animate)
    still = await tools.create_image("a mug", "1:1", "Mug", ctx)
    clip = await tools.animate_image("steam rises", ctx)
    entry = ctx.state[tools.ASSETS][clip["asset_id"]]
    assert (entry["kind"], entry["parent"]) == ("video", still["asset_id"])
    assert entry["uri"].endswith(".mp4") and "/v002-animate-" in entry["uri"]
    assert "'component': 'Video'" in str(clip["a2ui"])
    assert ctx.calls["critique"] == 1  # the clip reuses the still's critique
    # Animating again from the clip animates the still, not the clip.
    again = await tools.animate_image("slow orbit", ctx)
    assert ctx.state[tools.ASSETS][again["asset_id"]]["parent"] == still["asset_id"]
    back = await tools.show_image(ctx)
    assert back["asset_id"] == still["asset_id"]


async def test_big_images_show_a_preview_and_open_the_full_file(ctx, monkeypatch):
    big = io.BytesIO()
    Image.new("RGB", (3712, 4608), (120, 90, 60)).save(big, "PNG")

    async def fake_upscale(image, prompt, model, size):
        return big.getvalue(), "image/png", model, False

    monkeypatch.setattr(tools.media, "upscale", fake_upscale)
    first = await tools.create_image("a mug", "4:5", "Mug", ctx)
    assert "preview_uri" not in ctx.state[tools.ASSETS][first["asset_id"]]
    out = await tools.upscale_image(ctx)
    entry = ctx.state[tools.ASSETS][out["asset_id"]]
    assert entry["uri"].endswith(".png")
    assert entry["preview_uri"] == entry["uri"].replace(".png", "-preview.jpg")
    shown = Image.open(io.BytesIO(tools.store().load(entry["preview_uri"])))
    assert max(shown.size) == tools.config.PREVIEW_SIDE
    comps = {
        c["id"]: c
        for m in out["a2ui"]
        if "updateComponents" in m
        for c in m["updateComponents"]["components"]
    }
    assert comps["img-current"]["url"]["call"] == "formatString"
    assert comps["open-full"]["action"]["functionCall"]["args"]["url"].endswith(
        entry["uri"]
    )
    # Critique saw the preview, not the 4K file.
    assert max(ctx.calls["critique_px"][-1]) == tools.config.PREVIEW_SIDE


def test_oversized_model_inputs_become_full_resolution_jpeg(monkeypatch):
    monkeypatch.setattr(assets.config, "MODEL_INPUT_MAX_BYTES", 1000)
    assert assets.model_input(b"x" * 10) == b"x" * 10
    noisy = io.BytesIO()
    Image.effect_noise((256, 256), 64).convert("RGB").save(noisy, "PNG")
    out = assets.model_input(noisy.getvalue())
    img = Image.open(io.BytesIO(out))
    assert (img.format, img.size) == ("JPEG", (256, 256))
