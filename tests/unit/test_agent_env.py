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

import importlib
import os

import google.auth

import app.agent


def test_agent_imports_when_adc_has_no_project(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setattr(google.auth, "default", lambda: (object(), None))
    try:
        importlib.reload(app.agent)  # used to raise TypeError on setdefault(None)
        assert "GOOGLE_CLOUD_PROJECT" not in os.environ
    finally:
        monkeypatch.undo()
        importlib.reload(app.agent)
