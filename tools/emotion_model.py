"""Download, export and verify the bundled offline emotion classifier.

The runtime ships no torch, so this tool runs in three steps, each in the venv that can do it:

  python tools/emotion_model.py download --out models/emotion-zh-tiny            # any venv
  <train venv>\\Scripts\\python.exe tools/emotion_model.py export --dir models/emotion-zh-tiny
  <backend venv>\\Scripts\\python.exe tools/emotion_model.py verify --dir models/emotion-zh-tiny

`verify` writes manifest.json only after the pure-Python tokenizer reproduces the reference
tokenizer's ids, sample by sample, and the ONNX logits match torch within 1e-4. The runtime
trusts that manifest and nothing else.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

MODEL_ID = "iic/nlp_structbert_emotion-classification_chinese-tiny"
SOURCE_URL = f"https://www.modelscope.cn/models/{MODEL_ID}"
REPO_URL = f"https://www.modelscope.cn/api/v1/models/{MODEL_ID}/repo?Revision=master&FilePath={{path}}"
FILES = ["config.json", "configuration.json", "vocab.txt", "README.md", "pytorch_model.bin"]
EXPECTED_BYTES = {"pytorch_model.bin": 35098895, "vocab.txt": 109540}
MAX_LEN = 128

SAMPLES = [
    "今天真的好开心呀，谢谢你陪我！",
    "你怎么又这样，我真的受够了。",
    "我有点担心明天的面试，睡不着。",
    "……",
    "哈哈哈这也太搞笑了吧 😂",
    "OK, see you tomorrow at 8pm.",
    "这是真的吗？我不敢相信。",
    "没事，我习惯了。",
    "",
    "你上次说的那家店，我去吃了，还不错。",
    "烦死了烦死了烦死了！！！",
    "I'm so sad today... nothing works.",
    "好的好的我知道了，你别念了。",
    "妈妈明天来我家，得收拾一下。",
    "Café au lait, naïve façade.",  # accents: exercises strip-accents parity
    "长文本" * 80,  # 240 chars: exercises truncation to MAX_LEN
]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(args) -> int:
    out = Path(args.out) / "raw"
    out.mkdir(parents=True, exist_ok=True)
    records = {}
    for name in FILES:
        dest = out / name
        if args.skip_existing and dest.is_file() and dest.stat().st_size > 0:
            pass
        else:
            url = REPO_URL.format(path=name)
            req = urllib.request.Request(url, headers={"User-Agent": "rapport-emotion-fetch/1"})
            tmp = dest.with_suffix(dest.suffix + ".part")
            with urllib.request.urlopen(req, timeout=120) as resp, tmp.open("wb") as fh:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    fh.write(chunk)
            tmp.replace(dest)
        size = dest.stat().st_size
        expected = EXPECTED_BYTES.get(name)
        flag = "OK" if expected is None or expected == size else f"UNEXPECTED (expected {expected})"
        records[name] = {"bytes": size, "sha256": _sha256(dest), "url": REPO_URL.format(path=name), "check": flag}
        print(f"{name}: {size} bytes [{flag}]", flush=True)
    (out / "download.json").write_text(
        json.dumps({"model": MODEL_ID, "source": SOURCE_URL, "files": records, "downloaded_at": datetime.now(timezone.utc).isoformat()}, indent=2),
        encoding="utf-8",
    )
    print(f"download manifest -> {out / 'download.json'}")
    return 0


def export(args) -> int:
    import torch
    import transformers
    from transformers import BertForSequenceClassification, BertTokenizerFast

    root = Path(args.dir)
    raw = root / "raw"
    tok = BertTokenizerFast(vocab_file=str(raw / "vocab.txt"))
    model, loading = BertForSequenceClassification.from_pretrained(str(raw), output_loading_info=True)
    print("labels:", [model.config.id2label[i] for i in range(len(model.config.id2label))])
    print("missing keys:", loading.get("missing_keys"))
    print("unexpected keys:", loading.get("unexpected_keys"))
    model.eval()
    for p in model.parameters():
        p.requires_grad = False

    samples = []
    with torch.no_grad():
        for text in SAMPLES:
            ids = tok(text, truncation=True, max_length=MAX_LEN)["input_ids"]
            tensor = torch.tensor([ids], dtype=torch.long)
            logits = model(input_ids=tensor, attention_mask=torch.ones_like(tensor), token_type_ids=torch.zeros_like(tensor)).logits
            samples.append({"text": text, "ids": ids, "logits": [round(x, 6) for x in logits[0].tolist()]})

        batch_texts = SAMPLES[:3]
        enc = tok(batch_texts, padding=True, truncation=True, max_length=MAX_LEN, return_tensors="pt")
        batch_logits = model(**enc).logits

    onnx_path = root / "model.onnx"
    dummy = torch.tensor([samples[0]["ids"]], dtype=torch.long)
    torch.onnx.export(
        model,
        (dummy, torch.ones_like(dummy), torch.zeros_like(dummy)),
        str(onnx_path),
        input_names=["input_ids", "attention_mask", "token_type_ids"],
        output_names=["logits"],
        dynamic_axes={
            "input_ids": {0: "batch", 1: "seq"},
            "attention_mask": {0: "batch", 1: "seq"},
            "token_type_ids": {0: "batch", 1: "seq"},
            "logits": {0: "batch"},
        },
        opset_version=14,
        do_constant_folding=True,
    )
    (root / "vocab.txt").write_bytes((raw / "vocab.txt").read_bytes())
    parity = {
        "max_len": MAX_LEN,
        "samples": samples,
        "batch": {
            "texts": batch_texts,
            "ids": enc["input_ids"].tolist(),
            "attention_mask": enc["attention_mask"].tolist(),
            "token_type_ids": enc["token_type_ids"].tolist(),
            "logits": [[round(x, 6) for x in row] for row in batch_logits.tolist()],
        },
    }
    (root / "parity.json").write_text(json.dumps(parity, ensure_ascii=False), encoding="utf-8")
    info = {
        "torch": torch.__version__,
        "transformers": transformers.__version__,
        "opset": 14,
        "missing_keys": list(loading.get("missing_keys") or []),
        "unexpected_keys": list(loading.get("unexpected_keys") or []),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (root / "export-info.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    print(f"model.onnx: {onnx_path.stat().st_size} bytes; parity samples: {len(samples)}")
    return 0


def verify(args) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
    import numpy as np
    import onnxruntime as ort
    from rapport.emotion.model import RAW_TO_FIXED
    from rapport.emotion.tokenizer import BertTokenizer

    root = Path(args.dir)
    parity = json.loads((root / "parity.json").read_text(encoding="utf-8"))
    vocab_path = root / "vocab.txt"
    if not vocab_path.is_file():
        vocab_path = root / "raw" / "vocab.txt"
    tokenizer = BertTokenizer(vocab_path)
    max_len = int(parity.get("max_len", MAX_LEN))

    id_mismatch = []
    for sample in parity["samples"]:
        got = tokenizer.encode(sample["text"], max_len)
        if got != sample["ids"]:
            id_mismatch.append((sample["text"], sample["ids"], got))
    if id_mismatch:
        text, want, got = id_mismatch[0]
        print(f"TOKENIZER PARITY FAILED on {text!r}\n  reference: {want[:40]}\n  ours:      {got[:40]}")
        return 2

    session = ort.InferenceSession(str(root / "model.onnx"), providers=["CPUExecutionProvider"])
    worst = 0.0
    for sample in parity["samples"]:
        ids = np.array([sample["ids"]], dtype=np.int64)
        logits = session.run(
            ["logits"],
            {"input_ids": ids, "attention_mask": np.ones_like(ids), "token_type_ids": np.zeros_like(ids)},
        )[0]
        worst = max(worst, float(np.abs(np.asarray(logits) - np.array([sample["logits"]], dtype=np.float64)).max()))
    batch = parity["batch"]
    logits = session.run(
        ["logits"],
        {
            "input_ids": np.array(batch["ids"], dtype=np.int64),
            "attention_mask": np.array(batch["attention_mask"], dtype=np.int64),
            "token_type_ids": np.array(batch["token_type_ids"], dtype=np.int64),
        },
    )[0]
    worst = max(worst, float(np.abs(np.asarray(logits) - np.array(batch["logits"], dtype=np.float64)).max()))
    if worst > 1e-4:
        print(f"ONNX PARITY FAILED: max |logits diff| = {worst}")
        return 3
    print(f"parity ok: {len(parity['samples'])} samples + 1 batch, max |logits diff| = {worst:.2e}")

    config = json.loads((root / "raw" / "config.json").read_text(encoding="utf-8"))
    labels = [config["id2label"][str(i)] for i in range(len(config["id2label"]))]
    export_info = json.loads((root / "export-info.json").read_text(encoding="utf-8"))
    download_info = json.loads((root / "raw" / "download.json").read_text(encoding="utf-8"))
    manifest = {
        "id": MODEL_ID,
        "task": "single-sentence emotion classification (7 classes)",
        "source": SOURCE_URL,
        "license": "Apache License 2.0",
        "labels": labels,
        "raw_to_fixed": RAW_TO_FIXED,
        "max_len": max_len,
        "files": {
            "model.onnx": {"bytes": (root / "model.onnx").stat().st_size, "sha256": _sha256(root / "model.onnx")},
            "vocab.txt": {"bytes": (root / "vocab.txt").stat().st_size, "sha256": _sha256(root / "vocab.txt")},
        },
        "tokenizer": {"type": "bert-wordpiece-pure-python", "do_lower_case": True, "max_len": max_len,
                       "parity": {"samples": len(parity["samples"]), "ids_match": True}},
        "export": {**export_info, "onnx_max_logit_diff": worst},
        "upstream": download_info["files"],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    readme = root / "raw" / "README.md"
    if readme.is_file():
        (root / "SOURCE-README.md").write_text(readme.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
    print(f"manifest -> {root / 'manifest.json'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("download")
    d.add_argument("--out", required=True)
    d.add_argument("--skip-existing", action="store_true")
    d.set_defaults(fn=download)
    e = sub.add_parser("export")
    e.add_argument("--dir", required=True)
    e.set_defaults(fn=export)
    v = sub.add_parser("verify")
    v.add_argument("--dir", required=True)
    v.set_defaults(fn=verify)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
