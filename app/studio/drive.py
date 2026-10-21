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

"""Export to the end user's Google Drive, as the end user.

Why the user's token and not the agent's service account: files must land in *their*
Drive, owned by them and governed by their Workspace sharing rules and audit log. A
service account has no Drive of its own. The drive.file scope limits the agent to
files it created, so it can never read the rest of the user's Drive.

Plain REST with requests (google-auth's AuthorizedSession for local ADC); no client library.
"""

import dataclasses
import json
import uuid

import google.auth
import requests
from google.auth.transport.requests import AuthorizedSession

from app import config

FILES = "https://www.googleapis.com/drive/v3/files"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"
TOKENINFO = "https://oauth2.googleapis.com/tokeninfo"
SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/cloud-platform",
]


class DriveAuthRequired(Exception):
    """No user token: the agent has no Drive authorization in this environment."""


@dataclasses.dataclass
class DriveFile:
    name: str
    data: bytes
    mime: str
    description: str = ""


def session_for(token: str | None) -> requests.Session:
    if token:
        # A bare bearer token: it can't be refreshed, so a 401 should surface as is.
        s = requests.Session()
        s.headers["Authorization"] = f"Bearer {token}"
        return s
    if config.DRIVE_USE_ADC:
        credentials, _ = google.auth.default(scopes=SCOPES)
        return AuthorizedSession(credentials)
    raise DriveAuthRequired


def describe_token(token: str) -> dict:
    """What kind of token arrived, for logs. Never includes the token itself."""
    kind = "jwt" if token.count(".") == 2 and token.startswith("eyJ") else "opaque"
    param = "id_token" if kind == "jwt" else "access_token"
    try:
        info = requests.get(TOKENINFO, params={param: token}, timeout=10).json()
    except requests.RequestException as e:
        return {"kind": kind, "tokeninfo_error": type(e).__name__}
    keep = ("aud", "azp", "scope", "expires_in", "error", "error_description")
    return {"kind": kind, "length": len(token)} | {
        k: info[k] for k in keep if k in info
    }


def _folder(s: requests.Session, name: str, parent: str = "root") -> str:
    """Finds or creates a folder. drive.file only lists folders this app made."""
    escaped = name.replace("\\", "\\\\").replace("'", "\\'")
    q = (
        f"mimeType='{FOLDER_MIME}' and name='{escaped}' "
        f"and '{parent}' in parents and trashed=false"
    )
    r = s.get(FILES, params={"q": q, "fields": "files(id)", "spaces": "drive"})
    r.raise_for_status()
    if found := r.json().get("files"):
        return found[0]["id"]
    r = s.post(
        FILES,
        params={"fields": "id"},
        json={"name": name, "mimeType": FOLDER_MIME, "parents": [parent]},
    )
    r.raise_for_status()
    return r.json()["id"]


def _upload(s: requests.Session, f: DriveFile, parent: str) -> dict:
    boundary = uuid.uuid4().hex
    meta = {"name": f.name, "parents": [parent], "description": f.description}
    body = (
        (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(meta)}\r\n--{boundary}\r\nContent-Type: {f.mime}\r\n\r\n"
        ).encode()
        + f.data
        + f"\r\n--{boundary}--".encode()
    )
    r = s.post(
        UPLOAD,
        params={"uploadType": "multipart", "fields": "id,name,webViewLink"},
        data=body,
        headers={"Content-Type": f"multipart/related; boundary={boundary}"},
    )
    r.raise_for_status()
    return r.json()


def export(token: str | None, folder: str, files: list[DriveFile]) -> dict:
    """Uploads `files` to <DRIVE_ROOT_FOLDER>/<folder>/. Blocking; run in a thread."""
    s = session_for(token)
    parent = _folder(s, folder, _folder(s, config.DRIVE_ROOT_FOLDER))
    uploaded = [_upload(s, f, parent) for f in files]
    return {
        "folder_url": f"https://drive.google.com/drive/folders/{parent}",
        "files": [{"name": u["name"], "url": u["webViewLink"]} for u in uploaded],
    }
