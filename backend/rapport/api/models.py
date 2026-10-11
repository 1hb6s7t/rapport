"""Local-model endpoints (v0.8): doctor / pull / local-on for the Ollama profiles.

Nothing here touches the user's material. `pull` streams Ollama's own registry download and
keeps the progress in memory so the settings page can poll it; the weights land in Ollama's
model directory, not in Rapport's data directory.
"""

from __future__ import annotations

import threading
import time

import httpx
from fastapi import APIRouter, Request
from pydantic import BaseModel

from rapport.api.deps import err, get_state
from rapport.config import PROVIDER_PRESETS, ConnectionMode
from rapport.llm.ollama import _root
from rapport.models_cli import PROFILES, doctor_report
from rapport.security import SessionDep

router = APIRouter(prefix="/models", dependencies=[SessionDep])

_lock = threading.Lock()
_pull: dict = {"running": False, "profile": None, "items": [], "error": None, "started_at": None, "finished_at": None}


class ProfileIn(BaseModel):
    profile: str = "solo"


def _preset_for(profile: str):
    if profile not in PROFILES:
        raise err(400, "bad_profile", "profile 只能是 solo / split / lite")
    return PROVIDER_PRESETS[PROFILES[profile]]


@router.get("/doctor")
def doctor(request: Request):
    get_state(request)
    return doctor_report()


@router.get("/pull")
def pull_status(request: Request):
    get_state(request)
    with _lock:
        return dict(_pull, items=[dict(i) for i in _pull["items"]])


def _pull_worker(models: list[str], base_url: str | None) -> None:
    root = _root(base_url)
    try:
        with httpx.Client(base_url=root, timeout=None, trust_env=False) as c:
            for idx, name in enumerate(models):
                with c.stream("POST", "/api/pull", json={"model": name, "stream": True}) as r:
                    if r.status_code >= 400:
                        raise RuntimeError(f"Ollama 返回 {r.status_code}")
                    for line in r.iter_lines():
                        if not line:
                            continue
                        try:
                            import json

                            ev = json.loads(line)
                        except ValueError:
                            continue
                        if ev.get("error"):
                            raise RuntimeError(str(ev["error"])[:200])
                        with _lock:
                            item = _pull["items"][idx]
                            item["status"] = str(ev.get("status") or "")
                            if ev.get("total"):
                                item["total"] = int(ev["total"])
                                item["completed"] = int(ev.get("completed") or 0)
                            if item["status"] == "success":
                                item["done"] = True
                with _lock:
                    _pull["items"][idx]["done"] = True
    except Exception as e:  # noqa: BLE001
        with _lock:
            _pull["error"] = f"{type(e).__name__}: {e}"[:300]
    finally:
        with _lock:
            _pull["running"] = False
            _pull["finished_at"] = time.time()


@router.post("/pull")
def pull(body: ProfileIn, request: Request):
    st = get_state(request)
    preset = _preset_for(body.profile)
    rep = doctor_report()
    if not rep["ollama_running"]:
        raise err(400, "ollama_down", "Ollama 未运行。先启动 Ollama 应用，或在终端运行 `ollama serve`。")
    need = sorted(set((preset.task_models or {}).values()))
    with _lock:
        if _pull["running"]:
            raise err(409, "pull_running", "已有拉取任务在进行")
        _pull.update(
            running=True,
            profile=body.profile,
            items=[{"model": m, "status": "queued", "completed": 0, "total": 0, "done": False} for m in need],
            error=None,
            started_at=time.time(),
            finished_at=None,
        )
    base_url = st.provider_config().get("base_url") if st.provider_config().get("preset") in PROFILES.values() else None
    threading.Thread(target=_pull_worker, args=(need, base_url), name="ollama-pull", daemon=True).start()
    return {"ok": True, "models": need}


@router.post("/local-on")
def local_on(body: ProfileIn, request: Request):
    st = get_state(request)
    preset = _preset_for(body.profile)
    st.save_provider_config(preset=preset.id, model=None, base_url=None, api_key=None, cost_warn_cny=None)
    st.set_connection_mode(ConnectionMode.local)
    rep = doctor_report()
    return {"ok": True, "preset": preset.id, "connection_mode": "local", "ready": rep["profiles"][body.profile]["ready"], "provider": st.provider_config()}
