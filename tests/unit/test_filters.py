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

import io

from PIL import Image, ImageStat

from app.studio import filters


def _png(w: int = 400, h: int = 300, color=(120, 120, 120)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def _open(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).convert("RGB")


def test_normalize_clamps_and_coerces_ui_values():
    p = filters.normalize(
        {"warmth": "250", "exposure": -30.4, "blur": -5, "aspect": "16:9", "bogus": 1}
    )
    assert "aspect" not in p  # aspect is a render setting now, not a filter
    assert p["warmth"] == 100
    assert p["exposure"] == -30
    assert p["blur"] == 0
    assert "bogus" not in p


def test_neutral_params_keep_size():
    out = _open(filters.apply(_png(), {}))
    assert out.size == (400, 300)
    assert filters.is_neutral({"warmth": 0})


def test_warmth_shifts_red_up_and_blue_down():
    r, _, b = ImageStat.Stat(_open(filters.apply(_png(), {"warmth": 100}))).mean
    assert r > 125 and b < 115


def test_exposure_brightens():
    before = ImageStat.Stat(_open(_png())).mean[0]
    after = ImageStat.Stat(_open(filters.apply(_png(), {"exposure": 50}))).mean[0]
    assert after > before + 20
