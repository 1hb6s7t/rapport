"""Account display names and a selected account's private friend directory."""

from __future__ import annotations

import json
import re
import threading
import time
from contextlib import contextmanager

from rapport.desktop import dsh_database
from rapport.errors import RapportError


class DshDirectory:
    def __init__(self, state, *, accounts, opener, active=dsh_database.active_account_ids):
        self.state = state
        self.accounts = accounts
        self.opener = opener
        self.active = active
        self.root = state.settings.data_dir / "wechat-directory"
        self.lock = threading.RLock()
        self.checked_at = {}
        self.profiles = None

    def _load(self):
        if self.profiles is None:
            path = self.root / "profiles.bin"
            try:
                self.profiles = (
                    json.loads(self.state.crypto.decrypt(path.read_bytes())) if path.exists() else {}
                )
            except Exception:
                self.profiles = {}
        return self.profiles

    def _save(self):
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / "profiles.bin"
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(
            self.state.crypto.encrypt(json.dumps(self._load(), ensure_ascii=False).encode())
        )
        temporary.replace(path)

    def _store(self, item):
        return self.root / item["id"]

    def _cached_key(self, item):
        name = item["id"] + "-keys.bin"
        return self._store(item) / name, self.state.settings.data_dir / "wechat-dsh" / name

    @contextmanager
    def _reader(self, item, *, extract=False):
        store = self._store(item)
        own, seed = self._cached_key(item)
        store.mkdir(parents=True, exist_ok=True)
        if seed.exists() and (not own.exists() or seed.stat().st_mtime > own.stat().st_mtime):
            # Separate contact-only key storage cannot overwrite message keys.
            own.write_bytes(seed.read_bytes())
        reader = self.opener(
            item["path"], store, self.state.crypto, contacts_only=True, allow_key_extraction=extract
        )
        try:
            yield reader
        finally:
            reader.close()

    @staticmethod
    def _username(item):
        return re.sub(r"_\w{4}$", "", item["path"].name)

    def _remember(self, item, reader, choices):
        info = reader.get_self_info()
        if info.get("username") != self._username(item):
            raise RapportError("dsh_self_unknown", "账号昵称与所选数据库身份不一致。", status=409)
        profiles = self._load()
        nickname = (info.get("nick_name") or "").strip()
        if nickname:
            profiles[item["id"]] = {"nickname": nickname, "source": "self"}
        # Other previously logged-in accounts may appear by their exact wxid in
        # this readable contact DB. Use their nickname, never somebody's remark.
        names = reader.account_nicknames([self._username(entry) for entry in choices])
        for entry in choices:
            name = (names.get(self._username(entry)) or "").strip()
            if name and profiles.get(entry["id"], {}).get("source") != "self":
                profiles[entry["id"]] = {"nickname": name, "source": "contact"}
        self._save()

    def _public(self, item, active):
        profile = self._load().get(item["id"], {})
        nickname = profile.get("nickname") or ""
        return {
            "id": item["id"],
            "label": item["label"],
            "wxid": self._username(item),
            "nickname": nickname,
            "nickname_source": profile.get("source"),
            "active": item["id"] in active,
            "display_label": f"{item['label']} · {nickname or '昵称未取得'}"
            + (" · 当前登录" if item["id"] in active else ""),
        }

    def list_accounts(self, *, refresh=False):
        with self.lock:
            choices = self.accounts()
            active = self.active(choices)
            # A listing never scans memory or opens chat shards. Use already
            # validated encrypted keys where available; selection unlocks contacts.
            for item in sorted(choices, key=lambda entry: entry["id"] not in active):
                if not any(path.exists() for path in self._cached_key(item)):
                    continue
                if not refresh and time.monotonic() - self.checked_at.get(item["id"], 0) < 60:
                    continue
                self.checked_at[item["id"]] = time.monotonic()
                try:
                    with self._reader(item) as reader:
                        self._remember(item, reader, choices)
                except Exception:
                    continue  # Keep the account selectable, with its known name.
            return sorted(
                (self._public(item, active) for item in choices),
                key=lambda item: (not item["active"], item["label"]),
            )

    def contacts(self, account):
        with self.lock:
            choices = self.accounts()
            found = [item for item in choices if item["id"] == account]
            if len(found) != 1:
                raise RapportError("dsh_account_missing", "所选账号已变化，请刷新账号列表。", status=409)
            active = self.active(choices)
            item = found[0]
            try:
                with self._reader(item, extract=account in active) as reader:
                    self._remember(item, reader, choices)
                    friends = reader.list_friends()
            except RapportError as exc:
                if exc.code == "dsh_keys_unavailable":
                    raise RapportError(
                        "dsh_contacts_locked",
                        "暂时无法读取此账号的好友列表。",
                        "请在电脑版微信登录这个账号，再点击刷新好友。",
                        409,
                    ) from exc
                raise
            if not self.state._flag("desktop.wechat.database", True):
                raise RapportError("dsh_disabled", "数据库读取已关闭。", status=409)
            contacts = [
                {
                    "id": friend["username"],
                    "nickname": friend.get("nick_name") or "",
                    "remark": friend.get("remark") or "",
                    "alias": friend.get("alias") or "",
                    "display_name": friend.get("remark") or friend.get("nick_name") or friend["username"],
                }
                for friend in friends
            ]
            contacts.sort(key=lambda friend: (friend["display_name"].casefold(), friend["id"]))
            return {
                "account": account,
                "profile": self._public(item, active),
                "accounts": [self._public(entry, active) for entry in choices],
                "contacts": contacts,
                "total": len(contacts),
            }

    def clear(self):
        with self.lock:
            self.profiles = {}
            self.checked_at.clear()
            for path in [self.root / "profiles.bin", *self.root.glob("*/*-keys.bin")]:
                if path.exists():
                    path.unlink()
