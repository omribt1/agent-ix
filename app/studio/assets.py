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

"""Asset storage. The model context only ever sees asset ids and URLs, never bytes.

The UI renders in someone else's browser (Gemini Enterprise), so every asset needs
a URL that browser can fetch. On Agent Runtime that is a V4 signed GCS URL, signed
through the IAM signBlob API because the runtime credentials carry no private key.

Objects are never overwritten. Names are readable for auditing:
  chats/<session id>/v003-edit-image-1a2b3c4d5e.jpg
"""

import datetime
import functools
import io
import pathlib
import uuid
from collections import OrderedDict

from PIL import Image

from app import config

_EXT = {"image/png": "png", "image/jpeg": "jpg", "video/mp4": "mp4"}


def new_asset_id(kind: str) -> str:
    return f"{kind}-{uuid.uuid4().hex[:10]}"


def object_name(session_id: str, version: int, op: str, asset_id: str) -> str:
    """Readable, unique storage name (without extension) for one asset version."""
    return f"chats/{session_id}/v{version:03d}-{op}-{asset_id}"


def extension(mime_type: str) -> str:
    return _EXT.get(mime_type, "bin")


def _jpeg(img: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def preview(data: bytes) -> bytes | None:
    """A light JPEG for the card when the image is bigger than the card needs."""
    img = Image.open(io.BytesIO(data))
    if max(img.size) <= config.PREVIEW_SIDE * 1.25:
        return None
    img.thumbnail((config.PREVIEW_SIDE, config.PREVIEW_SIDE), Image.LANCZOS)
    return _jpeg(img, 85)


def model_input(data: bytes) -> bytes:
    """Bytes small enough to send inline to a model, at full resolution."""
    if len(data) <= config.MODEL_INPUT_MAX_BYTES:
        return data
    return _jpeg(Image.open(io.BytesIO(data)), 92)


class _BytesCache:
    """Small in-process LRU so edits and filters don't re-download the parent."""

    def __init__(self, max_items: int = 64):
        self._items: OrderedDict[str, bytes] = OrderedDict()
        self._max = max_items

    def get(self, key: str) -> bytes | None:
        if key in self._items:
            self._items.move_to_end(key)
        return self._items.get(key)

    def put(self, key: str, data: bytes) -> None:
        self._items[key] = data
        self._items.move_to_end(key)
        while len(self._items) > self._max:
            self._items.popitem(last=False)


class LocalAssetStore:
    def __init__(
        self,
        root: str = config.LOCAL_ASSET_DIR,
        base_url: str = config.LOCAL_ASSET_BASE_URL,
    ):
        self.root = pathlib.Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.base_url = base_url.rstrip("/")
        self._cache = _BytesCache()

    def _path(self, name: str) -> pathlib.Path:
        # Names embed the A2A contextId, which the client chooses: never let one
        # ("../..") point outside the asset root.
        path = (self.root / name).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError(f"Asset path escapes the asset root: {name!r}")
        return path

    def save(self, name: str, data: bytes, mime_type: str) -> str:
        name = f"{name}.{extension(mime_type)}"
        path = self._path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self._cache.put(name, data)
        return name

    def load(self, uri: str) -> bytes:
        data = self._cache.get(uri)
        if data is None:
            data = self._path(uri).read_bytes()
            self._cache.put(uri, data)
        return data

    def url(self, uri: str) -> str:
        return f"{self.base_url}/{uri}"


class GcsAssetStore:
    def __init__(self, bucket: str = config.ASSETS_BUCKET):
        from google.cloud import storage

        self._client = storage.Client()
        self._bucket = self._client.bucket(bucket)
        self._cache = _BytesCache()
        self._credentials = None

    def save(self, name: str, data: bytes, mime_type: str) -> str:
        name = f"{name}.{extension(mime_type)}"
        self._bucket.blob(name).upload_from_string(data, content_type=mime_type)
        self._cache.put(name, data)
        return name

    def load(self, uri: str) -> bytes:
        data = self._cache.get(uri)
        if data is None:
            data = self._bucket.blob(uri).download_as_bytes()
            self._cache.put(uri, data)
        return data

    def _signing_credentials(self):
        """ADC for signing, loaded once and refreshed only when the token expires."""
        import google.auth
        from google.auth.transport.requests import Request

        if self._credentials is None:
            self._credentials, _ = google.auth.default()
        if not self._credentials.valid:
            self._credentials.refresh(Request())
        return self._credentials

    def url(self, uri: str) -> str:
        credentials = self._signing_credentials()
        kwargs = {}
        if not hasattr(credentials, "sign_bytes") or not getattr(
            credentials, "signer", None
        ):
            # Metadata-server credentials can't sign locally; let GCS call signBlob.
            kwargs = {
                "service_account_email": credentials.service_account_email,
                "access_token": credentials.token,
            }
        return self._bucket.blob(uri).generate_signed_url(
            version="v4",
            expiration=datetime.timedelta(hours=config.SIGNED_URL_TTL_HOURS),
            method="GET",
            **kwargs,
        )


@functools.cache
def store() -> LocalAssetStore | GcsAssetStore:
    if config.ASSET_BACKEND == "gcs":
        return GcsAssetStore()
    return LocalAssetStore()
