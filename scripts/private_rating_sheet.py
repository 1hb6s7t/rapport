"""A five-dimension rating sheet built from the user's own chats — private, never in the repo.

This is the only script in `scripts/` that reads real material, so it is the one that has to be
careful:

- output always goes under `~/.rapport/eval/` (`ensure_private_out` refuses any repo path), and the
  sheet says in its own header that the folder holds real conversation fragments;
- sending material to a cloud endpoint is an explicit opt-in (`--allow-cloud`); by default only a
  loopback endpoint is accepted, which is the same local-first rule the product enforces. There is
  no silent fallback;
- the run uses its own data directory, so evaluating material never touches the user's product
  database.

The sheet is for the user to fill in: for each time window and each dimension, 「偏高 / 合适 / 偏低」.
Plan §1's gate is ≥70% 「合适」 on ≥3 relationships × 2 windows, with at most 1/5 in the wrong
direction. Nothing here computes that verdict — the user's own judgement is the measurement.

Usage:
    uv run python ../scripts/private_rating_sheet.py "C:/path/API KEY.txt" \
        --material "C:/path/chat.txt" --label 小林 --split 2 --allow-cloud
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _private import ensure_private_out, private_eval_dir  # noqa: E402
from _probe import Probe  # noqa: E402
from real_byok_check import load_key  # noqa: E402

from rapport.config import PROVIDER_PRESETS, reset_settings  # noqa: E402
from rapport.jobs.pipeline import ByokContext, run_byok  # noqa: E402
from rapport.llm.extract import build_payload  # noqa: E402
from rapport.llm.openai_compat import OpenAICompatProvider  # noqa: E402
from rapport.parsers import ParseOptions, parse  # noqa: E402
from rapport.schemas.profile import Person, Profile  # noqa: E402
from rapport.schemas.report_v1 import DIMENSIONS  # noqa: E402

DIM_LABELS = {
    "responsiveness": "响应度",
    "initiative": "主动性",
    "investment": "投入度",
    "warmth": "温度",
    "future": "未来指向",
}


def _is_loopback(url: str | None) -> bool:
    host = urlparse(url or "").hostname or ""
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def split_events(events: list, parts: int) -> list[list]:
    """Split by message order into `parts` windows (earlier … most recent). Order, not timestamps,
    because a paste may carry no usable dates at all."""
    if parts <= 1 or len(events) < parts:
        return [events]
    size = len(events) // parts
    chunks = [events[i * size : (i + 1) * size] for i in range(parts - 1)]
    chunks.append(events[(parts - 1) * size :])
    return [c for c in chunks if c]


def render_sheet(label: str, model: str, material: str, windows: list[dict]) -> str:
    import datetime as _dt

    dims = " | ".join(DIM_LABELS[d] for d in DIMENSIONS)
    L = [
        f"# {label} · 五维评分表（私有）",
        "",
        f"- 日期：{_dt.date.today().isoformat()}",
        f"- 材料文件：`{material}`（本表**不复制**聊天内容，只给分数）",
        f"- 模型：`{model}`",
        f"- 时间窗：{len(windows)} 段（按消息顺序切分，越靠后越近）",
        "",
        "> ⚠️ 本目录下的文件含有你的真实对话片段（报告 JSON 里有证据原文）。不要分享、不要提交到任何仓库。",
        "",
        f"| 段 | 消息数 | total | {dims} | 证据条数 | 你的判断（偏高 / 合适 / 偏低） |",
        "|---|---:|---:|" + "---:|" * (len(DIMENSIONS) + 2),
    ]
    for w in windows:
        if w["error"]:
            L.append(f"| {w['name']} | {w['messages']} | — | " + "— | " * len(DIMENSIONS) + f"— | 运行失败：{w['error']} |")
            continue
        cells = " | ".join(str(w["dims"].get(d, "—")) for d in DIMENSIONS)
        L.append(f"| {w['name']} | {w['messages']} | {w['total']} | {cells} | {w['evidence']} |  |")
    L += [
        "",
        "## 怎么填",
        "",
        "- 每格三选一：**偏高**（你觉得自己真实的好感度比这个分数低）/ **合适** / **偏低**（真实比这个分数高）。",
        "- 拿不准就留空——留空是有用的信息，猜一个会污染结论。",
        "- 目的：≥3 段关系 × ≥2 个时间窗后，才能判断这套权重是否值得信。单个窗口的一次打分说明不了问题。",
        "- 填完的表格不要放进仓库；方向校准在阶段 6 用 `scripts/outcome_direction.py` 统一看。",
    ]
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("key_file", nargs="?", default=None, help="API key file; omit it when using --fake")
    ap.add_argument("--material", required=True, help="path to the real chat file (never copied into the repo)")
    ap.add_argument("--label", default=None, help="a name for this relationship; defaults to the file stem")
    ap.add_argument("--split", type=int, default=1, help="split the material into N time windows, each scored separately")
    ap.add_argument("--allow-cloud", action="store_true", help="agree to send this material to a non-loopback endpoint")
    ap.add_argument("--preset", default="deepseek")
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--window", type=int, default=30, help="messages per extraction window")
    ap.add_argument("--progress", default=None, help="append one line per finished window here")
    ap.add_argument("--out", default=None, help="must be outside the repo; defaults to ~/.rapport/eval/")
    ap.add_argument("--fake", action="store_true", help="offline fake provider: no key, no network, no cost")
    args = ap.parse_args()

    material = Path(args.material)
    if not material.exists():
        ap.error(f"材料文件不存在：{material}")
    label = args.label or material.stem

    # Resolve the output path *before* anything is sent anywhere: a refused path must not be
    # discovered after a paid run that already shipped the user's chats to an endpoint.
    from datetime import date

    out_md = ensure_private_out(args.out, f"{label}-{date.today().isoformat()}-rating.md")

    if args.fake:
        from rapport.llm.fake import FakeProvider

        provider = FakeProvider()
        model = "fake-1（假供应商 · 仅供流程验证）"
        print("provider=fake（离线假供应商：不出网、不产生费用；分数无意义）")
    else:
        if not args.key_file:
            ap.error("需要 key_file；只想验证流程请用 --fake")
        key, file_url = load_key(Path(args.key_file))
        preset = args.preset if args.preset in PROVIDER_PRESETS else "custom"
        base_url = args.base_url or file_url or PROVIDER_PRESETS[preset].base_url
        if not _is_loopback(base_url) and not args.allow_cloud:
            print(
                f"拒绝运行：端点 {base_url} 不在本机。这一步会把你的真实聊天发到该服务，"
                "确认后加 --allow-cloud 重跑（本地模型则无需此项）。",
                file=sys.stderr,
            )
            return 2
        provider = OpenAICompatProvider(preset, key, None, base_url, timeout=args.timeout)
        ok, msg = provider.ping()
        print(f"ping: ok={ok} msg={msg}", flush=True)
        if not ok:
            return 1
        model = provider.info.model

    # A private data dir of its own: evaluating material must not write into the product database.
    reset_settings(data_dir=private_eval_dir() / "run-rating-sheet")
    conv, _parser = parse(material.read_text(encoding="utf-8"), label, None, material.name, ParseOptions())
    events = conv.events()
    profile = Profile(me=Person(alias="我"), other=Person(alias="对方"), goal="invite")
    chunks = split_events(events, args.split)
    probe = Probe(args.progress)
    probe.line(f"材料 {material.name}（{len(events)} 条消息）→ {len(chunks)} 个时间窗 · 模型 {model}")

    windows: list[dict] = []
    total_calls = 0
    for i, chunk in enumerate(chunks, 1):
        name = f"第 {i}/{len(chunks)} 段" + ("（最近）" if i == len(chunks) and len(chunks) > 1 else "（较早）" if len(chunks) > 1 else "")
        ctx = ByokContext(
            provider=provider,
            payload=build_payload(conv, chunk, window_size=args.window, overlap=5),
            versions_extra={"provider": provider.info.preset, "model": getattr(provider.info, "model", model), "price_date": provider.info.price_date},
            preset=provider.info.preset,
            run_intent=True,
        )
        try:
            rep = run_byok(conv, chunk, profile, ctx)
        except Exception as e:  # noqa: BLE001 - one failed window must not lose the others
            windows.append({"name": name, "messages": len(chunk), "error": f"{type(e).__name__}: {e}"})
            probe.line(f"[{name}] 失败 {type(e).__name__}: {e}")
            continue
        total_calls += getattr(getattr(rep, "cost", None), "calls", 0)
        windows.append(
            {
                "name": name,
                "messages": len(chunk),
                "error": None,
                "total": rep.affinity.total,
                "dims": {d: (rep.affinity.dims[d].score if d in rep.affinity.dims else None) for d in DIMENSIONS},
                "evidence": len(rep.evidence),
                "report": rep.model_dump(mode="json"),
            }
        )
        probe.line(f"[{name}] total={rep.affinity.total} dims={windows[-1]['dims']}")
        probe.done(f"DONE window={i}/{len(chunks)} messages={len(chunk)} total={rep.affinity.total}")

    out_md.write_text(render_sheet(label, model, material.name, windows), encoding="utf-8")
    out_json = out_md.with_suffix(".json")
    out_json.write_text(
        json.dumps({"label": label, "model": model, "windows": windows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    failed = sum(1 for w in windows if w["error"])
    probe.done(f"DONE label={label} windows={len(windows)} failed={failed} out={out_md.name}")
    print(f"\n评分表 {out_md}\n报告数据 {out_json}")
    return 0 if failed < len(windows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
