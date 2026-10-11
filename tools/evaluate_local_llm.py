"""Zero-shot emotion classification with a local instruct model, scored against the reference labels.

    python tools/evaluate_local_llm.py --data-dir desktop-data --dataset-id <id> --model <path> [--limit N]

Same rows, same label set, same metrics as tools/evaluate_grounded.py --gold, so the numbers are
directly comparable with the fused offline layer. The prompt is the workbench rubric. With
--context the model also sees the causal context the workbench shows a human annotator; without
it, the model sees the single message only, exactly what the deployed layer sees.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

FIXED_EMOTIONS = ("不满", "伤心", "开心", "担忧", "亲近", "混合", "无明显情绪", "无奈", "惊讶困惑", "不确定")
HINTS = {
    "不满": "生气、嫌弃、责备",
    "伤心": "难过、失落、委屈",
    "开心": "高兴、兴奋、满足",
    "担忧": "担心、不安、害怕",
    "亲近": "亲昵、喜欢、想念",
    "混合": "几种情绪同时明显",
    "无明显情绪": "平铺直叙、链接、通知、功能性对话",
    "无奈": "无力、认命、算了",
    "惊讶困惑": "意外、不解、反问",
    "不确定": "看不出来，材料不够",
}
SYSTEM = "你是一个中文聊天情绪标注器。只输出一个标签，不要解释。"


def build_prompt(text: str, style: str = "v1", context: str = "") -> str:
    lines = ["读下面这条微信聊天消息，从固定标签集中选出一个最贴切的情绪标签。", "", "标签集："]
    for lab in FIXED_EMOTIONS:
        lines.append(f"- {lab}：{HINTS[lab]}")
    if context:
        lines += ["", "上文（仅供理解语境，不要标注它）：", context]
    lines += ["", "要标注的消息：" + text, ""]
    if style == "v2":
        lines.append(
            "注意：日常微信消息里大多数是功能性的或闲聊内容，应当判为「无明显情绪」；不要为了填而硬猜情绪。只有情绪确实明显时才选情绪类。"
        )
    lines.append("只输出标签本身。")
    return "\n".join(lines)


def parse_label(raw: str) -> str:
    s = raw.strip().strip("。.，,：:\"' \n")
    if s in FIXED_EMOTIONS:
        return s
    for lab in sorted(FIXED_EMOTIONS, key=len, reverse=True):
        if lab in s:
            return lab
    return "不确定"


def _prf(truth: list[str], preds: list[str], labels: list[str]) -> dict:
    per = {}
    for lab in labels:
        tp = sum(1 for t, p in zip(truth, preds) if t == lab and p == lab)
        fp = sum(1 for t, p in zip(truth, preds) if t != lab and p == lab)
        fn = sum(1 for t, p in zip(truth, preds) if t == lab and p != lab)
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        per[lab] = {"precision": round(prec, 4), "recall": round(rec, 4), "f1": round(f1, 4), "support": tp + fn}
    macro = sum(v["f1"] for v in per.values()) / len(per) if per else 0.0
    acc = sum(1 for t, p in zip(truth, preds) if t == p) / max(len(truth), 1)
    return {"macro_f1": round(macro, 4), "accuracy": round(acc, 4), "per_class": per}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--dataset-id", required=True)
    ap.add_argument("--pool", default="offline_review")
    ap.add_argument("--model", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--max-new-tokens", type=int, default=12)
    ap.add_argument("--prompt-style", default="v1", choices=["v1", "v2"])
    ap.add_argument("--context", action="store_true", help="show the causal context before the target")
    ap.add_argument("--context-messages", type=int, default=6)
    ap.add_argument("--out", default="output/grounded-eval-llm.json")
    ap.add_argument("--dump-predictions", default="")
    args = ap.parse_args()

    data_dir = Path(args.data_dir).resolve()
    os.environ.setdefault("RAPPORT_DATA_DIR", str(data_dir))

    from rapport.datasets import DatasetStore
    from rapport.security import Crypto

    store = DatasetStore(data_dir / "datasets", Crypto(data_dir / ".key"))
    rows = store.records(args.dataset_id, args.pool)
    by_id = {str(r.get("id")): r for r in rows}

    con = sqlite3.connect(f"file:{(data_dir / 'app.db').as_posix()}?mode=ro", uri=True)
    try:
        gold = {
            sid: emo
            for sid, emo, skipped in con.execute(
                "SELECT sample_id, emotion, skipped FROM gold_labels WHERE dataset_id = ?", (args.dataset_id,)
            )
            if emo and not skipped
        }
    finally:
        con.close()
    if not gold:
        print("no reference labels stored for this dataset yet")
        return 4

    def context_text(rec: dict) -> str:
        if not args.context:
            return ""
        ti = rec.get("target_index")
        msgs = [
            m
            for m in (rec.get("context") or [])
            if isinstance(m, dict) and (ti is None or m.get("index", 0) < ti)
        ]
        msgs = msgs[-args.context_messages :]
        out = []
        for m in msgs:
            who = "我" if str(m.get("sender")) in ("me", "self") else "对方"
            body = (m.get("text") or "").strip() or f"[{m.get('type') or '消息'}]"
            out.append(f"{who}：{body}")
        return "\n".join(out)

    items = []
    for sid, emo in gold.items():
        rec = by_id.get(sid)
        if not rec:
            continue
        txt = (rec.get("text") or "").strip()
        if len(txt) < 2:
            continue
        items.append((sid, emo, txt, context_text(rec)))
    items.sort(key=lambda x: x[0])
    if args.limit:
        items = items[: args.limit]
    print(f"rows: {len(items)} | model: {args.model} | context: {args.context}", flush=True)

    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(args.model)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    tok.padding_side = "left"
    model = AutoModelForCausalLM.from_pretrained(args.model, torch_dtype="bfloat16", device_map="cuda")
    model.eval()

    preds: list[str] = []
    raws: list[str] = []
    for i in range(0, len(items), args.batch_size):
        batch = items[i : i + args.batch_size]
        prompts = []
        for _sid, _truth, text, ctx in batch:
            msgs = [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": build_prompt(text, args.prompt_style, ctx)},
            ]
            prompts.append(
                tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True, enable_thinking=False)
            )
        enc = tok(prompts, return_tensors="pt", padding=True, truncation=True, max_length=2048).to(model.device)
        with torch.no_grad():
            out = model.generate(
                **enc, max_new_tokens=args.max_new_tokens, do_sample=False, pad_token_id=tok.pad_token_id
            )
        gen = out[:, enc["input_ids"].shape[1] :]
        for g in gen:
            raw = tok.decode(g, skip_special_tokens=True).strip()
            raws.append(raw)
            preds.append(parse_label(raw))
        print(f"  generated {min(i + args.batch_size, len(items))}/{len(items)}", flush=True)

    truth = [t for _s, t, _x, _c in items]
    ids = [s for s, _t, _x, _c in items]
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset_id": args.dataset_id,
        "pool": args.pool,
        "n": len(items),
        "label_source": "claude_reference（Claude 独立标注，非人工金标）",
        "model": args.model,
        "prompt": (
            f"workbench rubric {args.prompt_style}, "
            + (f"last {args.context_messages} context messages, " if args.context else "single message, ")
            + "zero-shot, thinking disabled"
        ),
        "results": _prf(truth, preds, list(FIXED_EMOTIONS)),
        "pred_distribution": dict(Counter(preds)),
        "unparsed": sum(1 for r in raws if parse_label(r) == "不确定" and "不确定" not in r),
    }
    out_path = ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    r = report["results"]
    lines = [
        f"# 本地小指令模型零样本评测（{args.dataset_id} · {args.pool} · {Path(args.model).name} · {len(items)} 条）",
        "",
        "标签为 Claude 独立标注（独立于弱标注，非人工金标）；下列数字是「与独立参考标注的一致度」。",
        "",
        f"提示词：{report['prompt']}",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
        f"| macro-F1 | {r['macro_f1']} |",
        f"| accuracy | {r['accuracy']} |",
        "",
        "| 类别 | precision | recall | F1 | n |",
        "| --- | --- | --- | --- | --- |",
    ]
    for lab, v in r["per_class"].items():
        lines.append(f"| {lab} | {v['precision']} | {v['recall']} | {v['f1']} | {v['support']} |")
    (out_path.with_suffix(".md")).write_text("\n".join(lines) + "\n", encoding="utf-8")

    if args.dump_predictions:
        dump = ROOT / args.dump_predictions
        dump.parent.mkdir(parents=True, exist_ok=True)
        dump.write_text(
            json.dumps(
                {sid: {"truth": t, "pred": p, "raw": raw} for sid, t, p, raw in zip(ids, truth, preds, raws)},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    print("\n".join(lines))
    print(f"\nwrote {out_path} and {out_path.with_suffix('.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
