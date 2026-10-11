"""Real SQLite queries through the pinned adapter, with decryption stubbed.

No WeChat process or user database is accessed by these tests.
"""
import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest

from rapport.desktop import dsh_database
from rapport.security.crypto import Crypto


@pytest.fixture
def scoped_reader(tmp_path, monkeypatch):
    if sys.platform != "win32":
        pytest.skip("Pinned WCDB engine has Windows API declarations")
    from rapport.vendor.wechatauto_db import db as engine

    account = tmp_path / "wxid_me_abcd"
    storage = account / "db_storage"
    storage.mkdir(parents=True)
    tables = {
        "contact.db": "CREATE TABLE contact (username TEXT, nick_name TEXT, remark TEXT, alias TEXT, local_type INT, flag INT, delete_flag INT, verify_flag INT, chat_room_type INT); INSERT INTO contact VALUES ('wxid_me','自己','','',1,1,0,0,0); INSERT INTO contact VALUES ('wxid_friend','好友','测试好友','',1,1,0,0,0); INSERT INTO contact VALUES ('wxid_stranger','测试好友','','',3,4,0,0,0);",
        "message_resource.db": "CREATE TABLE SenderName2Id(user_name TEXT); INSERT INTO SenderName2Id(rowid,user_name) VALUES(2,'wxid_friend'); INSERT INTO SenderName2Id(rowid,user_name) VALUES(7,'wxid_me');",
    }
    name = "Msg_" + hashlib.md5(b"wxid_friend").hexdigest()
    for shard in (0, 1):
        tables[f"message_{shard}.db"] = f"CREATE TABLE {name} (local_id INTEGER,local_type INTEGER,real_sender_id INTEGER,create_time INTEGER,message_content TEXT,source TEXT,packed_info_data BLOB,compress_content BLOB,server_id INTEGER,sort_seq INTEGER); CREATE TABLE Name2Id (user_name TEXT); INSERT INTO Name2Id(rowid,user_name) VALUES (2,'wxid_friend'),(7,'wxid_me');"
    for filename, script in tables.items():
        with sqlite3.connect(storage / filename) as conn:
            conn.executescript(script)
            if filename.startswith("message_") and filename != "message_resource.db":
                for local in (1, 2):
                    conn.execute(f"INSERT INTO {name} VALUES (?,1,?,1700000000,'正文','',NULL,NULL,0,10)", (local, 2 if local == 1 else 7))

    def initialize(self, db_dir, account, workdir, keys_file):
        self.db_dir, self.account, self.workdir, self.keys_file = db_dir, account, workdir, keys_file
        self.account_dir = str(Path(db_dir) / account)
        self._db_files = self._collect_db_files()
        self._keys = {}

    def open_plain(self, rel):
        source = Path(self._db_path(rel))
        stamp = Path(self.workdir) / (rel.replace("\\", "__") + ".stamp")
        stamp.write_text(f"3,{source.stat().st_mtime!r},{source.stat().st_size},0,0,0")
        connection = sqlite3.connect(source)
        connection.row_factory = sqlite3.Row
        return connection

    monkeypatch.setattr(engine.WeChatDB, "__init__", initialize)
    monkeypatch.setattr(engine.WeChatDB, "_open", open_plain)
    reader = dsh_database.open_database(account, tmp_path / "private", Crypto(tmp_path / ".key"))
    yield reader, storage, name
    reader.close()


def test_per_shard_composite_cursor_does_not_skip_equal_sort_sequences(scoped_reader):
    reader, storage, table = scoped_reader
    rows, cursors = reader.fetch("wxid_friend", 1)
    assert len(rows) == 1
    assert set(cursors) == {"message_0.db", "message_1.db"}
    assert all(position == [10, 2] for position in cursors.values())
    for shard in (0, 1):
        with sqlite3.connect(storage / f"message_{shard}.db") as conn:
            conn.execute(f"INSERT INTO {table} VALUES (3,1,7,1700000001,'新增','',NULL,NULL,0,10)")
    first, after = reader.fetch("wxid_friend", 1, cursors)
    second, final = reader.fetch("wxid_friend", 1, after)
    assert first[0]["shard"] != second[0]["shard"]
    assert first[0]["sender_username"] == "wxid_me"
    assert reader.fetch("wxid_friend", 1, final)[0] == []


def test_database_sender_mapping_includes_two(scoped_reader):
    reader, _, _ = scoped_reader
    rows, _ = reader.fetch("wxid_friend", 10)
    assert len(rows) == 4
    assert {r["sender_username"] for r in rows if r["sender_id"] == 2} == {"wxid_friend"}
    assert len({(r["shard"], r["local_id"]) for r in rows}) == 4


def test_contact_search_excludes_same_named_nonfriend_group_members(scoped_reader):
    reader, _, _ = scoped_reader
    assert [r["username"] for r in reader.search_contact("测试好友")] == ["wxid_friend"]


def test_sender_number_is_resolved_in_its_own_shard(scoped_reader):
    reader, storage, _ = scoped_reader
    with sqlite3.connect(storage / "message_1.db") as conn:
        conn.execute("UPDATE Name2Id SET user_name='wxid_me' WHERE rowid=2")
    rows, _ = reader.fetch("wxid_friend", 10)
    assert {r["shard"]: r["sender_username"] for r in rows if r["sender_id"] == 2} == {
        "message_0.db": "wxid_friend", "message_1.db": "wxid_me"}


def test_cleanup_refuses_paths_outside_private_work_root(tmp_path):
    outside = tmp_path / "original"
    outside.mkdir()
    (outside / "preserve.txt").write_text("original")
    with pytest.raises(ValueError):
        dsh_database.remove_workdir(outside, tmp_path / "private")
    assert (outside / "preserve.txt").read_text() == "original"


def test_explicit_key_must_unlock_its_selected_database(scoped_reader, tmp_path):
    from rapport.errors import RapportError

    _, storage, _ = scoped_reader
    with sqlite3.connect(storage / "session.db") as connection:
        connection.execute("CREATE TABLE SessionTable(username TEXT)")
    key = b"k" * 32
    reader = dsh_database.open_database(storage.parent, tmp_path / "explicit-key", Crypto(tmp_path / "explicit.key"),
                                        extra_databases=("session.db",), provided_key=key, provided_key_database="session.db")
    try:
        reader._key_works = lambda _: True
        reader._keys_from_candidates = lambda _: {"contact.db": key}
        with pytest.raises(RapportError, match="密钥"):
            reader._load_or_extract_keys()
        reader._keys = {rel: key for rel, _, _ in reader._db_files if Path(rel).name != "session.db"}
        reader._keys_from_candidates = lambda _: {"session.db": key}
        reader._load_or_extract_keys()
        assert reader._keys["session.db"] == key
    finally:
        reader.close()


def test_friend_directory_has_all_friends_and_excludes_self_and_nonfriends(scoped_reader):
    reader, storage, _ = scoped_reader
    with sqlite3.connect(storage / "contact.db") as connection:
        connection.executemany("INSERT INTO contact VALUES (?, '重名', '同名备注', ?, 1, 1, 0, 0, 0)",
                               [(f"wxid_{index}", f"alias{index}") for index in range(80)])
        for username, flags in [("group@chatroom", (1, 1, 0, 0, 1)), ("deleted", (1, 1, 1, 0, 0)),
                                ("official", (1, 1, 0, 8, 0)), ("not_friend", (1, 0, 0, 0, 0))]:
            connection.execute("INSERT INTO contact VALUES (?, '重名', '', '', ?, ?, ?, ?, ?)", (username, *flags))
    friends = reader.list_friends()
    assert len(friends) == 81
    assert "wxid_me" not in {friend["username"] for friend in friends}
    assert reader.get_friend("wxid_79")["alias"] == "alias79"
    assert reader.get_friend("wxid_stranger") is None
    assert reader.account_nicknames(["wxid_me", "wxid_stranger", "unknown"]) == {
        "wxid_me": "自己", "wxid_stranger": "测试好友"}


def test_contact_only_reader_does_not_require_message_shards_or_extract_keys(scoped_reader, tmp_path, monkeypatch):
    from rapport.vendor.wechatauto_db import db as engine

    _, storage, _ = scoped_reader
    monkeypatch.setattr(engine.WeChatDB, "extract_keys", lambda _: pytest.fail("directory listing must not scan memory"))
    reader = dsh_database.open_database(storage.parent, tmp_path / "directory", Crypto(tmp_path / "directory.key"),
                                        contacts_only=True, allow_key_extraction=False)
    try:
        assert {Path(filename).name for _, filename, _ in reader._db_files} == {"contact.db"}
        reader._key_works = lambda _: False
        with pytest.raises(Exception, match="数据库"):
            reader._load_or_extract_keys()
    finally:
        reader.close()
