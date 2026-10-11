"""Account-scoped message and voice behavior of the documented C ABI worker."""

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "weflow_worker", Path(__file__).resolve().parents[2] / "native/weflow_wcdb/worker.py"
)
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)

FRIEND = "wxid_friend"
OTHER = "wxid_other"
ME = "wxid_me"
STAMP = 1700000000
SERVER = (1 << 63) + 12345


@pytest.fixture
def account(tmp_path):
    base = tmp_path / "db_storage"
    base.mkdir()
    contact = base / "contact.db"
    with sqlite3.connect(contact) as db:
        db.execute(
            "CREATE TABLE contact(username TEXT, nick_name TEXT, remark TEXT, local_type INTEGER, flag INTEGER, delete_flag INTEGER, verify_flag INTEGER, chat_room_type INTEGER)"
        )
        db.executemany(
            "INSERT INTO contact VALUES(?,?,?,1,1,0,0,0)",
            [(name, name, name) for name in (ME, FRIEND, OTHER)],
        )
    with sqlite3.connect(base / "session.db") as db:
        db.execute(
            "CREATE TABLE SessionTable(username TEXT, sort_timestamp INTEGER, last_timestamp INTEGER, unread_count INTEGER, summary TEXT, is_hidden INTEGER)"
        )
        db.execute("INSERT INTO SessionTable VALUES(?,?,?,?,?,0)", (FRIEND, STAMP, STAMP, 7, "last message"))
    files = [("contact.db", str(contact), contact.stat().st_size)]
    for shard in range(2):
        path = base / f"message_{shard}.db"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE Name2Id(user_name TEXT)")
            db.execute("INSERT INTO Name2Id(rowid,user_name) VALUES(2,?)", (FRIEND if shard == 0 else ME,))
            for user in (FRIEND, OTHER):
                table = "Msg_" + hashlib.md5(user.encode()).hexdigest()
                db.execute(
                    f'CREATE TABLE "{table}"(local_id INTEGER,sort_seq INTEGER,real_sender_id INTEGER,server_id INTEGER,create_time INTEGER,local_type INTEGER,message_content TEXT,compress_content BLOB)'
                )
                db.execute(
                    f'INSERT INTO "{table}" VALUES(1,10,2,?,?,34,?,NULL)',
                    (SERVER - (1 << 64) if shard == 0 else 77, STAMP, "voice"),
                )
                db.execute(
                    f'INSERT INTO "{table}" VALUES(2,11,2,?, ?,1,?,NULL)', (900 + shard, STAMP + 1, "message")
                )
        files.append((path.name, str(path), path.stat().st_size))
    media = base / "media_0.db"
    with sqlite3.connect(media) as db:
        db.execute("CREATE TABLE Name2Id(user_name TEXT)")
        db.executemany("INSERT INTO Name2Id(rowid,user_name) VALUES(?,?)", [(1, ME), (2, FRIEND), (3, OTHER)])
        db.execute(
            "CREATE TABLE VoiceInfo(chat_name_id INTEGER,msg_svr_id INTEGER,create_time INTEGER,voice_data BLOB)"
        )
        db.executemany(
            "INSERT INTO VoiceInfo VALUES(?,?,?,?)",
            [
                (2, SERVER - (1 << 64), STAMP, b"\x02#!SILK_V3correct"),
                (3, SERVER - (1 << 64), STAMP, b"\x02#!SILK_V3other"),
            ],
        )

    def open_db(rel):
        connection = sqlite3.connect((base / rel).as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    obj = worker.Account.__new__(worker.Account)
    obj.path = tmp_path
    obj.me = ME
    obj.core = SimpleNamespace(_db_files=files, _open=open_db)
    obj.reader = lambda extra=None: obj.core
    obj.cursors = {}
    obj.next_cursor = 1
    return obj


def test_native_rows_preserve_shard_senders_and_unsigned_server_ids(account):
    rows = account.rows(FRIEND, 10, ascending=True)
    assert len(rows) == 4
    assert [row["sender_username"] for row in rows[:2]] == [FRIEND, ME]
    assert rows[0]["server_id"] == str(SERVER)
    assert [row["is_send"] for row in rows[:2]] == [0, 1]


def test_session_listing_uses_actual_unread_count_and_timestamps(account):
    sessions = account.sessions()
    assert len(sessions) == 1
    assert sessions[0]["username"] == FRIEND
    assert sessions[0]["unread_count"] == 7
    assert sessions[0]["last_timestamp"] == STAMP


def test_account_handles_do_not_alias_across_worker_generations(monkeypatch):
    monkeypatch.setattr(worker, "Account", lambda *args: object())
    first = worker.Service(None, None, generation=20)
    second = worker.Service(None, None, generation=21)
    old = int(first.dispatch("wcdb_open_account", ["path", ""]))
    new = int(second.dispatch("wcdb_open_account", ["path", ""]))
    assert old != new and 0 < new < 2**53
    with pytest.raises(worker.BridgeError, match="Invalid or closed"):
        second.dispatch("wcdb_get_db_status", [old])


def test_cursor_crosses_equal_sort_and_local_ids_without_repeating(account):
    service = worker.Service(None, None)
    service.accounts[1] = account
    cursor = int(service.dispatch("wcdb_open_message_cursor", [1, FRIEND, 1, 1, 0, 0]))
    got = []
    for _ in range(10):
        more, payload = service.dispatch("wcdb_fetch_message_batch", [1, cursor]).split("\n", 1)
        got.extend(json.loads(payload))
        if more == "0":
            break
    assert len(got) == 4
    assert len({(row["_shard"], row["local_id"]) for row in got}) == 4
    service.dispatch("wcdb_close_message_cursor", [1, cursor])
    with pytest.raises(worker.BridgeError):
        service.dispatch("wcdb_fetch_message_batch", [1, cursor])


def test_ambiguous_local_id_is_not_silently_bound_to_a_shard(account):
    with pytest.raises(worker.BridgeError, match="ambiguous"):
        account.one_message(FRIEND, local=1)
    assert account.one_message(FRIEND, server=SERVER)["server_id"] == str(SERVER)


def test_voice_is_bound_to_verified_message_and_chat_not_candidate_input(account):
    data = account.voice(FRIEND, STAMP, 1, SERVER, [OTHER])
    assert bytes.fromhex(data) == b"\x02#!SILK_V3correct"
    with pytest.raises(worker.BridgeError, match="not found"):
        account.voice(FRIEND, STAMP, 1, 1234567, [OTHER])


def test_voice_batch_matches_weflow_snake_case_contract(account):
    service = worker.Service(None, None)
    service.accounts[1] = account
    result = service.dispatch(
        "wcdb_get_voice_data_batch",
        [
            1,
            json.dumps(
                [
                    {
                        "session_id": FRIEND,
                        "create_time": STAMP,
                        "local_id": 1,
                        "svr_id": str(SERVER),
                        "candidates": [OTHER],
                    }
                ]
            ),
        ],
    )
    assert result[0]["success"] and bytes.fromhex(result[0]["hex"]) == b"\x02#!SILK_V3correct"


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM contact",
        "PRAGMA user_version=4",
        "ATTACH DATABASE ':memory:' AS other",
        "SELECT load_extension('anything')",
    ],
)
def test_raw_query_rejects_mutation_and_loading_extensions(account, sql):
    with pytest.raises(sqlite3.DatabaseError):
        account.query("contact", None, sql)
    assert account.contact(FRIEND)["username"] == FRIEND


def test_raw_query_cannot_read_unbound_voice_blobs_or_arbitrary_paths(account, tmp_path):
    with pytest.raises(sqlite3.DatabaseError):
        account.query("media", None, "SELECT hex(voice_data) FROM VoiceInfo")
    with pytest.raises(worker.BridgeError):
        account.query("contact", str(tmp_path / "unrelated.db"), "SELECT 1")
    assert account.query("media", None, "PRAGMA table_info(VoiceInfo)")


def test_same_second_voice_without_server_identity_is_ambiguous(account):
    dbfile = account.path / "db_storage" / "message_0.db"
    table = "Msg_" + hashlib.md5(FRIEND.encode()).hexdigest()
    with sqlite3.connect(dbfile) as db:
        db.execute(f'UPDATE "{table}" SET server_id=0 WHERE local_id=1')
    with sqlite3.connect(account.path / "db_storage" / "message_1.db") as db:
        db.execute(f'UPDATE "{table}" SET local_id=3 WHERE local_id=1')
    with sqlite3.connect(account.path / "db_storage" / "media_0.db") as db:
        db.execute("INSERT INTO VoiceInfo VALUES(2,0,?,?)", (STAMP, b"\x02#!SILK_V3ambiguous"))
    with pytest.raises(worker.BridgeError, match="timestamp"):
        account.voice(FRIEND, STAMP, 1, 0)
