import io
import json
import threading
import time
import zipfile
from contextlib import contextmanager

import pytest

from rapport.desktop.dsh_source import DBAdapter
from rapport.desktop.dsh_voice import DshVoiceTranscriber, summary
from tests.test_dsh_integration import ACCOUNT, Reader, install_service, row


class VoiceRuntime:
    def __init__(self):
        self.opens = []
        self.closed = 0
        self.lookups = []
        self.recognized = []
        self.missing = set()
        self.cancel = None
        self.model_error = False

    @contextmanager
    def session(self, account):
        self.opens.append(account)
        try:
            yield self, 42
        finally:
            self.closed += 1

    def text(self, operation, handle, contact, timestamp, local, server, candidates):
        self.lookups.append((operation, handle, contact, timestamp, local, server, candidates))
        if local in self.missing:
            raise RuntimeError("private database path / key must not be exposed")
        return b"#!SILK_V3:test".hex()

    def model(self):
        if self.model_error:
            raise ImportError("ASR unavailable")
        return self

    def transcribe(self, silk):
        self.recognized.append(silk)
        if self.cancel:
            self.cancel.set()
        return {"text": "周末一起出去玩吧", "model": "test-local", "provider": "cpu"}

    def service(self):
        return DshVoiceTranscriber(session_factory=self.session, model_factory=self.model)


def voices():
    adapter = DBAdapter(Reader(), ACCOUNT)
    return [
        adapter.convert(row(seq=i, local=i, kind=34, server=str(2**64 - i)), "wxid_friend") for i in (1, 2)
    ]


def test_native_identity_unsigned_ids_and_partial_failure():
    runtime = VoiceRuntime()
    runtime.missing.add(1)
    messages = voices()
    progress = []
    assert runtime.service().enrich(
        ACCOUNT, "wxid_friend", messages, report=lambda **value: progress.append(value)
    )
    assert runtime.opens == [ACCOUNT] and runtime.closed == 1
    assert runtime.lookups[0][2:6] == ("wxid_friend", 1700000001, 1, -1)
    assert messages[0].text is None
    assert messages[0].meta["voice_transcription"]["code"] == "audio_unavailable"
    assert "private database" not in json.dumps(messages[0].meta)
    assert messages[1].type == "voice" and messages[1].text == "[语音转文字] 周末一起出去玩吧"
    assert summary(messages) == {"total": 2, "completed": 1, "unavailable": 1, "pending": 0}
    assert progress[-1]["voice_done"] == progress[-1]["voice_total"] == 2
    assert not runtime.service().enrich(ACCOUNT, "wxid_friend", messages)
    assert len(runtime.lookups) == 2  # Sync does not keep retrying failed clips.


def test_missing_component_and_model_preserve_batch():
    @contextmanager
    def missing(_):
        raise OSError("missing DLL")
        yield

    messages = voices()
    DshVoiceTranscriber(session_factory=missing).enrich(ACCOUNT, "wxid_friend", messages)
    assert all(m.meta["voice_transcription"]["code"] == "component_unavailable" for m in messages)
    runtime = VoiceRuntime()
    runtime.model_error = True
    messages = voices()
    runtime.service().enrich(ACCOUNT, "wxid_friend", messages)
    assert runtime.closed == 1 and len(runtime.lookups) == 1
    assert all(m.meta["voice_transcription"]["code"] == "runtime_unavailable" for m in messages)


def test_invalid_timestamp_does_not_call_native():
    runtime = VoiceRuntime()
    messages = voices()[:1]
    messages[0].ts = None
    runtime.service().enrich(ACCOUNT, "wxid_friend", messages)
    assert runtime.lookups == [] and runtime.recognized == []
    assert summary(messages)["unavailable"] == 1


def test_opt_out_then_sync_transcribes_and_reuses_encrypted_results(app_client):
    service, reader = install_service(app_client)
    runtime = VoiceRuntime()
    service.voice = runtime.service()
    reader.rows = [row(kind=34, server="789")]
    first = service.read(ACCOUNT, "测试好友", "default", 100, transcribe_voice=False)
    assert runtime.opens == [] and first["voice_transcription"]["pending"] == 1
    second = service.read(ACCOUNT, "测试好友", "default", 100, snapshot_id=first["snapshot_id"])
    assert second["added"] == 0 and second["revision"] != first["revision"]
    assert second["voice_transcription"]["completed"] == 1
    encrypted = service._path(first["snapshot_id"]).read_bytes()
    assert "周末一起出去玩吧".encode() not in encrypted
    again = service.read(ACCOUNT, "测试好友", "default", 100, snapshot_id=first["snapshot_id"])
    assert again["revision"] == second["revision"]
    fresh = service.read(ACCOUNT, "测试好友", "default", 100)
    assert fresh["voice_transcription"]["completed"] == 1 and len(runtime.recognized) == 1
    reader.rows[0]["content"] = "changed-source-voice"
    service.read(ACCOUNT, "测试好友", "default", 100)
    assert len(runtime.recognized) == 2


def test_cancel_during_recognition_does_not_commit_or_advance(app_client):
    service, reader = install_service(app_client)
    first = service.read(ACCOUNT, "测试好友", "default", 100)
    reader.rows += [row(seq=2, local=2, kind=34), row(seq=3, local=3, kind=34)]
    runtime = VoiceRuntime()
    runtime.cancel = threading.Event()
    service.voice = runtime.service()
    assert (
        service.read(
            ACCOUNT, "测试好友", "default", 100, snapshot_id=first["snapshot_id"], cancel=runtime.cancel
        )
        is None
    )
    assert service.load(first["snapshot_id"], "default")["revision"] == first["revision"]
    assert runtime.closed == 1 and len(runtime.recognized) == 1
    runtime.cancel = None
    result = service.read(ACCOUNT, "测试好友", "default", 100, snapshot_id=first["snapshot_id"])
    assert result["added"] == 2 and result["voice_transcription"]["completed"] == 2


def test_failed_audio_retries_on_fresh_read(app_client):
    service, reader = install_service(app_client)
    runtime = VoiceRuntime()
    runtime.missing.add(1)
    service.voice = runtime.service()
    reader.rows = [row(kind=34)]
    first = service.read(ACCOUNT, "测试好友", "default", 100)
    runtime.missing.clear()
    synced = service.read(ACCOUNT, "测试好友", "default", 100, snapshot_id=first["snapshot_id"])
    assert synced["revision"] == first["revision"] and len(runtime.lookups) == 1
    fresh = service.read(ACCOUNT, "测试好友", "default", 100)
    assert fresh["voice_transcription"]["completed"] == 1 and len(runtime.lookups) == 2


@pytest.mark.parametrize("enabled", [True, False])
def test_production_api_import_archive_export_preserves_voice(app_client, enabled):
    service, reader = install_service(app_client)
    runtime = VoiceRuntime()
    service.voice = runtime.service()
    reader.rows = [row(), row(seq=2, local=2, kind=34)]
    body = {"account": ACCOUNT, "contact": "测试好友", "limit": 100}
    if not enabled:
        body["transcribe_voice"] = False
    response = app_client.post("/api/wechat/dsh/read", json=body)
    assert response.status_code == 200
    for _ in range(100):
        job = app_client.get("/api/wechat/dsh/jobs/" + response.json()["id"]).json()
        if job["status"] != "running":
            break
        time.sleep(0.01)
    assert job["status"] == "completed"
    batch = job["result"]
    scope = {key: batch[key] for key in ("snapshot_id", "revision", "relationship_id")}
    imported = app_client.post("/api/wechat/dsh/import", json=scope).json()
    assert imported["messages"][1]["type"] == "voice"
    assert app_client.post(f"/api/imports/{imported['id']}/confirm", json={}).status_code == 200
    archive = app_client.post(
        "/api/archives", json={"import_id": imported["id"], "relationship_id": "default"}
    ).json()
    prefix = f"/api/archives/{archive['id']}"
    preview = app_client.post(
        prefix + "/export-preview", json={"relationship_id": "default", "include_media": False}
    ).json()
    exported = app_client.post(
        prefix + "/export",
        json={
            "relationship_id": "default",
            "include_media": False,
            "token": preview["token"],
            "confirmed": True,
        },
    )
    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.content)) as package:
        records = [json.loads(line) for line in package.read("messages.jsonl").splitlines()]
        assert records[1]["type"] == "voice"
        if enabled:
            assert records[1]["text"] == "[语音转文字] 周末一起出去玩吧"
            assert records[1]["text"] in package.read("viewer.html").decode()
        else:
            assert records[1]["text"] is None and runtime.opens == []
