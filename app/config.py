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

"""Model IDs and runtime settings. Everything is overridable via env vars."""

import os

AGENT_MODEL = os.getenv("AGENT_MODEL", "gemini-3.8-flash")
CRITIC_MODEL = os.getenv("CRITIC_MODEL", "gemini-3.8-flash")
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "gemini-3.1-flash-image")  # Nano Banana 2
IMAGE_PRO_MODEL = os.getenv("IMAGE_PRO_MODEL", "gemini-3-pro-image")  # Nano Banana Pro
IMAGE_LITE_MODEL = os.getenv("IMAGE_LITE_MODEL", "gemini-3.1-flash-lite-image")
# The user picks a tier (first message or the studio's model toggle); Lite is the
# default because quick iterations matter more than fidelity until the last round.
IMAGE_TIERS = {"lite": IMAGE_LITE_MODEL, "nb2": IMAGE_MODEL, "pro": IMAGE_PRO_MODEL}
DEFAULT_IMAGE_TIER = os.getenv("DEFAULT_IMAGE_TIER", "lite")
# Aspect ratios offered for generation and reframing (all supported by Nano Banana).
ASPECT_RATIOS = ["1:1", "4:5", "3:4", "16:9", "9:16"]
# NB2 and Pro render up to 4K; Lite takes no size setting, so upscaling from Lite uses NB2.
UPSCALE_SIZES = ["2K", "4K"]
# Images larger than this (2K and 4K upscales) get a JPEG preview for the card; the
# full file stays the version of record (Drive export, "Open full size", filters).
PREVIEW_SIDE = int(os.getenv("PREVIEW_SIDE", "1600"))
# Side of the image embedded in the studio card for the live filter preview
# (app/studio/live_preview.py); ~80-120 KB per card at 800.
LIVE_PREVIEW_SIDE = int(os.getenv("LIVE_PREVIEW_SIDE", "800"))
# Model inputs above this are re-encoded as full-resolution JPEG (inline request limit).
MODEL_INPUT_MAX_BYTES = 8 * 1024 * 1024
# Omni is only served through the Interactions API, not generateContent.
VIDEO_MODEL = os.getenv("VIDEO_MODEL", "gemini-omni-1.1-flash-preview")

# Where generated assets live. `gcs` needs ASSETS_BUCKET and returns V4 signed
# URLs (what Gemini Enterprise needs). `local` writes to ./.assets and serves
# them from the dev server, for the local Lit client only.
ASSET_BACKEND = os.getenv(
    "ASSET_BACKEND", "gcs" if os.getenv("ASSETS_BUCKET") else "local"
)
ASSETS_BUCKET = os.getenv("ASSETS_BUCKET", "")
LOCAL_ASSET_DIR = os.getenv("LOCAL_ASSET_DIR", ".assets")
LOCAL_ASSET_BASE_URL = os.getenv("LOCAL_ASSET_BASE_URL", "http://localhost:8000/assets")
SIGNED_URL_TTL_HOURS = int(os.getenv("SIGNED_URL_TTL_HOURS", "24"))

# `inline` renders the studio as a card in the chat stream; `canvas` renders
# it in Gemini Enterprise's resizable side panel.
STUDIO_LAYOUT = os.getenv("STUDIO_LAYOUT", "inline")

# Export to the end user's Google Drive, as the user (drive.file scope: the app only
# sees files it created). In Gemini Enterprise the user's OAuth token comes from the
# agent's authorization resource: in the Authorization header, or in session state
# under DRIVE_AUTH_ID. DRIVE_USE_ADC=true uses your own ADC instead, for local dev:
#   gcloud auth application-default login \
#     --scopes=openid,https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/drive.file
DRIVE_AUTH_ID = os.getenv("DRIVE_AUTH_ID", "")
DRIVE_USE_ADC = os.getenv("DRIVE_USE_ADC", "false").lower() == "true"
DRIVE_ROOT_FOLDER = os.getenv("DRIVE_ROOT_FOLDER", "Agent-I:X")
# Agent Runtime replaces the Authorization header with its own ID token, so an A2A
# agent there never sees the user's token. DRIVE_EXPORT=false swaps "Save to Drive"
# for a Download button (a signed link to the full file).
DRIVE_EXPORT = os.getenv("DRIVE_EXPORT", "true").lower() == "true"
