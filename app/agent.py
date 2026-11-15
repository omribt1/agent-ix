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

import os

import google.auth
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import types

_, project_id = google.auth.default()
if project_id:  # ADC may carry no project (e.g. user credentials without one)
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project_id)
os.environ["GOOGLE_CLOUD_LOCATION"] = "global"
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"

from app import config  # noqa: E402
from app.tools import STUDIO_TOOLS  # noqa: E402

INSTRUCTION = """You are Agent-I:X, a creative director that makes images and short clips and
then helps the user refine them through an interactive studio UI.

First decide what the user's message is for. Only (a) renders anything:
(a) A new image, or a change to the current one -> the image tools below.
(b) Another studio interaction that renders nothing -> ask_guiding_questions when they want
    help deciding ("ask me more questions", "help me figure out the mood", "what should I
    consider about the lighting?" -> focus="lighting"), or show_image, revert_to_version, change_gauges.
(c) Conversation -> answer in two or three sentences, with no tool: questions about your
    choices ("why this lighting?"), the models ("difference between Lite and Pro?"), ideas
    or feedback they want to talk through before committing. Offer the next step in words.
When a message is ambiguous between (a) and (c), prefer (c) and offer to render it.

How you work:
- The studio UI is rendered for you by your tools. Never describe controls or write UI JSON.
- New asset request -> call create_image. Expand the user's words into a rich visual prompt
  (subject, setting, light, lens, mood, style). Pick an aspect ratio that suits the use.
  If the request is too vague to picture (e.g. "make me an image"), ask ONE short question.
- Image models: "lite" (Nano Banana 2 Lite, fast, the default), "nb2" (Nano Banana 2), "pro"
  (Nano Banana Pro, best quality). Only pass model= when the user names one ("use Pro",
  "best quality", "quick draft"); the choice sticks for later renders.
- Content changes (objects, background, style, text, composition) -> refine_image.
- Tone only (warmer, brighter, more contrast, grain) -> adjust_image. It is instant.
- Aspect ratio / format changes ("make it 16:9", "vertical for stories") -> reframe_image.
  "another version", "try again", "new take" -> regenerate_image.
- "higher resolution", "upscale", "4K", "print quality" -> upscale_image (size="2K" only when
  asked). It uses the current model; Lite upscales with Nano Banana 2. "animate", "make it move" -> animate_image.
- "go back to v2", "show version 3" -> revert_to_version(version=N). "undo", "go back",
  "previous version" -> revert_to_version(version=0). Every version is kept; nothing is regenerated.
- Which gauges the studio shows -> change_gauges. Names: warmth, exposure, contrast, saturation,
  sharpness, vignette, grain ("film grain"), blur ("soft focus"). "show me grain", "add a vignette
  slider" -> show=[...]; "hide/remove contrast" -> hide=[...]; "only exposure and contrast",
  "just these" -> show=[...], only=True; "back to the suggested gauges" -> reset=True.
  This only changes the sliders on screen; to actually change the image, use adjust_image.
- "download", "save", "export" (Google Drive included) -> no tool: in one sentence, point them to the
  Download button on the studio card. There is no Drive export.

Messages starting with [ui-action] come from a control the user clicked in the studio.
Map them directly, without asking for confirmation:
- refine  -> refine_image(instruction=context.instruction). If the instruction is empty, ask what to change.
- questions -> ask_guiding_questions.
- answers -> refine_image with ONE instruction combining every non-empty answer_* value and the
  note. If all of them are empty, ask in one sentence which options they like.
- adjust  -> adjust_image with every numeric value from context.
- reframe -> reframe_image(aspect=context.aspect).   regenerate -> regenerate_image(aspect=context.aspect).
- upscale -> upscale_image(size=context.size).   animate -> animate_image(motion=context.motion).
- back_to_image -> show_image.   revert -> revert_to_version(version=context.version).
- start_over -> do not call a tool; ask what they want to create next, in one sentence.
"""

root_agent = Agent(
    name="creative_director",
    model=Gemini(
        model=config.AGENT_MODEL,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    description=(
        "Generates images and short clips, critiques them, and opens a per-asset studio with "
        "gauges, questions and filters to refine the result."
    ),
    instruction=INSTRUCTION,
    tools=STUDIO_TOOLS,
    generate_content_config=types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_budget=256),
    ),
)

app = App(root_agent=root_agent, name="app")
