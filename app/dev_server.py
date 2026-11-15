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

"""Local A2A server for the Lit client: same agent, same executor, same A2UI.

    uv run uvicorn app.dev_server:app --port 8000

Serves A2A JSON-RPC at /a2a, the agent card at /.well-known/agent-card.json and
generated assets from ./.assets at /assets (unless ASSETS_BUCKET is set, in which
case assets come back as GCS signed URLs, exactly as in Gemini Enterprise).
"""

import os
from pathlib import Path

from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import TransportProtocol
from dotenv import load_dotenv
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

load_dotenv()

from app import config  # noqa: E402
from app.agent import app as adk_app  # noqa: E402
from app.card import build_agent_card  # noqa: E402
from app.executor import StudioAgentExecutor  # noqa: E402

PUBLIC_URL = os.getenv("DEV_SERVER_URL", "http://localhost:8000")

card = build_agent_card(
    adk_app, url=f"{PUBLIC_URL}/a2a", transport=TransportProtocol.jsonrpc
)
runner = Runner(app=adk_app, session_service=InMemorySessionService())
handler = DefaultRequestHandler(
    agent_executor=StudioAgentExecutor(runner=runner, agent_card=card),
    task_store=InMemoryTaskStore(),
)
a2a = A2AStarletteApplication(agent_card=card, http_handler=handler)

Path(config.LOCAL_ASSET_DIR).mkdir(parents=True, exist_ok=True)
app = Starlette(
    routes=[
        *a2a.routes(rpc_url="/a2a"),
        Mount("/assets", StaticFiles(directory=config.LOCAL_ASSET_DIR), name="assets"),
    ]
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("DEV_CORS_ORIGINS", "http://localhost:5173").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)
