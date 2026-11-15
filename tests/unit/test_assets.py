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

import google.auth
import pytest

from app.studio import assets


@pytest.fixture
def local(tmp_path):
    return assets.LocalAssetStore(root=str(tmp_path / "assets"), base_url="http://t")


def test_local_names_cannot_escape_the_root(local, tmp_path):
    # The session id in a name is the client's A2A contextId.
    with pytest.raises(ValueError, match="escapes"):
        local.save("chats/../../outside/v001-x", b"data", "image/png")
    assert not (tmp_path / "outside").exists()
    with pytest.raises(ValueError, match="escapes"):
        local.load("../secret.txt")


def test_local_reads_are_cached_after_a_miss(local):
    uri = local.save("chats/c1/v001-original-image-1", b"PNG", "image/png")
    fresh = assets.LocalAssetStore(root=str(local.root), base_url="http://t")
    assert fresh.load(uri) == b"PNG"
    (local.root / uri).unlink()
    assert fresh.load(uri) == b"PNG"  # second read comes from the cache


class _Creds:
    def __init__(self):
        self.valid = False
        self.refreshes = 0

    def refresh(self, request):
        self.refreshes += 1
        self.valid = True


def test_signing_credentials_load_once_and_refresh_only_when_expired(monkeypatch):
    creds, loads = _Creds(), []

    def default():
        loads.append(1)
        return creds, "p"

    monkeypatch.setattr(google.auth, "default", default)
    gcs = object.__new__(assets.GcsAssetStore)  # skip the storage client
    gcs._credentials = None
    for _ in range(3):
        assert gcs._signing_credentials() is creds
    assert (len(loads), creds.refreshes) == (1, 1)
    creds.valid = False  # token expired
    gcs._signing_credentials()
    assert (len(loads), creds.refreshes) == (1, 2)
