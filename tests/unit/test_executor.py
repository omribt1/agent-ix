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

from types import SimpleNamespace

from a2a.types import DataPart, Message, Part, Role, TextPart

from app.executor import (
    action_from_part,
    pending_filters,
    picked_tier,
    request_to_text,
    user_token,
)


def _action_part(name="adjust", **context) -> Part:
    return Part(
        root=DataPart(
            data={
                "version": "v0.9",
                "action": {
                    "name": name,
                    "surfaceId": "s",
                    "sourceComponentId": "c",
                    "timestamp": "t",
                    "context": context,
                },
            }
        )
    )


def _ctx(*parts: Part):
    return SimpleNamespace(
        message=Message(message_id="m", role=Role.user, parts=list(parts)),
        call_context=None,
    )


def test_action_from_part_reads_v09_actions_only():
    assert action_from_part(_action_part())["name"] == "adjust"
    assert (
        action_from_part(
            Part(root=DataPart(data={"version": "v0.8", "userAction": {"name": "x"}}))
        )
        is None
    )
    assert action_from_part(Part(root=TextPart(text="hi"))) is None


def test_action_wins_over_prompt_text_and_drops_prompt():
    text = request_to_text(
        _ctx(
            Part(root=TextPart(text="Apply these adjustments")),
            _action_part(
                "adjust", prompt="Apply these adjustments", warmth=40, aspect="4:5"
            ),
        )
    )
    assert text == '[ui-action] adjust context={"warmth": 40, "aspect": "4:5"}'


def test_plain_text_passes_through():
    assert (
        request_to_text(_ctx(Part(root=TextPart(text="a red bicycle"))))
        == "a red bicycle"
    )


def test_refine_moves_slider_values_out_of_the_prompt():
    ctx = _ctx(_action_part("refine", instruction="add steam", warmth=20, model="pro"))
    assert (
        request_to_text(ctx)
        == '[ui-action] refine context={"instruction": "add steam"}'
    )
    assert pending_filters(ctx) == {"warmth": 20}
    assert picked_tier(ctx) == "pro"


def test_other_actions_carry_no_pending_filters():
    assert pending_filters(_ctx(_action_part("adjust", warmth=20))) is None
    assert pending_filters(_ctx(Part(root=TextPart(text="warmer")))) is None


def test_user_token_comes_from_the_bearer_header():
    ctx = _ctx(Part(root=TextPart(text="hi")))
    assert user_token(ctx) is None
    ctx.call_context = SimpleNamespace(
        state={"headers": {"authorization": "Bearer abc"}}
    )
    assert user_token(ctx) == "abc"
    ctx.call_context = SimpleNamespace(state={"headers": {"authorization": "Basic x"}})
    assert user_token(ctx) is None


def test_unknown_tier_is_ignored():
    assert picked_tier(_ctx(_action_part("reframe", aspect="1:1", model="x"))) is None


def test_answers_carry_pending_filters():
    ctx = _ctx(_action_part("answers", answer_bg="studio", note="", warmth=15))
    assert pending_filters(ctx) == {"warmth": 15}
    assert "warmth" not in request_to_text(ctx)
