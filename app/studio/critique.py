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

"""The vision critique that decides which controls a given asset gets.

This is where the UI becomes per-asset: a portrait gets different questions,
gauges and quick fixes than a product shot or a landscape. The model only picks
*what* to show; app/studio/surface.py decides *how* it renders.
"""

from typing import Literal, TypeVar

from google.genai import types
from pydantic import BaseModel, Field

from app import config
from app.studio import media
from app.studio.filters import GAUGES

GaugeName = Literal[tuple(GAUGES)]  # type: ignore[valid-type]
T = TypeVar("T", bound=BaseModel)


class QuickFix(BaseModel):
    label: str = Field(description="2-4 word chip label, e.g. 'Soften shadows'.")
    instruction: str = Field(
        description="Concrete edit instruction for the image model."
    )


class Option(BaseModel):
    label: str = Field(description="1-3 word option label.")
    instruction: str = Field(
        description="Edit instruction applied if the user picks this option."
    )


class ClosedQuestion(BaseModel):
    id: str = Field(description="snake_case id, e.g. 'background'.")
    question: str = Field(
        description="Short question about a creative choice in this image."
    )
    options: list[Option] = Field(min_length=2, max_length=4)


class Critique(BaseModel):
    subject_type: Literal[
        "product", "portrait", "landscape", "food", "interior", "illustration", "other"
    ]
    summary: str = Field(
        description="One sentence: what works and the main thing to improve."
    )
    quick_fixes: list[QuickFix] = Field(
        max_length=3, description="Specific flaws you can see."
    )
    questions: list[ClosedQuestion] = Field(
        min_length=1,
        max_length=2,
        description="Creative forks worth asking about for THIS image.",
    )
    open_question: str = Field(
        description="One open question inviting a free-text refinement."
    )
    gauges: list[GaugeName] = Field(  # type: ignore[valid-type]
        min_length=3,
        max_length=5,
        description="The pixel adjustments most useful for this image.",
    )
    motion_ideas: list[str] = Field(
        min_length=2,
        max_length=3,
        description="2-5 word camera/subject motion ideas for a short clip.",
    )


class GuidingQuestions(BaseModel):
    intro: str = Field(description="One short sentence framing the questions.")
    questions: list[ClosedQuestion] = Field(min_length=2, max_length=4)
    open_question: str = Field(
        description="One open question inviting a free-text answer."
    )


_PROMPT = """You are a senior art director reviewing a generated image before it goes to the client.
The brief was: {brief}

Look at the actual pixels. Return:
- quick_fixes: up to 3 concrete flaws you can SEE (lighting, artifacts, composition, text, hands...).
  Skip generic advice. Empty if the image is clean.
- questions: 1-2 creative forks that genuinely matter for THIS subject (a product shot might ask
  about background or surface; a portrait about expression or wardrobe; a landscape about time of day).
- gauges: the 3-5 pixel adjustments from the allowed list that would help most here.
- motion_ideas: short motion ideas that suit this subject.
Keep all labels short enough for a chip or button."""


_QUESTIONS_PROMPT = """You are a senior art director helping a client decide where to take this image.
The brief was: {brief}
{focus}
Already asked (do NOT repeat these or close variants):
{asked}

Look at the actual pixels. Return 2-4 NEW closed questions about creative choices that would
change this image meaningfully, each with 2-4 short options whose instructions an image model
can apply directly. Then one open question. Keep labels short enough for a chip."""


async def _structured(image: bytes, prompt: str, schema: type[T]) -> T:
    response = await media.client().aio.models.generate_content(
        model=config.CRITIC_MODEL,
        contents=[
            types.Part.from_bytes(
                data=media.thumbnail_jpeg(image), mime_type="image/jpeg"
            ),
            prompt,
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
            thinking_config=types.ThinkingConfig(thinking_budget=512),
        ),
    )
    return (
        response.parsed
        if isinstance(response.parsed, schema)
        else schema.model_validate_json(response.text)
    )


async def critique(image: bytes, brief: str) -> Critique:
    return await _structured(image, _PROMPT.format(brief=brief), Critique)


async def guiding_questions(
    image: bytes, brief: str, focus: str = "", asked: list[str] | None = None
) -> GuidingQuestions:
    """Fresh questions on demand, without touching the image."""
    prompt = _QUESTIONS_PROMPT.format(
        brief=brief,
        focus=f"The user wants to explore: {focus}." if focus else "",
        asked="\n".join(f"- {q}" for q in asked or []) or "- (nothing yet)",
    )
    return await _structured(image, prompt, GuidingQuestions)
