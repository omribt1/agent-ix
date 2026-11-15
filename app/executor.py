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

"""A2A executor that speaks A2UI v0.9 the way Gemini Enterprise expects.

Why not ADK's A2aAgentExecutor: it forwards only the *last* event's parts as the
final artifact and leaves status.message empty. Our A2UI comes from a tool response
that is followed by nothing, but a text-only turn (a clarifying question) must work
too, and GE renders the parts of one final message. So: run the ADK runner, collect
text and A2UI from the whole turn, and send a single completed message, the same
shape the official GE sample uses.
"""

import json
import logging
import re
from typing import Any

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import AgentCard, DataPart, Part, TaskState, TextPart
from a2a.utils import new_agent_parts_message, new_agent_text_message
from a2ui.a2a.extension import try_activate_a2ui_extension
from a2ui.a2a.parts import create_a2ui_part
from google.adk.runners import Runner
from google.genai import types

from app import config
from app.studio.surface import ANSWERS, PENDING_FILTER_KEYS, REFINE
from app.tools import A2UI_KEY, PENDING, TIER

logger = logging.getLogger(__name__)

USER_ID = "gemini-enterprise-user"


def action_from_part(part: Part) -> dict[str, Any] | None:
    """Returns the A2UI v0.9 client action carried by a DataPart, if any."""
    root = part.root
    if (
        isinstance(root, DataPart)
        and isinstance(root.data, dict)
        and root.data.get("version") == "v0.9"
    ):
        action = root.data.get("action")
        if isinstance(action, dict) and action.get("name"):
            return action
    return None


def _carries_filters(action: dict[str, Any]) -> bool:
    return action["name"] in (REFINE, ANSWERS)


def action_to_text(action: dict[str, Any]) -> str:
    hidden = {
        "prompt",
        "model",
        *(PENDING_FILTER_KEYS if _carries_filters(action) else ()),
    }
    context = {
        k: v for k, v in (action.get("context") or {}).items() if k not in hidden
    }
    return f"[ui-action] {action['name']} context={json.dumps(context, ensure_ascii=False)}"


def pending_filters(context: RequestContext) -> dict[str, Any] | None:
    """Slider values riding along with a refine/answer click.

    They go to session state, not the prompt: the tool applies them exactly, instead
    of trusting the model to copy eight numbers into a second tool call.
    """
    for part in context.message.parts if context.message else []:
        if (action := action_from_part(part)) and _carries_filters(action):
            ctx = action.get("context") or {}
            return {k: ctx[k] for k in PENDING_FILTER_KEYS if k in ctx} or None
    return None


# The [ui-action] marker tells the model a message is a studio click. Only real,
# structured actions may produce it; in typed text it is defused, so a user can't
# pass a message off as a click.
_TYPED_MARKER = re.compile(r"\[\s*ui-action\s*\]", re.IGNORECASE)


def request_to_text(context: RequestContext) -> str:
    texts, actions = [], []
    for part in context.message.parts if context.message else []:
        if action := action_from_part(part):
            actions.append(action_to_text(action))
        elif isinstance(part.root, TextPart):
            texts.append(_TYPED_MARKER.sub("(typed: ui-action)", part.root.text))
    # When a control fires, GE also sends context.prompt as the visible chat text;
    # the structured action is what the agent should act on.
    return "\n".join(actions) if actions else "\n".join(texts)


def picked_tier(context: RequestContext) -> str | None:
    """The model tier from the studio toggle, sent along with any model action."""
    for part in context.message.parts if context.message else []:
        if action := action_from_part(part):
            tier = (action.get("context") or {}).get("model")
            if tier in config.IMAGE_TIERS:
                return tier
    return None


class StudioAgentExecutor(AgentExecutor):
    def __init__(self, runner: Runner, agent_card: AgentCard):
        self._runner = runner
        self._card = agent_card

    async def _session_id(self, context_id: str) -> str:
        service = self._runner.session_service
        app_name = self._runner.app_name
        session = await service.get_session(
            app_name=app_name, user_id=USER_ID, session_id=context_id
        )
        if session is None:
            session = await service.create_session(
                app_name=app_name, user_id=USER_ID, session_id=context_id
            )
        return session.id

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        try_activate_a2ui_extension(context, self._card)
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        if not context.current_task:
            await updater.submit()
        await updater.start_work(
            new_agent_text_message(
                "Working on it...", context.context_id, context.task_id
            )
        )

        query = request_to_text(context)
        state_delta = {PENDING: pending_filters(context)}
        if tier := picked_tier(context):  # persisted; absent means "keep the pick"
            state_delta[TIER] = tier
        logger.info("turn context=%s query=%s", context.context_id, query[:300])
        texts: list[str] = []
        errors: list[
            str
        ] = []  # tool errors are for the model; shown only as a fallback
        a2ui: list[dict] = []
        try:
            async for event in self._runner.run_async(
                user_id=USER_ID,
                session_id=await self._session_id(context.context_id),
                new_message=types.Content(
                    role="user", parts=[types.Part.from_text(text=query)]
                ),
                # Always set, so stale values never leak into a later turn.
                state_delta=state_delta,
            ):
                for part in (event.content.parts if event.content else None) or []:
                    response = (
                        part.function_response.response
                        if part.function_response
                        else None
                    )
                    if isinstance(response, dict) and response.get(A2UI_KEY):
                        a2ui.extend(response[A2UI_KEY])
                        if response.get("summary"):
                            texts.append(response["summary"])
                    elif (
                        isinstance(response, dict) and response.get("status") == "error"
                    ):
                        errors.append(response.get("message", "Something went wrong."))
                    elif part.text and not part.thought and event.is_final_response():
                        texts.append(part.text)
        except (
            Exception
        ) as e:  # surface failures in the chat instead of a silent spinner
            logger.exception("turn failed")
            await updater.update_status(
                TaskState.failed,
                new_agent_text_message(
                    f"Sorry, that failed: {e}", context.context_id, context.task_id
                ),
                final=True,
            )
            return

        parts = [Part(root=TextPart(text=t)) for t in texts or errors if t.strip()]
        parts += [create_a2ui_part(m, version="0.9") for m in a2ui]
        if not parts:
            parts = [Part(root=TextPart(text="Done."))]
        await updater.update_status(
            TaskState.completed,
            new_agent_parts_message(parts, context.context_id, context.task_id),
            final=True,
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("cancel is not supported")
