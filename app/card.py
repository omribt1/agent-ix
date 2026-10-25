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

"""The A2A agent card, shared by Agent Runtime and the local dev server."""

import os

from a2a.types import AgentCapabilities, AgentCard, AgentSkill, TransportProtocol
from a2ui.a2a.extension import get_a2ui_agent_extension
from google.adk.apps import App

from app.studio.surface import catalog


def build_agent_card(
    app: App,
    url: str = "http://localhost:9999/",
    transport: TransportProtocol = TransportProtocol.http_json,
) -> AgentCard:
    """Advertises A2UI v0.9 with the Gemini Enterprise composite catalog."""
    return AgentCard(
        name="Agent-I:X Creative Studio",
        description=app.root_agent.description,
        url=url,
        version=os.getenv("AGENT_VERSION", "0.1.0"),
        protocol_version="0.3.0",
        preferred_transport=transport,
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain", "application/json"],
        capabilities=AgentCapabilities(
            streaming=os.getenv("A2A_STREAMING", "false").lower() == "true",
            extensions=[
                get_a2ui_agent_extension(
                    "0.9", supported_catalog_ids=[catalog().catalog_id]
                )
            ],
        ),
        skills=[
            AgentSkill(
                id="creative_studio",
                name="Generate and refine media",
                description=(
                    "Creates an image (and optionally a short Omni clip), critiques it, "
                    "and opens an interactive studio tailored to that asset."
                ),
                tags=["image", "video", "a2ui", "creative"],
                examples=[
                    "A hero shot of a ceramic coffee mug for our spring campaign",
                    "Portrait of a barista in warm morning light, 4:5",
                ],
            )
        ],
    )
