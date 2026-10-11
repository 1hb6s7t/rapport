from __future__ import annotations

import hashlib
import html
import io
import json
import re
import sqlite3
import threading
import time
import uuid
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from rapport.archives.chunks import CHUNK_SIZE, read_chunks, write_chunks
from rapport.db import Import
from rapport.errors import RapportError
from rapport.schemas.message_v1 import MEDIA_TYPES

MAX_ASSET_BYTES = 512 * 1024 * 1024
_SERVICE_LOCK = threading.RLock()


def _json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_filename(value: str) -> str:
    value = re.split(r"[\\/]", value or "media")[-1]
    return re.sub(r"[\x00-\x1f\x7f<>:\"|?*]", "_", value)[:160].strip(" .") or "media"


def _mime(head: bytes) -> str:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:8] == b"ftyp":
        return "video/mp4"
    if head.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm"
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return "audio/wav"
    if head.startswith(b"ID3"):
        return "audio/mpeg"
    return "application/octet-stream"


class ArchiveStore:
    def __init__(self, state):
        self.state = state
        self.root = state.settings.data_dir / "archive"
        self.root.mkdir(parents=True, exist_ok=True)
        self.objects = self.root / "objects"
        self.objects.mkdir(exist_ok=True)
        self.index = self.root / "index.sqlite3"
        self.lock = threading.RLock()
        self.tickets: dict[str, dict] = {}
        with self._connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS archives (id TEXT PRIMARY KEY, relationship_id TEXT NOT NULL, digest TEXT NOT NULL, metadata BLOB NOT NULL, UNIQUE(relationship_id,digest))")
            connection.execute("CREATE TABLE IF NOT EXISTS assets (id TEXT PRIMARY KEY, archive_id TEXT NOT NULL, metadata BLOB NOT NULL, FOREIGN KEY(archive_id) REFERENCES archives(id) ON DELETE CASCADE)")
            connection.execute("CREATE INDEX IF NOT EXISTS archive_relationship ON archives(relationship_id)")
            connection.execute("CREATE INDEX IF NOT EXISTS asset_archive ON assets(archive_id)")

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.index, timeout=15)
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _encode(self, data):
        return self.state.crypto.encrypt(_json(data))

    def _decode(self, data):
        try:
            return json.loads(self.state.crypto.decrypt(data))
        except (ValueError, json.JSONDecodeError) as exc:
            raise RapportError("archive_corrupt", "归档数据损坏或无法解密", status=409) from exc

    def _path(self, object_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", object_id):
            raise RapportError("archive_not_found", "归档或媒体不存在", status=404)
        return self.objects / f"{object_id}.enc"

    def _scope(self, relationship_id: str) -> str:
        return self.state.resolve_relationship_id(relationship_id)

    def get(self, archive_id: str, relationship_id: str) -> dict:
        rid = self._scope(relationship_id)
        with self._connect() as connection:
            row = connection.execute("SELECT metadata FROM archives WHERE id=? AND relationship_id=?", (archive_id, rid)).fetchone()
            count = connection.execute("SELECT COUNT(*) FROM assets WHERE archive_id=?", (archive_id,)).fetchone()[0] if row else 0
        if not row:
            raise RapportError("archive_not_found", "该对象下没有此归档", status=404)
        metadata = self._decode(row[0])
        return {**metadata, "asset_count": count}

    def list(self, relationship_id: str) -> list[dict]:
        rid = self._scope(relationship_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT id FROM archives WHERE relationship_id=? ORDER BY rowid DESC", (rid,)).fetchall()
        return [self.get(row[0], rid) for row in rows]

    def create(self, import_id: str, relationship_id: str) -> dict:
        rid = self._scope(relationship_id)
        with self.state.db.session() as session:
            imported = session.get(Import, import_id)
            if not imported or imported.deleted or imported.relationship_id != rid:
                raise RapportError("archive_import_not_found", "该对象下没有此导入", status=404)
            if imported.status != "confirmed" or imported.confirmed_at is None:
                raise RapportError("archive_import_unconfirmed", "请先校对并确认聊天，再创建长期归档", status=409)
            conversation = self.state.load_conversation(imported)
            if conversation is None:
                raise RapportError("archive_import_expired", "临时材料已清理，请重新导入", status=410)
            selected = conversation.messages[imported.range_start or 0 : (imported.range_end if imported.range_end is not None else len(conversation.messages) - 1) + 1]
        # Do not copy raw platform IDs, filenames, paths or arbitrary parser metadata.
        records = [{"source_message_id": m.id, "seq": i, "sender": m.sender.value, "ts": m.ts.isoformat() if m.ts else None, "ts_precision": m.ts_precision.value, "type": m.type.value, "text": m.text, "quoted_text": m.quoted_text} for i, m in enumerate(selected)]
        canonical = [{k: v for k, v in item.items() if k != "source_message_id"} for item in records]
        digest = hashlib.sha256(_json({"relationship_id": rid, "messages": canonical})).hexdigest()
        with self.lock, self._connect() as connection:
            existing = connection.execute("SELECT id FROM archives WHERE relationship_id=? AND digest=?", (rid, digest)).fetchone()
            if existing:
                return self.get(existing[0], rid)
            archive_id = uuid.uuid4().hex
            for item in records:
                item["message_ref"] = f"{archive_id}:{item['seq']:08d}"
            metadata = {"id": archive_id, "relationship_id": rid, "title": f"聊天归档 · {len(records)} 条", "created_at": _now(), "message_count": len(records), "content_hash": digest, "source_import_id": import_id, "source": conversation.source.value, "timezone": conversation.timezone, "participants": conversation.participants.model_dump(), "retention": "until_deleted", "schema_version": "archive-v1"}
            payload = _json(records)
            write_chunks(self._path(archive_id), self.state.crypto, (payload,), max_bytes=self.state.settings.max_text_bytes * 3)
            try:
                connection.execute("INSERT INTO archives VALUES(?,?,?,?)", (archive_id, rid, digest, self._encode(metadata)))
                for item in records:
                    if item["type"] in {m.value for m in MEDIA_TYPES}:
                        asset_id = uuid.uuid4().hex
                        asset = {"id": asset_id, "archive_id": archive_id, "relationship_id": rid, "message_ref": item["message_ref"], "kind": item["type"], "status": "missing", "mime": None, "sha256": None, "size_bytes": 0, "filename": None, "caption": "", "created_at": _now(), "source": "message_placeholder"}
                        connection.execute("INSERT INTO assets VALUES(?,?,?)", (asset_id, archive_id, self._encode(asset)))
            except BaseException:
                self._path(archive_id).unlink(missing_ok=True)
                raise
        return self.get(archive_id, rid)

    def messages(self, archive_id: str, relationship_id: str) -> list[dict]:
        self.get(archive_id, relationship_id)
        try:
            return json.loads(b"".join(read_chunks(self._path(archive_id), self.state.crypto)))
        except (ValueError, OSError) as exc:
            raise RapportError("archive_corrupt", "归档文件缺失或校验失败", status=409) from exc

    def assets(self, archive_id: str, relationship_id: str) -> list[dict]:
        self.get(archive_id, relationship_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT metadata FROM assets WHERE archive_id=? ORDER BY rowid", (archive_id,)).fetchall()
        return [self._decode(row[0]) for row in rows]

    def get_asset(self, asset_id: str, relationship_id: str) -> dict:
        rid = self._scope(relationship_id)
        with self._connect() as connection:
            row = connection.execute("SELECT assets.metadata FROM assets JOIN archives ON archives.id=assets.archive_id WHERE assets.id=? AND archives.relationship_id=?", (asset_id, rid)).fetchone()
        if not row:
            raise RapportError("archive_asset_not_found", "该对象下没有此媒体", status=404)
        return self._decode(row[0])

    describe_asset = get_asset

    def add_asset(self, archive_id: str, relationship_id: str, message_ref: str, source: BinaryIO, filename: str, kind: str | None = None, status: str = "original", caption: str = "", source_kind: str = "user_selected_upload") -> dict:
        rid = self._scope(relationship_id)
        messages = self.messages(archive_id, rid)
        message = next((m for m in messages if m["message_ref"] == message_ref), None)
        if message is None:
            raise RapportError("archive_message_not_found", "媒体必须关联此归档中的消息", status=404)
        if status not in {"original", "thumbnail"}:
            raise RapportError("archive_bad_status", "上传状态只能是原件或缩略图", status=422)
        if source_kind not in {"user_selected_upload", "wechat_save_as"}:
            raise RapportError("archive_bad_source", "未知媒体获取方式", status=422)
        head = source.read(CHUNK_SIZE)
        mime = _mime(head)
        kind = kind or (message["type"] if message["type"] in {m.value for m in MEDIA_TYPES} else "file")
        if kind not in {m.value for m in MEDIA_TYPES}:
            raise RapportError("archive_bad_kind", "不支持的媒体类型", status=422)
        if kind in {"image", "sticker"} and not mime.startswith("image/"):
            raise RapportError("archive_bad_media", "文件不是支持的图片或表情格式", status=422)
        if status == "thumbnail" and not mime.startswith("image/"):
            raise RapportError("archive_bad_media", "缩略图必须是图片", status=422)
        if kind == "video" and status == "original" and not mime.startswith("video/"):
            raise RapportError("archive_bad_media", "文件不是支持的视频格式", status=422)
        if not head:
            raise RapportError("archive_empty_file", "不能上传空文件", status=422)
        asset_id = uuid.uuid4().hex

        def chunks():
            yield head
            while block := source.read(CHUNK_SIZE):
                yield block

        try:
            size, digest = write_chunks(self._path(asset_id), self.state.crypto, chunks(), MAX_ASSET_BYTES)
        except ValueError as exc:
            raise RapportError("archive_asset_too_large", "单个媒体最多 512 MiB", status=413) from exc
        asset = {"id": asset_id, "archive_id": archive_id, "relationship_id": rid, "message_ref": message_ref, "kind": kind, "status": status, "mime": mime, "sha256": digest, "size_bytes": size, "filename": _safe_filename(filename), "caption": caption[:500], "created_at": _now(), "source": source_kind}
        with self.lock:
            try:
                self.get(archive_id, rid)  # may have been deleted during upload
                with self._connect() as connection:
                    existing = connection.execute("SELECT id,metadata FROM assets WHERE archive_id=?", (archive_id,)).fetchall()
                    for _old_id, encoded in existing:
                        old = self._decode(encoded)
                        if old["message_ref"] == message_ref and old["status"] == status and old["sha256"] == digest:
                            self._path(asset_id).unlink(missing_ok=True)
                            return old
                    connection.execute("INSERT INTO assets VALUES(?,?,?)", (asset_id, archive_id, self._encode(asset)))
                    # A thumbnail does not make the original available.
                    if status == "original":
                        for old_id, encoded in existing:
                            old = self._decode(encoded)
                            if old["message_ref"] == message_ref and old["status"] == "missing":
                                connection.execute("DELETE FROM assets WHERE id=?", (old_id,))
            except BaseException:
                self._path(asset_id).unlink(missing_ok=True)
                raise
        return asset

    def iter_asset(self, asset_id: str, relationship_id: str) -> Iterator[bytes]:
        asset = self.get_asset(asset_id, relationship_id)
        if asset["status"] == "missing":
            raise RapportError("archive_media_missing", "此媒体原文件尚未取得", status=404)
        try:
            digest = hashlib.sha256()
            size = 0
            for chunk in read_chunks(self._path(asset_id), self.state.crypto):
                digest.update(chunk)
                size += len(chunk)
                yield chunk
            if digest.hexdigest() != asset["sha256"] or size != asset["size_bytes"]:
                raise ValueError("archive_index_mismatch")
        except (ValueError, OSError) as exc:
            raise RapportError("archive_corrupt", "媒体文件缺失或校验失败", status=409) from exc

    def read_asset(self, asset_id: str, relationship_id: str, max_bytes: int = 32 * 1024 * 1024) -> bytes:
        asset = self.get_asset(asset_id, relationship_id)
        if asset["size_bytes"] > max_bytes:
            raise RapportError("archive_asset_too_large", "此操作不支持该媒体大小", status=413)
        return b"".join(self.iter_asset(asset_id, relationship_id))

    def list_assets_for_relationship(self, relationship_id: str, limit: int = 20) -> list[dict]:
        result = []
        for archive in self.list(relationship_id):
            result.extend(a for a in self.assets(archive["id"], relationship_id) if a["status"] == "original")
            if len(result) >= limit:
                break
        return result[: max(0, min(limit, 100))]

    def search(self, relationship_id: str, query: str, limit: int = 6) -> list[dict]:
        terms = set(re.findall(r"[a-zA-Z0-9]{2,}|[\u3400-\u9fff]{1,2}", query.lower()))
        if not terms:
            return []
        found = []
        for archive in self.list(relationship_id):
            for message in self.messages(archive["id"], relationship_id):
                text = message.get("text") or ""
                score = sum(term in text.lower() for term in terms) / len(terms)
                if score:
                    found.append({"archive_id": archive["id"], "message_ref": message["message_ref"], "sender": message["sender"], "text": text, "ts": message["ts"], "score": score, "retrieval_method": "lexical-v1"})
        return sorted(found, key=lambda item: item["score"], reverse=True)[: max(0, min(limit, 50))]

    def delete(self, archive_id: str, relationship_id: str) -> None:
        with self.lock:
            self.get(archive_id, relationship_id)
            with self._connect() as connection:
                ids = [row[0] for row in connection.execute("SELECT id FROM assets WHERE archive_id=?", (archive_id,))]
                connection.execute("DELETE FROM archives WHERE id=?", (archive_id,))
            for object_id in [archive_id, *ids]:
                self._path(object_id).unlink(missing_ok=True)
            self.tickets = {key: value for key, value in self.tickets.items() if value["archive_id"] != archive_id}

    def delete_relationship(self, relationship_id: str) -> None:
        # Called before OR after the relationship DB row is removed.
        with self.lock, self._connect() as connection:
            archive_ids = [row[0] for row in connection.execute("SELECT id FROM archives WHERE relationship_id=?", (relationship_id,))]
            asset_ids = [row[0] for row in connection.execute("SELECT assets.id FROM assets JOIN archives ON assets.archive_id=archives.id WHERE archives.relationship_id=?", (relationship_id,))]
            connection.execute("DELETE FROM archives WHERE relationship_id=?", (relationship_id,))
            for object_id in archive_ids + asset_ids:
                self._path(object_id).unlink(missing_ok=True)
            self.tickets = {key: value for key, value in self.tickets.items() if value["relationship_id"] != relationship_id}

    def wipe_all(self) -> None:
        with self.lock, self._connect() as connection:
            connection.execute("DELETE FROM archives")
            for path in self.objects.glob("*.enc"):
                path.unlink(missing_ok=True)
            for path in self.objects.glob("*.part"):
                path.unlink(missing_ok=True)
            self.tickets.clear()

    def export_preview(self, archive_id: str, relationship_id: str, owner: str, include_media: bool = False) -> dict:
        archive = self.get(archive_id, relationship_id)
        assets = self.assets(archive_id, relationship_id)
        signature = hashlib.sha256(_json({"archive": archive, "assets": assets})).hexdigest()
        manifest = {"archive_id": archive_id, "message_count": archive["message_count"], "asset_count": len(assets), "included_media_count": sum(a["status"] != "missing" for a in assets) if include_media else 0, "missing_media_count": sum(a["status"] == "missing" for a in assets), "include_media": include_media, "plaintext": True, "files": ["messages.jsonl", "viewer.html", "media-manifest.json"], "warning": "导出包包含明文聊天和所选媒体，请自行安全保存。不会包含密钥、数据库、API 配置或源文件路径。"}
        token = uuid.uuid4().hex
        with self.lock:
            self.tickets = {key: value for key, value in self.tickets.items() if value["expires"] > time.monotonic()}
            if len(self.tickets) >= 100:
                self.tickets.pop(next(iter(self.tickets)))
            self.tickets[token] = {"archive_id": archive_id, "relationship_id": relationship_id, "owner": owner, "include_media": include_media, "signature": signature, "expires": time.monotonic() + 600}
        return {"token": token, "manifest": manifest, "expires_at": datetime.fromtimestamp(time.time() + 600, UTC).isoformat()}

    def export_confirm(self, archive_id: str, relationship_id: str, owner: str, token: str, include_media: bool, confirmed: bool) -> Iterator[bytes]:
        if not confirmed:
            raise RapportError("archive_export_confirmation", "请确认导出明文聊天与媒体", status=422)
        archive = self.get(archive_id, relationship_id)
        assets = self.assets(archive_id, relationship_id)
        signature = hashlib.sha256(_json({"archive": archive, "assets": assets})).hexdigest()
        with self.lock:
            ticket = self.tickets.get(token)
            if not ticket or ticket["expires"] <= time.monotonic() or any(ticket[key] != value for key, value in {"archive_id": archive_id, "relationship_id": relationship_id, "owner": owner, "include_media": include_media, "signature": signature}.items()):
                raise RapportError("archive_export_stale", "导出预览已过期、发生变化或属于其他会话，请重新预览", status=409)
            del self.tickets[token]
        return self._zip(archive, assets, include_media)

    def _zip(self, archive: dict, assets: list[dict], include_media: bool) -> Iterator[bytes]:
        archive_id, rid = archive["id"], archive["relationship_id"]
        messages = self.messages(archive_id, rid)
        ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp", "video/mp4": ".mp4", "video/webm": ".webm", "audio/ogg": ".ogg", "audio/wav": ".wav", "audio/mpeg": ".mp3"}
        manifest = [{**asset, "export_path": f"media/{asset['id']}{ext.get(asset['mime'], '.bin')}" if include_media and asset["status"] != "missing" else None} for asset in assets]
        # No embedded JSON, JavaScript, remote resources, source filenames or unescaped chat HTML.
        by_message: dict[str, list] = {}
        for asset in manifest:
            by_message.setdefault(asset["message_ref"], []).append(asset)
        rows = []
        for message in messages:
            links = []
            for asset in by_message.get(message["message_ref"], []):
                label = html.escape(f"{asset['kind']} · {asset['status']}")
                links.append(f'<a href="{asset["export_path"]}">{label}</a>' if asset["export_path"] else label)
            rows.append(f'<article><small>{html.escape(message["sender"])} · {html.escape(message["ts"] or "时间未知")} · {html.escape(message["ts_precision"])}</small><p>{html.escape(message["text"] or "[非文字消息]")}</p>{" · ".join(links)}</article>')
        viewer = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; img-src \'self\'; media-src \'self\'"><title>心迹聊天归档</title><style>body{font:16px system-ui;max-width:850px;margin:40px auto;padding:20px;background:#fff8fa;color:#443546}article{background:white;padding:18px;margin:12px 0;border:1px solid #ead9e4;border-radius:14px}p{white-space:pre-wrap;overflow-wrap:anywhere}small{color:#756276}a{color:#98496e}</style><h1>心迹聊天归档</h1><p>本文件包含明文聊天。时间精度以原材料为准。</p>' + "".join(rows) + "</html>"
        sink = _ZipSink()
        with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zipped:
            for name, data in [("messages.jsonl", b"\n".join(_json(message) for message in messages)), ("media-manifest.json", _json(manifest)), ("viewer.html", viewer.encode("utf-8"))]:
                with zipped.open(name, "w", force_zip64=True) as output:
                    for start in range(0, len(data), CHUNK_SIZE):
                        output.write(data[start : start + CHUNK_SIZE])
                        yield sink.drain()
                yield sink.drain()
            for asset in manifest:
                if asset["export_path"]:
                    with zipped.open(asset["export_path"], "w", force_zip64=True) as output:
                        for chunk in self.iter_asset(asset["id"], rid):
                            output.write(chunk)
                            yield sink.drain()
                    yield sink.drain()
        yield sink.drain()


class _ZipSink(io.RawIOBase):
    def __init__(self):
        self.buffer = bytearray()
        self.position = 0

    def writable(self):
        return True

    def seekable(self):
        return False

    def write(self, data):
        self.buffer.extend(data)
        self.position += len(data)
        return len(data)

    def tell(self):
        return self.position

    def drain(self):
        data = bytes(self.buffer)
        self.buffer.clear()
        return data


def get_service(state) -> ArchiveStore:
    with _SERVICE_LOCK:
        if not hasattr(state, "archives"):
            state.archives = ArchiveStore(state)
        return state.archives
