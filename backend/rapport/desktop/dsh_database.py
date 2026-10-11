"""Account-pinned adapter for DSH's wechatauto database dependency.

The underlying Apache-2.0 engine is vendored; no GUI driver is imported.
Account labels use cached contact keys; selecting an account can unlock its
contact database. Chat shards open only for an explicit history read.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from rapport.errors import RapportError


def account_id(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).casefold().encode()).hexdigest()[:32]


def discover_accounts():
    if sys.platform != "win32":
        return []
    from rapport.vendor.wechatauto_db import db as engine

    base = engine.auto_detect_db_dir()
    if not base:
        return []
    return [{"id": account_id(Path(item["path"])), "label": item["account"], "path": Path(item["path"])}
            for item in engine.list_accounts(base)]


def active_account_ids(accounts):
    """Identify logged-in accounts from WeChat's open database files, not mtimes."""
    if sys.platform != "win32":
        return set()
    import psutil

    roots = [(item["id"], (item["path"] / "db_storage").resolve()) for item in accounts]
    active = set()
    for process in psutil.process_iter(["name"]):
        if (process.info.get("name") or "").casefold() not in {"weixin.exe", "wechat.exe"}:
            continue
        try:
            for opened in process.open_files():
                path = Path(opened.path).resolve()
                active.update(key for key, root in roots if path.is_relative_to(root))
        except (psutil.Error, OSError, ValueError):
            continue
    return active


def remove_workdir(path: Path, parent: Path):
    """Remove only a verified, unlinked connection directory inside our store."""
    parent = parent.resolve()
    if path.is_symlink() or path.is_junction() or path.resolve().parent != parent:
        raise ValueError("Unsafe database work directory")
    if path.exists():
        for current, directories, files in os.walk(path):
            for name in directories + files:
                candidate = Path(current) / name
                if candidate.is_symlink() or candidate.is_junction():
                    raise ValueError("Linked database work path")
        shutil.rmtree(path)


def open_database(account_path: Path, store: Path, crypto, *, extra_databases=(), provided_key: bytes | None = None,
                  provided_key_database: str | None = None, contacts_only=False, allow_key_extraction=True):
    if sys.platform != "win32":
        raise RapportError("dsh_windows_only", "数据库读取仅支持 Windows。", status=501)
    from rapport.vendor.wechatauto_db import db as engine

    extra_databases = frozenset(extra_databases)
    if any(name != "session.db" and not re.fullmatch(r"media_\d+\.db", name) for name in extra_databases):
        raise ValueError("Unsupported extra database")
    if provided_key is not None and len(provided_key) != 32:
        raise ValueError("A database key must contain 32 bytes")
    if provided_key_database and (provided_key is None or provided_key_database not in extra_databases):
        raise ValueError("The key's target database must be explicitly selected")
    account_path = account_path.resolve()
    if not (account_path / "db_storage").is_dir():
        raise RapportError("dsh_account_missing", "所选微信账号的数据目录不存在。", status=409)
    store.mkdir(parents=True, exist_ok=True)
    work_root = store / "work"
    work_root.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="connection-", dir=work_root))
    key_path = store / (account_id(account_path) + "-keys.bin")

    class ScopedDB(engine.WeChatDB):
        def _collect_db_files(self):
            files = super()._collect_db_files()
            accepted = []
            base = (account_path / "db_storage").resolve()
            for rel, filename, size in files:
                if not Path(filename).resolve().is_relative_to(base):
                    raise RuntimeError("Database path left account directory")
                if contacts_only and Path(filename).name != "contact.db":
                    continue
                if (Path(filename).name == "contact.db" or Path(filename).name in extra_databases
                        or re.fullmatch(r"message_\d+\.db", Path(filename).name)):
                    accepted.append((rel, filename, size))
            return accepted

        def _load_or_extract_keys(self, master_key=None):
            # Never load global/plaintext caches, guess another account, derive
            # image keys, or call the upstream account-switching recovery paths.
            if key_path.exists():
                data = json.loads(crypto.decrypt(key_path.read_bytes()))
                if data.get("account") != account_id(account_path):
                    raise RuntimeError("Key scope mismatch")
                self._keys = {k: bytes.fromhex(v) for k, v in data["keys"].items()
                              if any(rel == k for rel, _, _ in self._db_files)}
                self._keys = {k: v for k, v in self._keys.items() if self._key_works(k)}
            if provided_key is not None:
                verified = self._keys_from_candidates({(provided_key, None)})
                if not verified or (provided_key_database and not any(Path(rel).name == provided_key_database for rel in verified)):
                    raise RapportError("dsh_key_invalid", "提供的密钥未通过所选账号数据库校验。", status=409)
                self._keys.update(verified)
            if allow_key_extraction and any(rel not in self._keys for rel, _, _ in self._db_files):
                self._keys.update(self.extract_keys())
            self.unkeyed = [rel for rel, _, _ in self._db_files if not self._key_works(rel)]
            if self.unkeyed:
                raise RapportError("dsh_keys_unavailable", "未能读取所选账号的完整数据库。",
                                   "请保持该账号已登录；可以继续使用可见聊天读取。", 409)
            self._save_keys()

        def _save_keys(self):
            if self._keys:
                payload = {"account": account_id(account_path), "keys": {k: v.hex() for k, v in self._keys.items()}}
                temporary = key_path.with_suffix(".tmp")
                temporary.write_bytes(crypto.encrypt(json.dumps(payload).encode()))
                temporary.replace(key_path)

        def _auto_diagnose_key_failure(self, rel):
            raise RapportError("dsh_keys_unavailable", "数据库密钥不可用，请重新连接所选账号。", status=409)

        def _refresh_db_files(self):
            files = self._collect_db_files()
            if files != self._db_files:
                self._db_files = files
                self._load_or_extract_keys()

        def _open(self, rel):
            connection = super()._open(rel)
            try:
                stamp = work / (rel.replace(os.sep, "__") + ".stamp")
                src = Path(self._db_path(rel))
                parts = stamp.read_text().split(",")
                wal = self._wal_path(rel)
                # The base engine may fall back to an older main-file snapshot.
                # Never advance the sync cursor from a knowingly stale view.
                if (int(parts[5]) < 0 or float(parts[1]) != src.stat().st_mtime
                        or int(parts[2]) != src.stat().st_size
                        or float(parts[3]) != (Path(wal).stat().st_mtime if wal else 0)
                        or int(parts[4]) != (Path(wal).stat().st_size if wal else 0)):
                    raise RapportError("dsh_database_changing", "微信数据库正在更新，读取进度未推进，请稍后重试。", status=409)
            except BaseException:
                connection.close()
                raise
            return connection

        def get_self_info(self):
            for rel, filename, _ in self._db_files:
                if Path(filename).name == "contact.db":
                    connection = self._open(rel)
                    try:
                        row = connection.execute("SELECT username, nick_name FROM contact WHERE username=?", (self.wxid,)).fetchone()
                    finally:
                        connection.close()
                    if row:
                        return {"username": row[0], "nick_name": row[1]}
            raise RapportError("dsh_self_unknown", "无法核对数据库所属账号，未读取聊天内容。", status=409)

        def search_contact(self, keyword):
            # contact.db also includes non-friend group members. Restrict the
            # source search to active private friends before checking uniqueness.
            for rel, filename, _ in self._db_files:
                if Path(filename).name != "contact.db":
                    continue
                connection = self._open(rel)
                try:
                    columns = {r[1] for r in connection.execute("PRAGMA table_info(contact)")}
                    if not {"local_type", "flag", "delete_flag", "verify_flag", "chat_room_type"} <= columns:
                        raise RapportError("dsh_contact_schema", "当前微信版本的好友字段尚未适配，请使用画面读取。", status=409)
                    rows = connection.execute(
                        "SELECT username,nick_name,remark FROM contact "
                        "WHERE local_type=1 AND (flag & 1)=1 AND delete_flag=0 AND verify_flag=0 AND chat_room_type=0 "
                        "AND (nick_name LIKE ? OR remark LIKE ?) LIMIT 50", ("%" + keyword + "%",) * 2,
                    ).fetchall()
                    return [dict(row) for row in rows]
                finally:
                    connection.close()
            return []

        def list_friends(self):
            """All active private friends; never truncate to search_contact's 50 rows."""
            for rel, filename, _ in self._db_files:
                if Path(filename).name != "contact.db":
                    continue
                connection = self._open(rel)
                try:
                    columns = {row[1] for row in connection.execute("PRAGMA table_info(contact)")}
                    if not {"local_type", "flag", "delete_flag", "verify_flag", "chat_room_type"} <= columns:
                        raise RapportError("dsh_contact_schema", "当前微信版本的好友字段尚未适配。", status=409)
                    alias = "alias" if "alias" in columns else "'' AS alias"
                    rows = connection.execute(
                        f"SELECT username,nick_name,remark,{alias} FROM contact "
                        "WHERE local_type=1 AND (flag & 1)=1 AND delete_flag=0 "
                        "AND verify_flag=0 AND chat_room_type=0 AND username<>? "
                        "AND username NOT LIKE '%@chatroom' ORDER BY username", (self.wxid,),
                    ).fetchall()
                    return [dict(row) for row in rows if row["username"]]
                finally:
                    connection.close()
            return []

        def get_friend(self, username):
            return next((item for item in self.list_friends() if item["username"] == username), None)

        def account_nicknames(self, usernames):
            """Only exact requested local account IDs, without contact remarks."""
            if not usernames:
                return {}
            for rel, filename, _ in self._db_files:
                if Path(filename).name != "contact.db":
                    continue
                connection = self._open(rel)
                try:
                    marks = ",".join("?" for _ in usernames)
                    return dict(connection.execute(
                        f"SELECT username,nick_name FROM contact WHERE username IN ({marks})", list(usernames),
                    ))
                finally:
                    connection.close()
            return {}

        def fetch(self, user, limit, cursors=None):
            """DSH history/new-message reads with a per-shard composite cursor."""
            self._refresh_db_files()
            table = "Msg_" + hashlib.md5(user.encode()).hexdigest()
            rows, boundary = [], dict(cursors or {})
            for rel, filename, _ in sorted(self._db_files):
                if not re.fullmatch(r"message_\d+\.db", Path(filename).name):
                    continue
                connection = self._open(rel)
                try:
                    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                        continue
                    shard = rel.replace("\\", "/")
                    where, params = "", []
                    if cursors is not None:
                        seq, local = cursors.get(shard, [0, 0])
                        where, params = "WHERE sort_seq > ? OR (sort_seq = ? AND local_id > ?)", [seq, seq, local]
                    order = "ASC" if cursors is not None else "DESC"
                    found = connection.execute(
                        f'SELECT * FROM "{table}" {where} ORDER BY sort_seq {order}, local_id {order} LIMIT ?',
                        [*params, limit],
                    ).fetchall()
                    sender_ids = sorted({int(row["real_sender_id"] or 0) for row in found})
                    index = {}
                    if sender_ids and connection.execute("SELECT 1 FROM sqlite_master WHERE name='Name2Id' AND type='table'").fetchone():
                        # real_sender_id refers to this message shard's Name2Id,
                        # not message_resource.SenderName2Id. IDs differ by shard.
                        marks = ",".join("?" for _ in sender_ids)
                        index = dict(connection.execute(f"SELECT rowid,user_name FROM Name2Id WHERE rowid IN ({marks})", sender_ids))
                    if cursors is None and found:
                        boundary[shard] = [int(found[0]["sort_seq"]), int(found[0]["local_id"])]
                    for row in found:
                        kind = engine.WeChatDB._msg_type_name(row["local_type"])
                        content = row["message_content"]
                        if isinstance(content, bytes):
                            content = engine.WeChatDB._friendly_content(content, kind)
                        if not content or content == f"[{kind}]":
                            compressed = row["compress_content"]
                            if isinstance(compressed, bytes) and compressed:
                                content = engine.WeChatDB._friendly_content(compressed, kind)
                        data = {"local_id": row["local_id"], "sort_seq": row["sort_seq"],
                                "create_time": row["create_time"], "content": content, "type": kind,
                                "shard": shard, "server_id": str(row["server_id"] or ""),
                                "raw_type": int(row["local_type"]), "sender_id": row["real_sender_id"],
                                "sender_username": index.get(int(row["real_sender_id"] or 0), "")}
                        rows.append(data)
                finally:
                    connection.close()
            rows.sort(key=lambda r: (int(r["sort_seq"]), r["shard"], int(r["local_id"])), reverse=cursors is None)
            rows = rows[:limit]
            if cursors is not None:
                for row in rows:
                    boundary[row["shard"]] = [int(row["sort_seq"]), int(row["local_id"])]
            return rows, boundary

        def close(self):
            self._keys.clear()
            remove_workdir(work, work_root)

        def assert_process_account(self, process_id):
            """Tie the visible WeChat window to this account using open DB files."""
            import psutil

            try:
                process = psutil.Process(process_id)
                if process.name().casefold() not in {"weixin.exe", "wechat.exe"}:
                    raise ValueError("Wrong process")
                base = (account_path / "db_storage").resolve()
                matched = any(Path(item.path).resolve().is_relative_to(base) for item in process.open_files())
            except (psutil.Error, OSError, ValueError):
                matched = False
            if not matched:
                raise RapportError("dsh_window_account_mismatch", "当前微信窗口与所选数据库账号未能核对一致。",
                                   "请切回该账号后重新准备回复。", 409)

    try:
        reader = ScopedDB(db_dir=str(account_path.parent), account=account_path.name,
                          workdir=str(work), keys_file=str(key_path))
        reader.get_self_info()  # account identity is checked before contact reads
        return reader
    except BaseException:
        remove_workdir(work, work_root)
        raise
