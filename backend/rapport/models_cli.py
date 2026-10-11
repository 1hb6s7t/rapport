"""`rapport models`: doctor / pull / local-on for the local (Ollama) profiles.

Zero cost, zero network except pulling weights from Ollama's registry. Everything printed is
about the machine and the models; never about the user's material.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import typer

from rapport.config import PROVIDER_PRESETS

models_app = typer.Typer(help="本地模型：检测、拉取、一键切换", no_args_is_help=True, add_completion=False)

PROFILES = {"solo": "ollama", "split": "ollama-split", "lite": "ollama-lite"}


def _ollama_exe() -> str | None:
    exe = shutil.which("ollama")
    if exe:
        return exe
    for cand in (
        Path.home() / "AppData" / "Local" / "Programs" / "Ollama" / "ollama.exe",
        Path("C:/Program Files/Ollama/ollama.exe"),
        Path("/usr/local/bin/ollama"),
        Path("/opt/homebrew/bin/ollama"),
    ):
        if cand.exists():
            return str(cand)
    return None


def _gpu() -> tuple[str | None, float | None]:
    """(name, vram_gb) via nvidia-smi when present; (None, None) otherwise."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None, None
    try:
        out = subprocess.run([exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10)
        line = (out.stdout or "").strip().splitlines()[0]
        name, mem = [x.strip() for x in line.split(",")[:2]]
        return name, round(float(mem) / 1024, 1)
    except Exception:  # noqa: BLE001
        return None, None


def _ram_gb() -> float | None:
    try:
        if sys.platform == "win32":
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            ms = MS()
            ms.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
            return round(ms.ullTotalPhys / 1024**3, 1)
        import os

        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024**3, 1)
    except Exception:  # noqa: BLE001
        return None


def models_dir() -> str:
    """Where Ollama stores weights: `OLLAMA_MODELS` (process, then user-level on Windows) or its default."""
    import os

    v = os.environ.get("OLLAMA_MODELS")
    if not v and sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                v = winreg.QueryValueEx(k, "OLLAMA_MODELS")[0]
        except OSError:
            v = None
    return v or str(Path.home() / ".ollama" / "models")


def recommend(vram_gb: float | None, ram_gb: float | None) -> str:
    if vram_gb is not None and vram_gb >= 7.5:
        return "solo"
    if vram_gb is not None and vram_gb >= 3.5:
        return "lite"
    if ram_gb is not None and ram_gb >= 16:
        return "lite"
    return "lite"


def doctor_report() -> dict:
    from rapport.llm.ollama import list_local_models, ollama_version
    from rapport.llm.provider import LLMError

    exe = _ollama_exe()
    version = ollama_version()
    running = version is not None
    models: list[dict] = []
    err = None
    if running:
        try:
            models = list_local_models()
        except LLMError as e:
            err = e.msg
    gpu_name, vram = _gpu()
    ram = _ram_gb()
    rec = recommend(vram, ram)
    from rapport.llm.ollama import has_model

    have = {m["name"] for m in models}
    profiles = {}
    for key, preset in PROFILES.items():
        p = PROVIDER_PRESETS[preset]
        need = sorted(set((p.task_models or {}).values()))
        missing = [m for m in need if not has_model(m, have)]
        profiles[key] = {"preset": preset, "label": p.profile_label, "models": need, "missing": missing, "ready": running and not missing}
    return {
        "ollama_installed": exe is not None,
        "ollama_path": exe,
        "ollama_running": running,
        "ollama_version": version,
        "error": err,
        "gpu": gpu_name,
        "vram_gb": vram,
        "ram_gb": ram,
        "models": models,
        "models_dir": models_dir(),
        "recommended_profile": rec,
        "profiles": profiles,
        "presets_task_models": {k: dict(PROVIDER_PRESETS[v].task_models or {}) for k, v in PROFILES.items()},
        "notes": [
            "solo 需要约 6 GB 显存常驻；split 两个模型交替；lite 4B 在无独显机器上走 CPU，一窗约 30–60 秒。",
            "Qwen3 的 thinking 已在调用里关闭（think:false），否则 JSON 会被思考 token 挤掉。",
        ],
    }


@models_app.command()
def doctor(as_json: bool = typer.Option(False, "--json")):
    """检测 Ollama、显存、已拉模型，并给出档位建议。"""
    rep = doctor_report()
    if as_json:
        typer.echo(json.dumps(rep, ensure_ascii=False, indent=1))
        return
    typer.secho(f"Ollama：{'已安装' if rep['ollama_installed'] else '未安装'}" + (f"（{rep['ollama_path']}）" if rep["ollama_path"] else ""), fg="green" if rep["ollama_installed"] else "yellow")
    typer.echo(f"运行中：{'是，版本 ' + str(rep['ollama_version']) if rep['ollama_running'] else '否（先启动 Ollama 应用或运行 `ollama serve`）'}")
    typer.echo(f"GPU：{rep['gpu'] or '未检测到独显'}" + (f"，显存 {rep['vram_gb']} GB" if rep["vram_gb"] else "") + (f"；内存 {rep['ram_gb']} GB" if rep["ram_gb"] else ""))
    if rep["models"]:
        typer.echo("已拉取：" + "，".join(f"{m['name']}（{m['size_gb']} GB）" for m in rep["models"]))
    else:
        typer.echo("已拉取：无")
    typer.echo(f"权重目录：{rep['models_dir']}（改目录：设置环境变量 OLLAMA_MODELS 后重启 Ollama）")
    typer.echo(f"建议档位：{rep['recommended_profile']}")
    for key, p in rep["profiles"].items():
        status = "就绪" if p["ready"] else ("缺 " + "、".join(p["missing"]) if p["missing"] else "Ollama 未运行")
        typer.echo(f"  {key:<5} {p['label']}  →  {status}")
    for n in rep["notes"]:
        typer.echo(f"  · {n}")
    if not rep["ollama_installed"]:
        typer.echo("安装：Windows 用 `winget install Ollama.Ollama`，或 https://ollama.com/download")


@models_app.command()
def pull(profile: str = typer.Option("solo", help="solo | split | lite"), check: bool = typer.Option(True, help="拉取后做一次 JSON 自检")):
    """拉取档位所需模型，并对每个模型做一次 20 token 的 JSON 自检。"""
    if profile not in PROFILES:
        raise typer.BadParameter("profile 只能是 solo / split / lite")
    exe = _ollama_exe()
    if not exe:
        typer.secho("未找到 ollama 可执行文件；先安装 Ollama。", fg="red")
        raise typer.Exit(2)
    preset = PROVIDER_PRESETS[PROFILES[profile]]
    need = sorted(set((preset.task_models or {}).values()))
    for m in need:
        typer.secho(f"拉取 {m} …", fg="cyan")
        rc = subprocess.call([exe, "pull", m])
        if rc != 0:
            typer.secho(f"拉取 {m} 失败（exit {rc}）", fg="red")
            raise typer.Exit(rc)
    if check:
        from rapport.llm.ollama import OllamaProvider

        bad = 0
        for m in need:
            ok, msg = OllamaProvider(PROFILES[profile], m, task="intent").ping()
            typer.echo(f"  自检 {m}: {'OK' if ok else 'FAIL'} {msg}")
            bad += 0 if ok else 1
        if bad:
            raise typer.Exit(1)
    typer.secho(f"DONE profile={profile} models={','.join(need)}", fg="green")


@models_app.command("local-on")
def local_on(profile: str = typer.Option("solo", help="solo | split | lite")):
    """把供应商配置切到本地档位，并把连接模式设为 local（零出网）。"""
    if profile not in PROFILES:
        raise typer.BadParameter("profile 只能是 solo / split / lite")
    from rapport.config import ConnectionMode, get_settings
    from rapport.state import AppState

    st = AppState(get_settings())
    preset = PROFILES[profile]
    st.save_provider_config(preset=preset, model=None, base_url=None, api_key=None, cost_warn_cny=None)
    st.set_connection_mode(ConnectionMode.local)
    typer.secho(f"已切换：preset={preset}，connection_mode=local。重启服务后生效。", fg="green")
