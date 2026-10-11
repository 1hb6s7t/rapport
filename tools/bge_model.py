"""Export the local BGE-small-zh-v1.5 encoder to ONNX for the rules-mode research layer.

Two steps, each in the venv that can do it (no download: the weights are already local):

  <train venv>\\python.exe tools/bge_model.py export --source <bge dir> --out models/bge-small-zh-v1.5
  <backend venv>\\python.exe tools/bge_model.py verify --dir models/bge-small-zh-v1.5 --source <bge dir>

`verify` writes manifest.json only after the pure-Python tokenizer reproduces the reference
tokenizer's ids and ONNX CLS vectors match torch within 1e-4. Runtime needs onnxruntime only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

MODEL_ID = "BAAI/bge-small-zh-v1.5"
REVISION = "7999e1d3359715c523056ef9478215996d62a620"
SOURCE_URL = "https://huggingface.co/BAAI/bge-small-zh-v1.5"
LICENSE = "MIT"
MAX_TOKENS = 96  # the research runs encoded 96 tokens; keep inference identical

SAMPLES = [
    "今天真的好开心呀，谢谢你陪我！",
    "你怎么又这样，我真的受够了。",
    "想你了",
    "https://platform.worldquantbrain.com/learn/documentation/understanding",
    "记得这个因子的提交标准是什么？",
    "❗进群先改群昵称 Brain_你的userid_大学简称 iqc学习群",
    "不行价格太低了，这个至少一千",
    "可以呀",
    "没事，我习惯了。",
    "接下来根据你之前所学习到的所有内容，来完成一下任务:",
    "OK, see you tomorrow at 8pm.",
    "长文本" * 60,
]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def export(args) -> int:
    import torch
    from transformers import AutoModel, AutoTokenizer

    source, out = Path(args.source), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    tokenizer = AutoTokenizer.from_pretrained(str(source), local_files_only=True, trust_remote_code=False)
    model = AutoModel.from_pretrained(str(source), local_files_only=True, trust_remote_code=False).eval()
    for parameter in model.parameters():
        parameter.requires_grad = False
    print("hidden size:", model.config.hidden_size)

    class Cls(torch.nn.Module):
        def __init__(self, inner):
            super().__init__()
            self.inner = inner

        def forward(self, input_ids, attention_mask, token_type_ids):
            state = self.inner(input_ids=input_ids, attention_mask=attention_mask,
                               token_type_ids=token_type_ids).last_hidden_state
            return torch.nn.functional.normalize(state[:, 0], p=2, dim=-1)

    wrapped = Cls(model).eval()
    samples = []
    with torch.no_grad():
        for text in SAMPLES:
            encoded = tokenizer(text, truncation=True, max_length=MAX_TOKENS)
            ids = encoded["input_ids"]
            tensor = torch.tensor([ids], dtype=torch.long)
            vector = wrapped(tensor, torch.ones_like(tensor), torch.zeros_like(tensor))
            samples.append({"text": text, "ids": ids, "vector": [round(x, 6) for x in vector[0].tolist()]})
        batch = tokenizer(SAMPLES[:4], padding=True, truncation=True, max_length=MAX_TOKENS, return_tensors="pt")
        batch_vector = wrapped(**batch)

    onnx_path = out / "model.onnx"
    dummy = torch.tensor([samples[0]["ids"]], dtype=torch.long)
    torch.onnx.export(
        wrapped,
        (dummy, torch.ones_like(dummy), torch.zeros_like(dummy)),
        str(onnx_path),
        input_names=["input_ids", "attention_mask", "token_type_ids"],
        output_names=["embedding"],
        dynamic_axes={"input_ids": {0: "batch", 1: "seq"}, "attention_mask": {0: "batch", 1: "seq"},
                      "token_type_ids": {0: "batch", 1: "seq"}, "embedding": {0: "batch"}},
        opset_version=14,
        do_constant_folding=True,
    )
    (out / "vocab.txt").write_bytes((source / "vocab.txt").read_bytes())
    (out / "parity.json").write_text(json.dumps({
        "max_tokens": MAX_TOKENS,
        "samples": samples,
        "batch": {"texts": SAMPLES[:4], "ids": batch["input_ids"].tolist(),
                  "attention_mask": batch["attention_mask"].tolist(),
                  "token_type_ids": batch["token_type_ids"].tolist(),
                  "vector": [[round(x, 6) for x in row] for row in batch_vector.tolist()]},
    }, ensure_ascii=False), encoding="utf-8")
    (out / "export-info.json").write_text(json.dumps({
        "torch": torch.__version__, "transformers": __import__("transformers").__version__, "opset": 14,
        "hidden_size": int(model.config.hidden_size), "pooling": "l2_cls", "created_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2), encoding="utf-8")
    print(f"model.onnx: {onnx_path.stat().st_size} bytes; parity samples: {len(samples)}")
    return 0


def verify(args) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
    import numpy as np
    import onnxruntime as ort
    from rapport.emotion.tokenizer import BertTokenizer

    root, source = Path(args.dir), Path(args.source)
    parity = json.loads((root / "parity.json").read_text(encoding="utf-8"))
    tokenizer = BertTokenizer(root / "vocab.txt", do_lower_case=False)
    for sample in parity["samples"]:
        got = tokenizer.encode(sample["text"], parity["max_tokens"])
        if got != sample["ids"]:
            print(f"TOKENIZER PARITY FAILED on {sample['text'][:24]!r}\n  ref: {sample['ids'][:32]}\n  our: {got[:32]}")
            return 2

    session = ort.InferenceSession(str(root / "model.onnx"), providers=["CPUExecutionProvider"])
    worst = 0.0
    for sample in parity["samples"]:
        ids = np.array([sample["ids"]], dtype=np.int64)
        vector = session.run(["embedding"], {"input_ids": ids, "attention_mask": np.ones_like(ids),
                                            "token_type_ids": np.zeros_like(ids)})[0]
        worst = max(worst, float(np.abs(np.asarray(vector) - np.array([sample["vector"]], dtype=np.float64)).max()))
    batch = parity["batch"]
    vector = session.run(["embedding"], {"input_ids": np.array(batch["ids"], dtype=np.int64),
                                         "attention_mask": np.array(batch["attention_mask"], dtype=np.int64),
                                         "token_type_ids": np.array(batch["token_type_ids"], dtype=np.int64)})[0]
    worst = max(worst, float(np.abs(np.asarray(vector) - np.array(batch["vector"], dtype=np.float64)).max()))
    if worst > 1e-4:
        print(f"ONNX PARITY FAILED: max |vector diff| = {worst}")
        return 3
    print(f"parity ok: {len(parity['samples'])} samples + 1 batch, max |vector diff| = {worst:.2e}")

    export_info = json.loads((root / "export-info.json").read_text(encoding="utf-8"))
    manifest = {
        "id": MODEL_ID, "task": "sentence embedding (l2-normalized CLS)", "revision": REVISION,
        "source": SOURCE_URL, "license": LICENSE, "max_tokens": parity["max_tokens"],
        "hidden_size": export_info["hidden_size"], "pooling": "l2_cls",
        "files": {"model.onnx": {"bytes": (root / "model.onnx").stat().st_size, "sha256": _sha256(root / "model.onnx")},
                  "vocab.txt": {"bytes": (root / "vocab.txt").stat().st_size, "sha256": _sha256(root / "vocab.txt")}},
        "upstream_weights": {"model.safetensors": {"bytes": (source / "model.safetensors").stat().st_size,
                                                  "sha256": _sha256(source / "model.safetensors")}},
        "tokenizer": {"type": "bert-wordpiece-pure-python", "do_lower_case": False,
                      "parity": {"samples": len(parity["samples"]), "ids_match": True}},
        "export": {**export_info, "onnx_max_vector_diff": worst},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"manifest -> {root / 'manifest.json'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("--source", required=True)
    e.add_argument("--out", required=True)
    e.set_defaults(fn=export)
    v = sub.add_parser("verify")
    v.add_argument("--dir", required=True)
    v.add_argument("--source", required=True)
    v.set_defaults(fn=verify)
    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
