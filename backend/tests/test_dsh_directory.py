import pytest

from rapport.desktop.dsh_directory import DshDirectory
from rapport.desktop.dsh_source import DBAdapter
from rapport.errors import RapportError
from tests.test_dsh_integration import ACCOUNT, Reader, install_service, row

OTHER = "b" * 32


class DirectoryReader(Reader):
    def __init__(self):
        super().__init__()
        self.friends = [
            {"username": "wxid_friend", "nick_name": "小明", "remark": "同名", "alias": "ming"},
            {"username": "wxid_second", "nick_name": "小红", "remark": "同名", "alias": "hong"},
        ]

    def list_friends(self):
        return self.friends

    def get_friend(self, user):
        return next((item for item in self.friends if item["username"] == user), None)

    def search_contact(self, name):
        return self.friends

    def account_nicknames(self, usernames):
        return {"wxid_other": "另一个账号"} if "wxid_other" in usernames else {}


def directory_fixture(client, tmp_path):
    choices = [
        {"id": ACCOUNT, "label": "wxid_me_abcd", "path": tmp_path / "wxid_me_abcd"},
        {"id": OTHER, "label": "wxid_other_abcd", "path": tmp_path / "wxid_other_abcd"},
    ]
    opened = []

    def opener(path, store, crypto, **options):
        assert options["contacts_only"]
        reader = DirectoryReader()
        opened.append((path, options, reader))
        return reader

    directory = DshDirectory(
        client.app_state, accounts=lambda: choices, opener=opener, active=lambda _: {ACCOUNT}
    )
    return directory, opened


def test_list_accounts_marks_active_without_scanning_or_opening_cold_databases(app_client, tmp_path):
    directory, opened = directory_fixture(app_client, tmp_path)
    accounts = directory.list_accounts()
    assert opened == []
    assert accounts[0]["id"] == ACCOUNT and accounts[0]["active"]
    assert "当前登录" in accounts[0]["display_label"] and "昵称未取得" in accounts[0]["display_label"]
    assert all("path" not in account for account in accounts)


def test_selected_contacts_resolve_names_and_encrypt_profiles_without_messages(app_client, tmp_path):
    directory, opened = directory_fixture(app_client, tmp_path)
    result = directory.contacts(ACCOUNT)
    assert result["account"] == ACCOUNT and result["total"] == 2
    assert result["profile"]["nickname"] == "我的昵称"
    assert result["accounts"][1]["nickname"] == "另一个账号"
    assert result["accounts"][1]["nickname_source"] == "contact"
    assert {friend["id"] for friend in result["contacts"]} == {"wxid_friend", "wxid_second"}
    assert result["contacts"][0]["alias"] == "ming"
    assert opened[0][1]["allow_key_extraction"] is True
    assert opened[0][2].closed and opened[0][2].calls == []
    payload = (directory.root / "profiles.bin").read_bytes()
    assert "我的昵称".encode() not in payload and b"wxid_friend" not in payload
    reloaded, _ = directory_fixture(app_client, tmp_path)
    assert reloaded.list_accounts()[0]["nickname"] == "我的昵称"
    directory.clear()
    assert not (directory.root / "profiles.bin").exists()


def test_cached_contact_keys_are_separate_and_listing_never_extracts(app_client, tmp_path):
    directory, opened = directory_fixture(app_client, tmp_path)
    source = app_client.app_state.settings.data_dir / "wechat-dsh" / (ACCOUNT + "-keys.bin")
    source.parent.mkdir(exist_ok=True)
    source.write_bytes(b"encrypted-key-fixture")
    profiles = directory.list_accounts()
    assert profiles[0]["nickname"] == "我的昵称"
    assert opened[0][1] == {"contacts_only": True, "allow_key_extraction": False}
    assert opened[0][2].closed
    assert source.read_bytes() == b"encrypted-key-fixture"
    assert (directory.root / ACCOUNT / source.name).read_bytes() == source.read_bytes()
    directory.list_accounts()
    assert len(opened) == 1


def test_unknown_or_offline_account_cannot_scan_another_login(app_client, tmp_path):
    directory, opened = directory_fixture(app_client, tmp_path)
    with pytest.raises(RapportError, match="账号"):
        directory.contacts("c" * 32)
    assert opened == []
    options = []

    def locked(*args, **kwargs):
        options.append(kwargs)
        raise RapportError("dsh_keys_unavailable", "Unavailable", status=409)

    directory.opener = locked
    with pytest.raises(RapportError) as result:
        directory.contacts(OTHER)
    assert result.value.code == "dsh_contacts_locked"
    assert options == [{"contacts_only": True, "allow_key_extraction": False}]


def test_explicit_friend_id_disambiguates_names_but_rejects_mismatches():
    reader = DirectoryReader()
    adapter = DBAdapter(reader, ACCOUNT)
    with pytest.raises(RapportError):
        adapter.resolve_contact("同名")
    assert adapter.resolve_contact("同名", "wxid_second") == "wxid_second"
    for user, name in [
        ("wxid_stranger", "同名"),
        ("wxid_me", "同名"),
        ("room@chatroom", "同名"),
        ("wxid_second", "错误的名字"),
    ]:
        with pytest.raises(RapportError):
            adapter.resolve_contact(name, user)


def test_read_scopes_selected_same_name_friend_and_snapshot_id(app_client):
    reader = DirectoryReader()
    reader.rows = [row(user="wxid_second")]
    service, _ = install_service(app_client, reader)
    result = service.read(ACCOUNT, "同名", "default", 100, contact_id="wxid_second")
    assert result["contact_id"] == "wxid_second"
    assert reader.calls[0][0] == "wxid_second"
    assert result["messages"][0]["sender"] == "other"
    with pytest.raises(RapportError) as error:
        service.read(
            ACCOUNT, "同名", "default", 100, contact_id="wxid_friend", snapshot_id=result["snapshot_id"]
        )
    assert error.value.code == "dsh_scope"


def test_contacts_api_requires_enabled_source_and_valid_selected_account(app_client, tmp_path):
    directory, opened = directory_fixture(app_client, tmp_path)
    app_client.app_state.dsh.directory = directory
    assert app_client.post("/api/wechat/dsh/contacts", json={"account": "../outside"}).status_code == 422
    response = app_client.post("/api/wechat/dsh/contacts", json={"account": ACCOUNT})
    assert response.status_code == 200 and response.json()["total"] == 2
    assert app_client.get("/api/wechat/dsh/accounts").json()["accounts"][0]["nickname"] == "我的昵称"
    app_client.patch("/api/wechat/dsh/config", json={"enabled": False})
    assert app_client.post("/api/wechat/dsh/contacts", json={"account": ACCOUNT}).status_code == 409
    assert len(opened) == 1
