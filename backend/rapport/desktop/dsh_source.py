"""DSH ChatSource adaptation and durable incremental batches.

Adapted from huaqian695-sudo/dsh-wechat-bot (MIT, Copyright 2025), commit
42d672ea8aa49925637973fe11194f768bc92af6, infra/wechat/db.py and listener.py.
See LICENSE-dsh-wechat-bot.txt. Changes: exact recipient binding, actual sender
mapping, message-v1 output, and encrypted batch+cursor commits with no sends.
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from uuid import uuid4
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from rapport.desktop import dsh_database
from rapport.desktop.dsh_directory import DshDirectory
from rapport.desktop.dsh_voice import DshVoiceTranscriber
from rapport.desktop.dsh_voice import summary as voice_summary
from rapport.desktop.wechat import normalized_title
from rapport.errors import RapportError
from rapport.schemas.message_v1 import (
    Conversation,
    Message,
    MsgType,
    ParseWarning,
    Participants,
    Sender,
    Source,
    TsPrecision,
)

DATABASE_ENABLED_SETTING = "desktop.wechat.database"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class DBAdapter:
    """DSH's reader port, restricted to one exact private contact per read."""
    def __init__(self, reader, account):
        self._db = reader
        self.account = account
        info = reader.get_self_info()
        self.self_username = info.get("username", "")
        self.self_nick = info.get("nick_name", "")
        if not self.self_username:
            raise RapportError("dsh_self_unknown", "未能核对数据库账号。", status=409)

    @property
    def raw(self):
        return self._db

    def resolve_contact(self, name, contact_id=None):
        if contact_id:
            friend = self._db.get_friend(contact_id)
            if not friend or contact_id == self.self_username or contact_id.endswith("@chatroom"):
                raise RapportError("dsh_contact_changed", "所选好友已不在此账号的好友列表中，请刷新好友。", status=409)
            if not any(normalized_title(value or "") == normalized_title(name)
                       for value in (friend.get("remark"), friend.get("nick_name"), friend["username"])):
                raise RapportError("dsh_contact_changed", "好友备注或昵称已变化，请刷新好友后重新选择。", status=409)
            return friend["username"]
        results = self._db.search_contact(name)
        if len(results) >= 50:
            raise RapportError("dsh_contact_ambiguous", "同名候选过多，请使用唯一完整备注。", status=409)
        exact = {r["username"]: r for r in results if r.get("username")
                 and any(normalized_title(r.get(key) or "") == normalized_title(name) for key in ("remark", "nick_name"))}
        if len(exact) != 1:
            raise RapportError("dsh_contact_ambiguous", "未找到唯一且完整匹配的好友，请核对备注。", status=409)
        user = next(iter(exact))
        if user.endswith("@chatroom") or user == self.self_username:
            raise RapportError("dsh_private_contact_only", "请选择一位私聊好友。", status=422)
        return user

    def messages(self, user, limit=100, cursors=None):
        return self._db.fetch(user, limit, cursors)

    def convert(self, row, contact_id):
        user = row.get("sender_username") or ""
        sender = Sender.me if user == self.self_username else Sender.other if user == contact_id else Sender.unknown
        raw_type = int(row.get("raw_type", 0))
        kind = {1: MsgType.text, 3: MsgType.image, 34: MsgType.voice, 43: MsgType.video,
                47: MsgType.sticker, 48: MsgType.unknown, 50: MsgType.call,
                10000: MsgType.system, 11000: MsgType.sticker}.get(raw_type & 0xffffffff, MsgType.unknown)
        content = row.get("content")
        content = content if isinstance(content, str) else ""
        text, quoted = (content if kind == MsgType.text else None), None
        meta = {"origin": "dsh_database", "sort_seq": int(row["sort_seq"]), "shard": row["shard"],
                "local_id": int(row["local_id"]), "raw_type": raw_type, "sender_verified": sender != Sender.unknown,
                "server_id": str(row.get("server_id") or "")}
        if raw_type & 0xffffffff == 49 and len(content) <= 1_000_000 and "<!DOCTYPE" not in content.upper() and "<!ENTITY" not in content.upper():
            try:
                root = ElementTree.fromstring(content)
                app = root if root.tag == "appmsg" else root.find(".//appmsg")
                subtype = app.findtext("type", "") if app is not None else ""
                kind = {"5": MsgType.link, "6": MsgType.file, "33": MsgType.link, "36": MsgType.link, "57": MsgType.text,
                        "2000": MsgType.transfer, "2001": MsgType.transfer}.get(subtype, MsgType.unknown)
                if subtype == "57":
                    text, quoted = app.findtext("title"), app.findtext("refermsg/content")
                meta["app_type"] = subtype
            except (ElementTree.ParseError, ValueError):
                pass
        if kind == MsgType.text and (not text or text.strip() == "[文本]"):
            kind, text = MsgType.unknown, None
            meta["body_unavailable"] = True
        if kind == MsgType.system:
            sender = Sender.system
        if kind != MsgType.text:
            meta["placeholder"] = {MsgType.transfer: "[转账/红包]", MsgType.image: "[图片]", MsgType.voice: "[语音]",
                                   MsgType.video: "[视频]", MsgType.sticker: "[表情]", MsgType.file: "[文件]",
                                   MsgType.link: "[链接]", MsgType.system: "[系统消息]", MsgType.call: "[通话]"}.get(kind, "[内容待核对]")
            if meta.get("app_type") in {"33", "36"}:
                meta["placeholder"] = "[小程序]"
            elif raw_type == 42:
                meta["placeholder"] = "[联系人名片]"
            elif meta.get("app_type") == "1":
                meta["placeholder"] = "[文本卡片]"
        timestamp, precision = None, TsPrecision.none
        try:
            seconds = int(row.get("create_time") or 0)
            if 0 < seconds < 32_503_680_000:
                timestamp, precision = datetime.fromtimestamp(seconds, ZoneInfo("Asia/Shanghai")), TsPrecision.second
        except (ValueError, TypeError, OverflowError, OSError):
            pass
        server_id = str(row.get("server_id") or "")
        identity = [self.account, contact_id, "server", server_id] if server_id not in {"", "0"} else [self.account, contact_id, row["shard"], row["local_id"]]
        return Message(id="dsh" + digest(identity)[:40], seq=0, sender=sender, type=kind,
                       text=text, quoted_text=quoted, ts=timestamp, ts_precision=precision, raw=content, meta=meta)


class DshService:
    def __init__(self, state, *, accounts=dsh_database.discover_accounts, opener=dsh_database.open_database, voice=None):
        self.state = state
        self.root = state.settings.data_dir / "wechat-dsh"
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.accounts = accounts
        self.opener = opener
        self.adapter = None
        self.connected_account = None
        self.voice = voice if voice is not None else DshVoiceTranscriber()
        self.directory = DshDirectory(state, accounts=accounts, opener=opener)

    @property
    def enabled(self):
        # Availability is on by default; chat history still requires an explicit
        # read. Preserve a saved disabled setting across restarts.
        return self.state._flag(DATABASE_ENABLED_SETTING, True)

    def _connect(self, account):
        if self.connected_account == account and self.adapter is not None:
            return self.adapter
        choices = [a for a in self.accounts() if a["id"] == account]
        if len(choices) != 1:
            raise RapportError("dsh_account_missing", "所选账号已变化，请刷新账号列表。", status=409)
        self.close()
        reader = self.opener(choices[0]["path"], self.root, self.state.crypto)
        try:
            self.adapter = DBAdapter(reader, account)
        except BaseException:
            reader.close()
            raise
        self.connected_account = account
        return self.adapter

    def _path(self, key):
        if not isinstance(key, str) or len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise RapportError("dsh_snapshot_missing", "读取结果不存在。", status=404)
        return self.root / (key + ".bin")

    def current_context(self, binding, contact, rid, process_id):
        """Read current DB context without modifying the imported material/cursor."""
        with self.lock:
            if not self.enabled:
                raise RapportError("dsh_disabled", "数据库读取已关闭，请重新开启后准备回复。", status=409)
            self.state.resolve_relationship_id(rid)
            data = self.load(binding["snapshot_id"], rid)
            if any(data.get(key) != binding.get(key) for key in ("account", "contact_id", "revision")) or data["contact"] != contact:
                raise RapportError("dsh_binding_changed", "数据库材料或好友绑定已变化，请重新准备回复。", status=409)
            adapter = self._connect(data["account"])
            if adapter.resolve_contact(contact) != data["contact_id"]:
                raise RapportError("dsh_contact_changed", "好友身份已变化，请重新读取。", status=409)
            adapter.raw.assert_process_account(process_id)
            rows, _ = adapter.messages(data["contact_id"], limit=30)
            messages = {message.id: message for message in (adapter.convert(row, data["contact_id"]) for row in rows)}
            ordered = sorted(messages.values(), key=lambda m: (m.meta["sort_seq"], m.meta["shard"], m.meta["local_id"]))
            for index, message in enumerate(ordered):
                message.seq = index + 1
            adapter.raw.assert_process_account(process_id)
            return Conversation(id="dsh-live-context", source=Source.wechat_json,
                                participants=Participants(me="我", other=contact), messages=ordered,
                                meta={"context_source": "dsh_database", "account": data["account"], "contact_id": data["contact_id"]})

    def load(self, key, rid):
        path = self._path(key)
        if not path.exists():
            raise RapportError("dsh_snapshot_missing", "读取结果不存在，请重新读取。", status=404)
        data = json.loads(self.state.crypto.decrypt(path.read_bytes()))
        if data["relationship_id"] != rid:
            raise RapportError("dsh_scope", "读取结果不属于当前关系对象。", status=403)
        return data

    def save(self, data):
        path = self._path(data["snapshot_id"])
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(self.state.crypto.encrypt(json.dumps(data, ensure_ascii=False).encode()))
        temporary.replace(path)  # message batch and cursor commit atomically

    def read(self, account, contact, rid, limit, *, snapshot_id=None, cancel=None, transcribe_voice=True, report=None, contact_id=None):
        with self.lock:
            self.state.resolve_relationship_id(rid)
            adapter = self._connect(account)
            user = adapter.resolve_contact(contact, contact_id)
            key = digest([account, user, rid])
            old = self.load(snapshot_id, rid) if snapshot_id else None
            if old and (old["snapshot_id"] != key or old["account"] != account or old["contact_id"] != user):
                raise RapportError("dsh_scope", "增量同步的账号或好友已变化。", status=409)
            if cancel and cancel.is_set():
                return None
            rows, cursor = adapter.messages(user, limit=limit, cursors=old["cursor"] if old else None)
            if cancel and cancel.is_set():
                return None
            previous = Conversation.model_validate(old["conversation"]).messages if old else []
            converted = [adapter.convert(row, user) for row in rows]
            # Reuse successful transcripts only for unchanged message identities
            # in the same encrypted account/contact/relationship snapshot.
            cached = old
            if cached is None and self._path(key).exists():
                cached = self.load(key, rid)
            cached_messages = {
                message.id: message for message in
                (Conversation.model_validate(cached["conversation"]).messages if cached else [])
            }
            for message in converted:
                saved = cached_messages.get(message.id)
                if (saved and message.type == saved.type == MsgType.voice
                        and saved.raw == message.raw and saved.ts == message.ts
                        and saved.sender == message.sender
                        and saved.meta.get("voice_transcription", {}).get("status") == "completed"):
                    message.text = saved.text
                    message.meta["voice_transcription"] = saved.meta["voice_transcription"]
            by_id = {m.id: m for m in previous}
            added = 0
            for message in converted:
                if message.id not in by_id:
                    added += 1
                by_id[message.id] = message
            messages = sorted(by_id.values(), key=lambda m: (m.meta["sort_seq"], m.meta["shard"], m.meta["local_id"]))
            dropped = max(0, len(messages) - 5000)
            messages = messages[-5000:]
            for index, message in enumerate(messages):
                message.seq = index + 1
            voice_changed = False
            if transcribe_voice:
                voice_changed = self.voice.enrich(account, user, messages, cancel=cancel, report=report)
            if cancel and cancel.is_set():
                return None
            if old and not rows and not voice_changed:
                return {**self.public(old), "added": 0}
            warnings = []
            unknown = [m.id for m in messages if m.sender == Sender.unknown]
            if unknown:
                warnings.append(ParseWarning(code="dsh_sender_unknown", msg="部分发送者无法核实，请校对角色后使用。", msg_ids=unknown))
            if dropped:
                warnings.append(ParseWarning(code="dsh_window_limit", msg="同步窗口保留最近 5000 条，较早材料请使用既有归档。"))
            voices = voice_summary(messages)
            if voices["completed"]:
                warnings.append(ParseWarning(code="dsh_voice_transcribed", msg=f"已在本机转写 {voices['completed']} 条语音；文字带有语音标记，使用前可校对。"))
            if voices["unavailable"]:
                warnings.append(ParseWarning(code="dsh_voice_unavailable", msg=f"{voices['unavailable']} 条语音未完成转写，已保留占位。可在电脑版播放后重新读取重试。"))
            conv = Conversation(id=uuid4().hex[:16], source=Source.wechat_json, parser_version="dsh-adapter-v1",
                                participants=Participants(me="我", other=contact), messages=messages, warnings=warnings,
                                meta={"origin": "dsh_database", "account": account, "contact_id": user, "snapshot_id": key})
            data = {"snapshot_id": key, "account": account, "contact_id": user, "contact": contact,
                    "relationship_id": rid, "cursor": cursor, "conversation": conv.model_dump(mode="json"), "import_id": None}
            data["revision"] = digest([data["conversation"], cursor])
            if cancel and cancel.is_set():
                return None
            self.state.resolve_relationship_id(rid)  # revocation during a slow read
            self.save(data)
            return {**self.public(data), "added": added}

    @staticmethod
    def public(data):
        conv = data["conversation"]
        return {k: data[k] for k in ("snapshot_id", "revision", "account", "contact", "contact_id", "relationship_id")} | {
            "messages": conv["messages"], "warnings": conv["warnings"], "import_id": data.get("import_id"),
            "source": "dsh_database", "sent": False,
            "voice_transcription": voice_summary(Conversation.model_validate(conv).messages)}

    def clear(self, rid=None):
        with self.lock:
            self.close()
            if rid is None:
                self.directory.clear()
            for path in self.root.glob("*.bin"):
                if path.name.endswith("-keys.bin"):
                    if rid is None:
                        path.unlink()
                    continue
                data = json.loads(self.state.crypto.decrypt(path.read_bytes()))
                if rid is None or data.get("relationship_id") == rid:
                    path.unlink()

    def close(self):
        with self.lock:
            try:
                if self.adapter is not None:
                    self.adapter.raw.close()
            finally:
                self.adapter = None
                self.connected_account = None
