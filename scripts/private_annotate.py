"""Batch-annotate a chat export with an external model — privately, and traceably.

Why a script instead of copy-paste: these exports run to tens of thousands of messages across
several relationships. Pasting them into a chat window is not just slow, it silently loses the
context at every batch boundary (the first message of each paste has no history, so it is
systematically mislabelled), and nothing checks that the model returned the lines it was given.

What it does:
  1. reads a single-chat export JSON (`session` / `messages`), or every export
     under a directory;
  2. turns it into numbered lines (`[12] 2026-05-18 20:31 对方：…`), keeping both sides — the other
     person's line is what gets labelled, mine is the evidence;
  3. sends batches of ≤80 lines, repeating the previous batch's last 3 lines so the conversation
     is not cut mid-exchange;
  4. merges the model's per-batch JSON into one annotation file, keeping every failure visible.

Media it cannot see (images, stickers, video) are emitted as `[图片]`-style markers and must be
annotated as 「不确定」 — see `docs/ANNOTATION-PROMPT.md` for why guessing them is not allowed.

Everything it writes goes under `~/.rapport/eval/` (enforced by `scripts/_private.py`): this is
real material and must not land in the repo.

Usage:
    uv run python ../scripts/private_annotate.py "C:/path/API KEY.txt" \
        --material "D:/exports/私聊_某位/私聊_某位.json" --model gpt-5.6-luna --limit-batches 2
    uv run python ../scripts/private_annotate.py "C:/path/API KEY.txt" \
        --material "D:/exports" --model gpt-5.6-luna --batch-size 80 --sleep 0.5
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _private import ensure_private_out  # noqa: E402
from _probe import Probe  # noqa: E402
from real_byok_check import load_key  # noqa: E402

from rapport.config import PROVIDER_PRESETS  # noqa: E402
from rapport.llm.openai_compat import OpenAICompatProvider  # noqa: E402
from rapport.llm.provider import LLMError  # noqa: E402
from rapport.schemas.report_v1 import EMOTIONS, PRAGMATIC_TAGS  # noqa: E402

# Export message types → how they appear on an annotation line.
AS_TEXT = {"文本消息", "引用消息", "语音消息", "链接消息", "聊天记录", "小程序消息", "转账消息", "文件消息", "位置消息", "名片消息", "音乐消息"}
AS_MARKER = {"图片消息": "[图片]", "动画表情": "[动画表情]", "视频消息": "[视频]", "通话消息": "[通话]"}
# Types that carry no expression at all — facts about the app, not about the relationship.
NOISE = {"系统消息"}
MARKER_UNKNOWN = {"[图片]", "[动画表情]", "[视频]", "[通话]", "[其他消息]"}

ANNOTATE_SYSTEM = """你是中文对话语用标注员。输入是一段编号的聊天记录，每行形如：
[序号] 时间 说话人：内容

任务：只标注**说话人是「对方」**的行。说话人是「我」的行也要原样回报（text 与 speaker），但标签留空数组。

语用意图与情绪必须逐字取自下面的固定词表，不得自创、改写或翻译：

语用意图（可多选）：
真生气 / 撒娇假生气 / 调侃 / 反讽 / 委婉拒绝 / 寻求安慰 / 信息确认 / 分享陈述 / 询问 / 观点评价 / 抱怨 / 同意接受 / 不确定

情绪（可多选）：
不满 / 伤心 / 开心 / 担忧 / 亲近 / 混合 / 无明显情绪 / 无奈 / 惊讶困惑 / 不确定

边界（单选，指关系层面的边界，不是语气）：
none / explicit_decline / stop_contact / safety_concern

硬性要求：
1. text 必须与输入该行的内容逐字一致，不要改写、补全或合并相邻行；index 用输入给的序号。
2. 判断要引用**同一批里**支持它的原句作为 evidence（可以是「我」说的话）。没有依据时，标签写「不确定」并设 needs_review 为 true。禁止编造依据。
3. 内容形如 [图片] / [动画表情] / [视频] / [通话] / [其他消息] 的行：你**看不到内容**，必须标 pragmatic_tags 为 ["不确定"]、emotions 为 ["不确定"]、needs_review 为 true，并加 "media_unknown": true。**不许根据上下文猜这些是什么。**
4. 内容以 [语音转文字] 开头的是语音的转写，按普通文本标注（在 note 里标 "from_voice": true）。
5. 以 [引用 某某：…] 结尾的是引用消息，引用部分是上下文，正文才是标注对象。
6. 「信息确认」用于纯事实/安排类消息；系统类消息（撤回、红包提醒等）标「信息确认」或跳过，不要当情绪信号。
7. 不确定是合法答案，猜不是。

只输出一个 JSON 对象，不要解释文字：
{"batch": <批号>, "messages": [{"index": 1, "speaker": "other", "text": "逐字原文", "pragmatic_tags": ["调侃"], "emotions": ["开心"], "boundary": "none", "evidence": ["支持该判断的原句"], "confidence": "high", "needs_review": false}], "uncertain_summary": "这批里最没把握的是哪几条、为什么"}
"""


def load_export(path: Path) -> tuple[str, list[dict]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        raise ValueError(f"{path.name} 不是单聊导出格式（缺 messages 数组）")
    sess = data.get("session") or {}
    label = sess.get("remark") or sess.get("displayName") or sess.get("nickname") or path.stem
    return str(label), data["messages"]


def _body(m: dict) -> tuple[str, bool]:
    """(text shown to the annotator, is_media_marker)."""
    t = m.get("type", "")
    c = str(m.get("content") or "").strip()
    if t in AS_MARKER:
        return AS_MARKER[t], True
    if t in NOISE:
        return c or "[系统消息]", False
    if t in AS_TEXT:
        # `content` is already the right text for text / quote / voice-transcript / link messages.
        return (c or "[空]"), False
    return "[其他消息]", True


def to_lines(messages: list[dict]) -> list[dict]:
    out = []
    for i, m in enumerate(messages, 1):
        text, is_marker = _body(m)
        ts = m.get("createTime")
        when = dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if isinstance(ts, int) else ""
        # `who` is what the annotator reads (Chinese); `side` is what the code compares. Keeping
        # them apart on purpose: comparing a Chinese label against an English literal is how every
        # label in a batch got silently discarded once already.
        mine = m.get("isSend") == 1
        out.append({
            "index": i,
            "when": when,
            "who": "我" if mine else "对方",
            "side": "me" if mine else "other",
            "text": text,
            "media": bool(is_marker),
        })
    return out


def render_lines(lines: list[dict]) -> str:
    return "\n".join(f"[{ln['index']}] {ln['when']} {ln['who']}：{ln['text']}" for ln in lines)


def batches(lines: list[dict], size: int, carry: int = 3) -> list[list[dict]]:
    """Chunks of the timeline. Each chunk repeats the previous chunk's last `carry` lines so a
    reply is never read without the message it answers (that loss is silent in the output)."""
    out = []
    for start in range(0, len(lines), size):
        chunk = lines[start : start + size]
        if start and carry:
            chunk = lines[start - carry : start] + chunk
        out.append(chunk)
    return out


def _ask(provider, user: str, batch_no: int) -> tuple[dict | None, str | None]:
    try:
        data, _usage = provider.complete_json(ANNOTATE_SYSTEM, user, max_tokens=8192, temperature=0.2)
    except LLMError as e:
        return None, f"{e.code}: {e.msg}"
    except Exception as e:  # noqa: BLE001 - one bad batch must not kill the run
        return None, f"{type(e).__name__}: {e}"
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        return None, "返回的不是含 messages 数组的 JSON"
    return data, None


def _sanitize(entries: list, expected: dict[int, dict]) -> tuple[list[dict], list[str]]:
    """Keep only label values from the fixed lists, and only lines that were actually sent."""
    kept: list[dict] = []
    problems: list[str] = []
    for e in entries:
        if not isinstance(e, dict):
            problems.append("非对象条目")
            continue
        idx = e.get("index")
        src = expected.get(idx) if isinstance(idx, int) else None
        if src is None:
            problems.append(f"未知序号 {idx!r}")
            continue
        tags = [t for t in (e.get("pragmatic_tags") or []) if t in PRAGMATIC_TAGS]
        emos = [x for x in (e.get("emotions") or []) if x in EMOTIONS]
        dropped = [t for t in (e.get("pragmatic_tags") or []) if t not in PRAGMATIC_TAGS]
        if dropped:
            problems.append(f"[{idx}] 词表外标签 {dropped}")
        abstained = False
        if src["side"] != "other":
            tags, emos = [], []  # my own lines are context, never labels
        elif not tags:
            # An empty label list is not "there is nothing to label here" — the annotator abstained,
            # and recording it as an empty gold set would score every prediction against it as wrong
            # and quietly drag the metric down. Abstention has to be visible as abstention.
            tags, emos = ["不确定"], ["不确定"]
            abstained = True
        if src["media"] and not tags:
            tags, emos = ["不确定"], ["不确定"]
        rec = {
            "index": idx,
            "speaker": src["side"],
            "text": src["text"],  # taken from the source, not from the model — no paraphrasing can slip in
            "when": src["when"],
            "pragmatic_tags": tags,
            "emotions": emos,
            "boundary": e.get("boundary") if e.get("boundary") in ("none", "explicit_decline", "stop_contact", "safety_concern") else "none",
            "evidence": [str(x)[:80] for x in (e.get("evidence") or [])][:3],
            "confidence": e.get("confidence") if e.get("confidence") in ("high", "medium", "low") else "low",
            "needs_review": bool(e.get("needs_review")) or abstained or (e.get("confidence") not in ("high",)),
            "media_unknown": bool(e.get("media_unknown")) or src["media"],
        }
        kept.append(rec)
    return kept, problems


def annotate_one(provider, path: Path, args, probe: Probe, batch_no_start: int) -> dict:
    label, messages = load_export(path)
    lines = to_lines(messages)
    chunks = batches(lines, args.batch_size)
    if args.limit_batches:
        chunks = chunks[: args.limit_batches]
    by_index = {ln["index"]: ln for ln in lines}
    probe.line(f"\n=== {path.name} · {label} · {len(messages)} 条消息 → {len(chunks)} 批（每批 {args.batch_size} 行）===")

    kept: list[dict] = []
    problems: list[str] = []
    failures: list[str] = []
    for n, chunk in enumerate(chunks):
        if args.sleep and n:
            time.sleep(args.sleep)
        user = f"批号 {n + 1}。\n\n" + render_lines(chunk)
        data, err = _ask(provider, user, n + 1)
        if err:
            failures.append(f"批 {n + 1}: {err}")
            probe.line(f"  批 {n + 1}/{len(chunks)} 失败 {err}")
            continue
        if args.debug_raw:
            # When labels come back suspiciously empty, the first question is what the model
            # actually sent — not what our own code did with it.
            probe.line("    模型原始返回：" + json.dumps((data.get("messages") or [])[:2], ensure_ascii=False)[:400])
        part, probs = _sanitize(data.get("messages") or [], by_index)
        kept.extend(part)
        problems.extend(probs)
        others = [x for x in part if x["speaker"] == "other"]
        review = sum(1 for x in others if x["needs_review"])
        probe.line(f"  批 {n + 1}/{len(chunks)} 标注 {len(others)} 条（待复核 {review}）")
        probe.done(f"DONE rel={path.stem} batch={n + 1}/{len(chunks)} labelled={len(others)} review={review}")

    tagged = Counter(t for r in kept if r["speaker"] == "other" for t in r["pragmatic_tags"])
    return {
        "relationship": label,
        "source": path.name,
        "messages": kept,
        "problems": problems,
        "failures": failures,
        "stats": {
            "lines": len(lines),
            "labelled_other": sum(1 for r in kept if r["speaker"] == "other"),
            "needs_review": sum(1 for r in kept if r["speaker"] == "other" and r["needs_review"]),
            "media_unknown": sum(1 for r in kept if r.get("media_unknown")),
            "tag_counts": dict(tagged.most_common()),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("key_file", nargs="?", default=None, help="API key file; omit it when using --fake")
    ap.add_argument("--material", required=True, help="a single-chat export JSON, or a directory of them")
    ap.add_argument("--model", default=None, help="annotator model; use a different vendor from the system under test")
    ap.add_argument("--preset", default="custom")
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--batch-size", type=int, default=80, help="lines per request (both sides included)")
    ap.add_argument("--limit-batches", type=int, default=0, help="only the first N batches per export (for a trial run)")
    ap.add_argument("--sleep", type=float, default=0.0, help="seconds between requests")
    ap.add_argument("--progress", default=None)
    ap.add_argument("--debug-raw", action="store_true", help="print the model's raw reply for the first entries of each batch")
    ap.add_argument("--out", default=None, help="must be outside the repo; defaults to ~/.rapport/eval/annotations/")
    ap.add_argument("--fake", action="store_true", help="offline fake provider: no key, no network, no cost (writes fake labels)")
    args = ap.parse_args()

    material = Path(args.material)
    if not material.exists():
        ap.error(f"找不到材料：{material}")
    if material.is_dir():
        exports = sorted(material.glob("*/*.json")) or sorted(material.glob("*.json"))
    else:
        exports = [material]
    if not exports:
        ap.error(f"{material} 下没有找到单聊导出 JSON")

    from datetime import date

    out = ensure_private_out(args.out, f"annotations-{date.today().isoformat()}.json")

    if args.fake:
        from rapport.llm.fake import FakeProvider

        provider = FakeProvider()
        model = "fake（假供应商 · 仅供流程验证）"
    else:
        if not args.key_file:
            ap.error("需要 key_file；只想验证流程请用 --fake")
        if not args.model:
            ap.error("--model 必填：标注者应当是**与将来评测的主力模型不同厂商**的模型")
        key, file_url = load_key(Path(args.key_file))
        preset = args.preset if args.preset in PROVIDER_PRESETS else "custom"
        base_url = args.base_url or file_url or PROVIDER_PRESETS[preset].base_url
        provider = OpenAICompatProvider(preset, key, args.model, base_url, timeout=args.timeout)
        ok, msg = provider.ping()
        model = args.model
        if not ok:
            print(f"ping 失败：{msg}", file=sys.stderr)
            return 1

    probe = Probe(args.progress)
    probe.line(f"标注者：{model} · 导出文件 {len(exports)} 个 · 每批 {args.batch_size} 行")
    results = [annotate_one(provider, p, args, probe, 0) for p in exports]

    payload = {"annotator": model, "generated_at": dt.datetime.now().isoformat(timespec="seconds"), "exports": results}
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    labelled = sum(r["stats"]["labelled_other"] for r in results)
    review = sum(r["stats"]["needs_review"] for r in results)
    failed = sum(len(r["failures"]) for r in results)
    probe.line(f"\n共标注 {labelled} 条（待复核 {review}），失败 {failed} 批；写入 {out}")
    probe.line("下一步：annotation_check.py 校验格式，然后只复核 needs_review 的条目。")
    probe.done(f"DONE exports={len(results)} labelled={labelled} review={review} failed_batches={failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
