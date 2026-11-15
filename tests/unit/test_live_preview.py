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

import base64
import io
import re

from PIL import Image

from app.studio import live_preview
from app.studio.filters import GAUGES, neutral_params


def _jpeg(size=(1200, 600), color=(60, 60, 60)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


def test_encode_downscales_and_reports_the_mean_grey():
    b64, w, h, mean = live_preview.encode(_jpeg(), 800)
    assert (w, h) == (800, 400)
    assert Image.open(io.BytesIO(base64.b64decode(b64))).size == (800, 400)
    assert abs(mean - 60 / 255) < 0.01


def test_url_reads_every_gauge_from_the_data_model():
    call = live_preview.live_url(live_preview.encode(_jpeg(), 800), neutral_params())
    assert call["call"] == "formatString"
    url = call["args"]["value"]
    assert url.startswith("data:image/svg+xml;utf8,")
    paths = set(re.findall(r"\$\{/filters/(\w+)\}", url))
    assert paths == set(GAUGES)
    # '#' would end the data URL; '%' must not start a bogus escape.
    assert "#" not in url
    assert not re.search(r"%(?![0-9A-F]{2})", url)


def test_negative_values_stay_valid_css():
    url = live_preview.live_url(
        live_preview.encode(_jpeg(), 800), neutral_params(), None
    )["args"]["value"]
    filled = re.sub(r"\$\{/filters/\w+\}", "-50", url)
    assert "--50" not in filled


def test_the_applied_render_is_layered_on_top_only_when_there_is_one():
    base = live_preview.encode(_jpeg(), 800)
    applied = {**neutral_params(), "warmth": 20}
    plain = live_preview.live_url(base, neutral_params())["args"]["value"]
    layered = live_preview.live_url(base, applied, live_preview.encode(_jpeg(), 800))
    layered = layered["args"]["value"]
    assert plain.count("data:image/jpeg") == 1
    assert layered.count("data:image/jpeg") == 2
    assert "${/filters/warmth} - 20" in layered


def test_images_live_in_the_data_model_so_the_template_stays_small():
    # The A2UI reference renderer rejects formatString templates over 10,000 chars.
    big = live_preview.encode(_jpeg((3000, 2000), (90, 120, 30)), 800)
    call = live_preview.live_url(big, neutral_params(), big)
    assert len(call["args"]["value"]) < 10_000
    assert "${/live/base}" in call["args"]["value"]
    assert live_preview.data(big, big) == {"base": big[0], "current": big[0]}
    assert live_preview.data(big) == {"base": big[0]}


def test_contrast_blends_toward_the_mean_grey_without_css_contrast():
    # CSS contrast() pivots on 50% grey; pivoting it with brightness steps clipped the
    # highlights and made the preview jump the moment contrast left 0.
    base = live_preview.encode(_jpeg(color=(51, 51, 51)), 800)  # mean grey 0.2
    url = live_preview.live_url(base, neutral_params())["args"]["value"]
    assert "contrast(" not in url
    assert "fill='rgb(51,51,51)'" in url  # negative contrast fades toward this grey
    assert "${/filters/contrast} * -0.006" in url
    assert "slope='1.6' intercept='-0.1200'" in url  # the k=1.6 copy, around the mean
