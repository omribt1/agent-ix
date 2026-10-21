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

"""Drive REST calls, against a fake session (no network)."""

import json

import pytest

from app.studio import drive


class _Response:
    def __init__(self, body: dict):
        self._body = body

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._body


class FakeSession:
    """Drive with no folders yet; records every request."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.created = 0

    def get(self, url, params=None):
        self.calls.append(("GET", url, params or {}))
        return _Response({"files": []})

    def post(self, url, params=None, json=None, data=None, headers=None):
        self.calls.append(
            (
                "POST",
                url,
                {"params": params, "json": json, "data": data, "headers": headers},
            )
        )
        if url == drive.FILES:
            self.created += 1
            return _Response({"id": f"folder-{self.created}"})
        return _Response(
            {"id": "f1", "name": "Mug v1.png", "webViewLink": "https://d/f1"}
        )


@pytest.fixture
def session(monkeypatch):
    fake = FakeSession()
    monkeypatch.setattr(drive, "session_for", lambda token: fake)
    return fake


def test_export_creates_folders_and_uploads_multipart(session):
    out = drive.export(
        "tok", "Mug's hero", [drive.DriveFile("Mug v1.png", b"PNG", "image/png", "v1")]
    )
    assert out == {
        "folder_url": "https://drive.google.com/drive/folders/folder-2",
        "files": [{"name": "Mug v1.png", "url": "https://d/f1"}],
    }
    lookup = session.calls[2][2]["q"]
    assert "name='Mug\\'s hero'" in lookup and "'folder-1' in parents" in lookup
    upload = session.calls[-1][2]
    assert upload["params"]["uploadType"] == "multipart"
    boundary = upload["headers"]["Content-Type"].split("boundary=")[1]
    meta = upload["data"].split(b"\r\n\r\n")[1].split(b"\r\n")[0]
    assert json.loads(meta) == {
        "name": "Mug v1.png",
        "parents": ["folder-2"],
        "description": "v1",
    }
    assert upload["data"].endswith(f"PNG\r\n--{boundary}--".encode())


def test_no_token_and_no_adc_needs_authorization(monkeypatch):
    monkeypatch.setattr(drive.config, "DRIVE_USE_ADC", False)
    with pytest.raises(drive.DriveAuthRequired):
        drive.session_for(None)


def test_user_token_is_sent_as_is_and_never_refreshed():
    s = drive.session_for("user-token")
    assert s.headers["Authorization"] == "Bearer user-token"
    assert not hasattr(s, "credentials")
