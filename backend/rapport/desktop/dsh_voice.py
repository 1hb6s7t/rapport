"""Enrich a scoped DSH batch with local native extraction and speech recognition."""

from __future__ import annotations

import hashlib
import importlib.util
import threading
from contextlib import contextmanager
from pathlib import Path

from rapport.desktop.dsh_database import discover_accounts
from rapport.schemas.message_v1 import MsgType, TsPrecision

RUNTIME = Path(__file__).resolve().parents[3] / "native/weflow_wcdb"
# One DLL worker / recognizer at a time, including across service instances.
_LOCK = threading.RLock()
_MODULES = {}
_MODEL = None


def _module(name):
    if name not in _MODULES:
        spec = importlib.util.spec_from_file_location("rapport_local_voice_" + name, RUNTIME / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _MODULES[name] = module
    return _MODULES[name]


def _recognizer():
    global _MODEL
    if _MODEL is None:
        _MODEL = _module("transcribe").Transcriber()
    return _MODEL


@contextmanager
def _native_account(account):
    choices = [entry for entry in discover_accounts() if entry["id"] == account]
    if len(choices) != 1:
        raise ValueError("Selected account is unavailable")
    paths = list((choices[0]["path"] / "db_storage").rglob("session.db"))
    if len(paths) != 1:
        raise ValueError("Selected session database is unavailable")
    client = _module("native_client").Client()
    handle = None
    try:
        handle = client.open(paths[0])
        yield client, handle
    finally:
        try:
            if handle is not None:
                client.close(handle)
        finally:
            client.shutdown()


def _failure(message, code, reason):
    message.meta["voice_transcription"] = {
        "status": "unavailable",
        "code": code,
        "reason": reason,
    }


def summary(messages):
    voices = [message for message in messages if message.type == MsgType.voice]
    completed = sum(
        bool(message.text) and message.meta.get("voice_transcription", {}).get("status") == "completed"
        for message in voices
    )
    failed = sum(
        message.meta.get("voice_transcription", {}).get("status") == "unavailable" for message in voices
    )
    return {
        "total": len(voices),
        "completed": completed,
        "unavailable": failed,
        "pending": len(voices) - completed - failed,
    }


class DshVoiceTranscriber:
    def __init__(self, *, session_factory=_native_account, model_factory=_recognizer):
        self.session_factory = session_factory
        self.model_factory = model_factory

    def enrich(self, account, contact_id, messages, *, cancel=None, report=None):
        # Failed clips retry on an explicit fresh read, not every sync poll.
        pending = [
            message
            for message in messages
            if message.type == MsgType.voice
            and not message.text
            and not message.meta.get("voice_transcription")
        ]
        if not pending:
            return False

        def cancelled():
            return cancel is not None and cancel.is_set()

        def progress(done):
            if report:
                report(
                    phase="transcribing", collected=len(messages), voice_done=done, voice_total=len(pending)
                )

        with _LOCK:
            if cancelled():
                return False
            progress(0)
            try:
                with self.session_factory(account) as (client, handle):
                    for index, message in enumerate(pending):
                        if cancelled():
                            return False
                        try:
                            if not message.ts or message.ts_precision != TsPrecision.second:
                                raise ValueError("Missing precise timestamp")
                            timestamp = int(message.ts.timestamp())
                            local_id = int(message.meta["local_id"])
                            server = int(message.meta.get("server_id") or 0)
                            if server >= 1 << 63:
                                server -= 1 << 64
                            if not (
                                0 < timestamp < 1 << 31
                                and 0 <= local_id < 1 << 31
                                and -(1 << 63) <= server < 1 << 63
                            ):
                                raise ValueError("Invalid native message identity")
                            silk = bytes.fromhex(
                                client.text(
                                    "wcdb_get_voice_data",
                                    handle,
                                    contact_id,
                                    timestamp,
                                    local_id,
                                    server,
                                    "[]",
                                )
                            )
                        except Exception:
                            _failure(
                                message,
                                "audio_unavailable",
                                "未能唯一定位本机语音，请在电脑版微信播放后重新读取。",
                            )
                            progress(index + 1)
                            continue
                        if cancelled():
                            return False
                        try:
                            model = self.model_factory()
                        except Exception:
                            for remaining in pending[index:]:
                                _failure(
                                    remaining,
                                    "runtime_unavailable",
                                    "本地语音模型未就绪，请检查组件后重新读取。",
                                )
                            progress(len(pending))
                            break
                        try:
                            result = model.transcribe(silk)
                            text = result["text"].strip()
                            if not text:
                                raise ValueError("Empty transcript")
                            message.text = "[语音转文字] " + text
                            message.meta["voice_transcription"] = {
                                **result,
                                "status": "completed",
                                "audio_sha256": hashlib.sha256(silk).hexdigest(),
                            }
                        except Exception:
                            _failure(
                                message,
                                "recognition_failed",
                                "这条语音未能识别，保留语音占位；可重新读取重试。",
                            )
                        progress(index + 1)
            except Exception:
                # A missing component or account must not discard text messages.
                for message in pending:
                    if not message.meta.get("voice_transcription"):
                        _failure(
                            message,
                            "component_unavailable",
                            "语音读取组件未就绪，保留语音占位；可重新读取重试。",
                        )
                progress(len(pending))
        return not cancelled()
