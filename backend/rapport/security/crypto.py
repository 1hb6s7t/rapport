"""Local encryption for blobs and secrets (Fernet). Key file lives next to the data, mode 0600."""

from __future__ import annotations

import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class Crypto:
    def __init__(self, key_file: Path):
        self.key_file = key_file
        if key_file.exists():
            key = key_file.read_bytes().strip()
        else:
            key = Fernet.generate_key()
            key_file.parent.mkdir(parents=True, exist_ok=True)
            key_file.write_bytes(key)
            try:
                os.chmod(key_file, 0o600)
            except OSError:
                pass
        self._f = Fernet(key)

    def encrypt(self, data: bytes) -> bytes:
        return self._f.encrypt(data)

    def decrypt(self, token: bytes) -> bytes:
        try:
            return self._f.decrypt(token)
        except InvalidToken as e:
            raise ValueError("无法解密：密钥文件已更改或数据损坏") from e


class SecretStore:
    """Small encrypted JSON file for API keys and provider config."""

    def __init__(self, crypto: Crypto, path: Path):
        self.crypto = crypto
        self.path = path

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.crypto.decrypt(self.path.read_bytes()).decode())
        except (ValueError, json.JSONDecodeError):
            return {}

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(self.crypto.encrypt(json.dumps(data).encode()))
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass


class BlobStore:
    def __init__(self, crypto: Crypto, root: Path):
        self.crypto = crypto
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def _p(self, blob_id: str) -> Path:
        if not blob_id.isalnum():
            raise ValueError("bad blob id")
        return self.root / f"{blob_id}.bin"

    def put(self, blob_id: str, data: bytes) -> None:
        self._p(blob_id).write_bytes(self.crypto.encrypt(data))

    def get(self, blob_id: str) -> bytes | None:
        p = self._p(blob_id)
        if not p.exists():
            return None
        return self.crypto.decrypt(p.read_bytes())

    def delete(self, blob_id: str) -> bool:
        p = self._p(blob_id)
        if p.exists():
            p.unlink()
            return True
        return False

    def exists(self, blob_id: str) -> bool:
        return self._p(blob_id).exists()
