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

"""Creates the Gemini Enterprise authorization the agent registration needs.

Agent Runtime's A2A endpoint is a Google Cloud API, so every call needs an OAuth
token, and GE only sends one to an A2A agent when an authorization is attached: it
signs the user in with this client and calls the agent with their token. The
cloud-platform scope is what lets that token call Agent Runtime.

Usage (the client JSON is the file downloaded from the OAuth client page):
  uv run python deployment/create_ge_authorization.py PROJECT_NUMBER client_secret.json [AUTH_ID]

Prints only the authorization name, never the secret.
"""

import json
import subprocess
import sys
import urllib.parse

import requests

SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
REDIRECT_URI = "https://vertexaisearch.cloud.google.com/static/oauth/oauth.html"


def main(project_number: str, client_file: str, auth_id: str = "agent-ix-auth") -> None:
    with open(client_file) as f:
        client = json.load(f)
    client = client.get("web") or client.get("installed") or client
    query = urllib.parse.urlencode(
        {
            "client_id": client["client_id"],
            "redirect_uri": REDIRECT_URI,
            "scope": " ".join(SCOPES),
            "include_granted_scopes": "true",
            "response_type": "code",
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    parent = f"projects/{project_number}/locations/global"
    body = {
        "name": f"{parent}/authorizations/{auth_id}",
        "serverSideOauth2": {
            "clientId": client["client_id"],
            "clientSecret": client["client_secret"],
            "authorizationUri": f"https://accounts.google.com/o/oauth2/v2/auth?{query}",
            "tokenUri": "https://oauth2.googleapis.com/token",
        },
    }
    token = subprocess.run(
        ["gcloud", "auth", "print-access-token"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    r = requests.post(
        f"https://discoveryengine.googleapis.com/v1alpha/{parent}/authorizations",
        params={"authorizationId": auth_id},
        headers={
            "Authorization": f"Bearer {token}",
            "X-Goog-User-Project": project_number,
        },
        json=body,
        timeout=60,
    )
    if not r.ok:
        sys.exit(
            f"Failed ({r.status_code}): {r.json().get('error', {}).get('message')}"
        )
    print(f"Created {r.json()['name']}")


if __name__ == "__main__":
    main(*sys.argv[1:])
