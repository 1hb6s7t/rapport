"""Real BYOK end-to-end run against the provider configured by an API-key file.

The key is read inside this process and never printed. Usage:
    uv run python ../scripts/real_byok_check.py "C:/path/API KEY.txt" [--preset deepseek] [--fixture warm|cooling|stop]
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from rapport.config import PROVIDER_PRESETS, reset_settings  # noqa: E402
from rapport.features import compute_features  # noqa: E402
from rapport.jobs.pipeline import ByokContext, PipelineHooks, run_byok  # noqa: E402
from rapport.llm.extract import build_payload  # noqa: E402
from rapport.llm.openai_compat import OpenAICompatProvider  # noqa: E402
from rapport.llm.provider import LLMError  # noqa: E402
from rapport.parsers import ParseOptions, parse  # noqa: E402
from rapport.report import to_markdown  # noqa: E402
from rapport.schemas.profile import Person, Profile  # noqa: E402

KEY_RE = re.compile(r"^[A-Za-z0-9_\-.:]{20,}$")
FIXTURES = {
    "warm": "paste_ab_warm.txt",
    "cooling": "paste_ab_cooling.txt",
    "stop": "paste_ab_stop_contact.txt",
}


def load_key(path: Path) -> tuple[str, str | None]:
    """Return (key, base_url_or_None). Prints only shapes, never characters."""
    text = None
    for enc in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            text = path.read_text(encoding=enc)
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
    if text is None:
        raise SystemExit("无法解码 Key 文件")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    print(f"key file: {len(lines)} line(s)")
    key = None
    base_url = None
    for i, ln in enumerate(lines, 1):
        is_url = ln.startswith("http://") or ln.startswith("https://")
        shape = "url" if is_url else ("key-like" if KEY_RE.match(ln) else "other")
        print(f"  line {i}: {len(ln)} chars, shape={shape}")
        if is_url and base_url is None:
            base_url = ln.rstrip("/")
        elif key is None and KEY_RE.match(ln):
            key = ln
    if key is None:
        raise SystemExit("没有找到形如 API Key 的行")
    return key, base_url


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("key_file")
    ap.add_argument("--preset", default="deepseek")
    ap.add_argument("--model", default=None)
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--fixture", default="warm", choices=sorted(FIXTURES))
    ap.add_argument("--window", type=int, default=60)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    key, file_base_url = load_key(Path(args.key_file))
    reset_settings(data_dir=Path(os.environ.get("TEMP", "/tmp")) / "rapport-real-byok", api_key=key)
    preset = args.preset if args.preset in PROVIDER_PRESETS else "custom"
    base_url = args.base_url or file_base_url or PROVIDER_PRESETS[preset].base_url
    print(f"provider: preset={preset} base_url={base_url} model={args.model or PROVIDER_PRESETS[preset].default_model}")

    provider = OpenAICompatProvider(preset, key, args.model, base_url)
    ok, msg = provider.ping()
    print(f"ping: ok={ok} msg={msg}")
    if not ok:
        return 2

    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic" / FIXTURES[args.fixture]
    conv, parser_id = parse(fixture.read_text(encoding="utf-8"), "real", None, fixture.name, ParseOptions())
    events = conv.events()
    profile = Profile(me=Person(alias="我"), other=Person(alias="对方", mbti="ENTJ"), goal="invite")
    payload = build_payload(conv, events, window_size=args.window, overlap=5)
    print(f"fixture={fixture.name} parser={parser_id} events={len(events)} windows={len(payload.windows)} est_tokens_in={payload.est_tokens_in}")

    stages: list[str] = []
    ctx = ByokContext(
        provider=provider,
        payload=payload,
        versions_extra={"provider": provider.info.preset, "model": provider.info.model, "price_date": provider.info.price_date},
        preset=provider.info.preset,
    )
    try:
        rep = run_byok(conv, events, profile, ctx, PipelineHooks(progress=lambda p: stages.append(f"{p.stage} {p.done}/{p.total}")))
    except LLMError as e:
        print(f"LLM ERROR [{e.code}] {e.msg}")
        return 3

    f = compute_features(conv, events)
    print(f"\n-- rule side: coverage={rep.affinity.coverage} total={rep.affinity.total} sessions={f.n_sessions}")
    print(f"-- ai_status={rep.ai_status} error={rep.ai_error}")
    print(f"-- calls={rep.cost.actual_usage.calls} tokens_in={rep.cost.actual_usage.prompt_tokens} tokens_out={rep.cost.actual_usage.completion_tokens} cost_cny={rep.cost.actual_cny}")
    print("-- dimensions: " + ", ".join(f"{k}=rule {v.rule}/ai {v.llm}/final {v.score} ({v.confidence})" for k, v in rep.affinity.dims.items()))
    print(f"-- stage={rep.stage.value} boundary={rep.boundary.value} observations={len(rep.observations)} traits={len(rep.persona.traits)} actions={len(rep.playbook.actions)} landmines={len(rep.playbook.landmines)}")
    valid = {m.id for m in events}
    bad = [i for o in rep.observations for i in o.evidence_ids if i not in valid]
    bad += [i for a in rep.playbook.actions for i in a.basis_ids if i not in valid]
    print(f"-- evidence ids valid: {not bad}" + (f" INVALID={bad}" if bad else ""))
    print(f"-- every action has stop_when: {all(a.stop_when for a in rep.playbook.actions)}")
    print(f"-- every observation has evidence: {all(o.evidence_ids for o in rep.observations)}")
    for a in rep.playbook.actions:
        print(f"   action: {a.goal} | stop_when: {a.stop_when[:40]} | text: {a.suggested_text[:60]}")
    for o in rep.observations[:3]:
        print(f"   obs: {o.claim} [{','.join(o.evidence_ids)}]")
    print(f"-- notes: {rep.notes}")
    print(f"-- stages: {stages[:4]} … {stages[-2:]}")

    if args.out:
        Path(args.out).write_text(to_markdown(rep, include_evidence=True), encoding="utf-8")
        print(f"-- markdown -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
