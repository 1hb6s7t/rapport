"""Immutable encrypted corpus snapshots, independent from temporary import retention."""

from __future__ import annotations

import json
import os
import re
import threading
import time as time  # backwards-compatible patch point for export expiry checks
import uuid
import zlib
from collections import Counter
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path

from rapport.datasets.corpus import (
    POOLS,
    VERSION,
    WARNINGS,
    build_records,
    digest,
    encode,
    informative_reply,
    load_corpus,
)
from rapport.errors import RapportError
from rapport.security import Crypto

_MUTATION_LOCK = threading.RLock()


def _serialized(fn):
    @wraps(fn)
    def call(*args, **kwargs):
        with _MUTATION_LOCK:
            return fn(*args, **kwargs)
    return call


def default_source() -> Path | None:
    """The read-only eval directory named by ``RAPPORT_EVAL_SOURCE``; never a built-in guess."""
    value = os.environ.get("RAPPORT_EVAL_SOURCE", "").strip()
    return Path(value).expanduser() if value else None


def _source_or_default(source_root: str | Path | None) -> Path:
    source = Path(source_root) if source_root else default_source()
    if source is None:
        raise RapportError("dataset_source_required", "请填写只读的 eval 资料目录，或设置 RAPPORT_EVAL_SOURCE。", status=422)
    return source.expanduser().resolve()


class DatasetStore:
    def __init__(self, root: Path, crypto: Crypto):
        self.root = Path(root).resolve()
        self.crypto = crypto
        self._generation = self._read_generation()

    def _read_generation(self):
        path = self.root / "generation"
        return path.read_text(encoding="ascii") if path.exists() else "initial"

    @_serialized
    def wipe_all(self, before_delete=None):
        """Delete only validated app-owned snapshot files; source material is untouched."""
        if not self.root.exists():
            return {"deleted": 0}
        for protected in {Path("D:/.rapport").resolve(), *([default_source().resolve()] if default_source() else [])}:
            if self.root == protected or self.root.is_relative_to(protected) or protected.is_relative_to(self.root):
                raise RapportError("dataset_protected_source", "不能清理只读资料目录。", status=409)
        paths = []
        folders = []
        for folder in self.root.iterdir():
            if folder.name == "generation" and folder.is_file() and not folder.is_symlink():
                continue
            if folder.is_symlink() or folder.is_junction() or not folder.is_dir() or not re.fullmatch(r"[a-f0-9]{32}", folder.name):
                raise RapportError("dataset_cleanup_path", "数据集目录包含非快照内容，清理已停止。", status=409)
            if not folder.resolve().is_relative_to(self.root):
                raise RapportError("dataset_cleanup_path", "数据集清理路径无效。", status=409)
            for path in folder.iterdir():
                if path.is_symlink() or not path.is_file() or path.name not in {"manifest.json", "snapshot.bin"}:
                    raise RapportError("dataset_cleanup_path", "快照目录包含未知内容，清理已停止。", status=409)
                paths.append(path)
            folders.append(folder)
        if before_delete:
            for folder in folders:
                before_delete(folder.name)
        (self.root / "generation").write_text(uuid.uuid4().hex, encoding="ascii")
        for path in paths:
            path.unlink(missing_ok=True)
        for folder in folders:
            folder.rmdir()
        return {"deleted": len(folders)}

    def _path(self, dataset_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", dataset_id):
            raise RapportError("dataset_not_found", "数据集不存在。", status=404)
        return self.root / dataset_id

    @_serialized
    def _save(self, manifest: dict, private: dict) -> dict:
        if self._generation != self._read_generation():
            raise RapportError("dataset_reset", "资料已清空，本次旧任务不再保存。", status=409)
        folder = self._path(manifest["id"])
        folder.mkdir(parents=True, exist_ok=False)
        # Manifest is committed last; incomplete snapshots never appear in the list.
        payload = encode(private)
        manifest["snapshot_sha256"] = digest(payload)
        manifest["storage"] = "zlib+fernet"
        (folder / "snapshot.bin").write_bytes(self.crypto.encrypt(zlib.compress(payload, level=6)))
        (folder / "manifest.json").write_bytes(encode(manifest))
        return manifest

    def _load(self, dataset_id: str) -> dict:
        manifest = self.get(dataset_id)
        try:
            data = self.crypto.decrypt((self._path(dataset_id) / "snapshot.bin").read_bytes())
            if manifest.get("storage") == "zlib+fernet":
                data = zlib.decompress(data)
            if digest(data) != manifest["snapshot_sha256"]:
                raise ValueError("snapshot hash")
            return json.loads(data)
        except (OSError, ValueError, zlib.error) as exc:
            raise RapportError("dataset_corrupt", "快照校验失败，未继续读取。", status=409) from exc

    def list(self) -> list[dict]:
        result = []
        if self.root.is_dir():
            for path in self.root.glob("*/manifest.json"):
                if not re.fullmatch(r"[a-f0-9]{32}", path.parent.name):
                    continue
                try:
                    result.append(json.loads(path.read_bytes()))
                except (OSError, ValueError):
                    continue
        return sorted(result, key=lambda m: m["created_at"], reverse=True)

    def get(self, dataset_id: str) -> dict:
        try:
            return json.loads((self._path(dataset_id) / "manifest.json").read_bytes())
        except (OSError, ValueError) as exc:
            raise RapportError("dataset_not_found", "数据集不存在。", status=404) from exc

    def audit(self, source_root: str | Path | None = None) -> dict:
        source = _source_or_default(source_root)
        protected = source.parent if source.parent.name == ".rapport" else source
        if self.root.is_relative_to(protected) or protected.is_relative_to(self.root):
            raise RapportError("dataset_output_overlaps_source", "输出目录与只读资料目录重叠，请使用独立的应用数据目录。", status=409)
        corpus = load_corpus(source)
        manifest = {
            "id": uuid.uuid4().hex, "status": "audited", "version": VERSION,
            "created_at": datetime.now(UTC).isoformat(), "counts": corpus["counts"],
            "provenance": corpus["provenance"], "warnings": WARNINGS,
            "quality": {"annotation_kind": "ai_assisted", "human_gold": False,
                        "authoritative": "annotations/context-v4", "cases_v2_usage": "reference_only"},
            "pools": {}, "splits": {},
        }
        return self._save(manifest, {"source_root": str(source), "corpus": corpus})

    def build(self, source_root: str | Path | None = None, *, audit_id: str | None = None) -> dict:
        if audit_id is None:
            audit_id = self.audit(source_root)["id"]
        audit = self.get(audit_id)
        if audit["status"] != "audited":
            raise RapportError("dataset_not_audit", "请选择审核快照进行构建。", status=409)
        if audit.get("version") != VERSION:
            raise RapportError("dataset_policy_stale", "审核快照使用旧的数据保护规则，请重新审核来源。", status=409)
        private = self._load(audit_id)
        pools, splitting = build_records(private["corpus"])
        manifest = {
            **{k: audit[k] for k in ("version", "counts", "provenance", "warnings", "quality")},
            "id": uuid.uuid4().hex, "audit_id": audit_id, "status": "built",
            "created_at": datetime.now(UTC).isoformat(),
            "pools": {k: len(rows) for k, rows in pools.items()},
            "splits": {k: {name: sum(r["split"] == name for r in rows) for name in ("train", "validation", "test")} for k, rows in pools.items()},
            "time_splits": {k: dict(Counter(r["time_split"] for r in rows)) for k, rows in pools.items()},
            "split_policy": splitting,
            "eligible": {k: sum(r["training_eligible"] for r in rows) for k, rows in pools.items()},
            "label_counts": {kind: dict(Counter(label for row in pools["offline_review"] for label in row["labels"][kind])) for kind in ("emotion", "intent")},
        }
        return self._save(manifest, {"pools": pools})

    def records(self, dataset_id: str, pool: str) -> list[dict]:
        if pool not in POOLS:
            raise RapportError("dataset_bad_pool", "未知的数据集用途。", status=422)
        if self.get(dataset_id)["status"] != "built":
            raise RapportError("dataset_not_built", "请先构建数据集。", status=409)
        return self._load(dataset_id)["pools"][pool]

    def refresh(self, dataset_id: str) -> dict:
        """Re-read the previously audited source; unchanged input creates no new snapshot."""
        current = self.get(dataset_id)
        audit_id = current.get("audit_id")
        if not audit_id:
            return current
        approved = self._load(audit_id)
        source = Path(approved["source_root"]).resolve()
        if current.get("version") != VERSION:
            return self.build(source)
        protected = source.parent if source.parent.name == ".rapport" else source
        if self.root.is_relative_to(protected) or protected.is_relative_to(self.root):
            raise RapportError("dataset_output_overlaps_source", "应用资料目录与只读来源重叠。", status=409)
        corpus = load_corpus(source)
        if corpus["provenance"] == current["provenance"]:
            return current
        audit = self.get(audit_id)
        updated = {**audit, "id": uuid.uuid4().hex, "created_at": datetime.now(UTC).isoformat(),
                   "counts": corpus["counts"], "provenance": corpus["provenance"]}
        audited = self._save(updated, {"source_root": str(source), "corpus": corpus})
        return self.build(audit_id=audited["id"])

    def preview(self, dataset_id: str, pool: str, page: int = 0, page_size: int = 20) -> dict:
        rows = self.records(dataset_id, pool)
        return {"items": rows[page * page_size:(page + 1) * page_size], "total": len(rows),
                "pool": pool, "page": page, "page_size": page_size}

    def review(self, dataset_id: str, pool: str, sample_id: str, decision: str, note: str = "") -> dict:
        if pool not in POOLS or decision not in {"accepted", "excluded"}:
            raise RapportError("dataset_bad_review", "无效的复核用途或决定。", status=422)
        manifest = self.get(dataset_id)
        if manifest["status"] != "built":
            raise RapportError("dataset_not_built", "请先构建数据集。", status=409)
        if manifest.get("version") != VERSION:
            raise RapportError("dataset_policy_stale", "该数据集使用旧的数据保护规则，请先生成新快照。", status=409)
        private = self._load(dataset_id)
        row = next((r for r in private["pools"][pool] if r["id"] == sample_id), None)
        if row is None:
            raise RapportError("dataset_sample_missing", "样本不存在。", status=404)
        if decision == "accepted" and row.get("prospective_holdout"):
            raise RapportError("dataset_holdout_frozen", "前瞻测试样本已冻结，复核不能将它改为训练样本。", status=409)
        if decision == "accepted" and pool == "personal_reply" and not informative_reply(row.get("response", "")):
            raise RapportError("dataset_reply_not_informative", "过短或仅为占位符的回复不进入个人表达训练。", status=409)
        if decision == "accepted" and (row.get("media_unknown") or (pool == "realtime_review" and row.get("post_hoc"))):
            raise RapportError("dataset_review_unobservable", "该样本缺少可观察内容，不能直接确认训练。", status=409)
        if decision == "accepted" and pool != "personal_reply" and any(not row.get("labels", {}).get(kind) for kind in ("emotion", "intent")):
            raise RapportError("dataset_review_unmapped", "样本存在尚未映射的标签，请先修订标签规范，不能直接作为已确认样本。", status=409)
        now = datetime.now(UTC).isoformat()
        row["human_confirmed"] = decision == "accepted"
        row["training_eligible"] = decision == "accepted"
        row["review"] = {"decision": decision, "note": note, "at": now, "scope": pool}
        if pool == "realtime_review" and decision == "accepted":
            row["causal_review_required"] = False
        updated = {**manifest, "id": uuid.uuid4().hex, "parent_id": dataset_id, "created_at": now,
                   "eligible": {k: sum(r["training_eligible"] for r in rows) for k, rows in private["pools"].items()},
                   "human_reviewed": {k: sum(bool(r.get("human_confirmed")) for r in rows) for k, rows in private["pools"].items()}}
        return self._save(updated, private)

    def export_preview(self, dataset_id: str, pools: list[str], **kwargs) -> dict:
        from rapport.datasets.exports import preview

        if self.get(dataset_id).get("version") != VERSION:
            raise RapportError("dataset_policy_stale", "旧快照不能作为新训练包导出，请重新构建。", status=409)
        return preview(self, dataset_id, pools, **kwargs)

    def export(self, dataset_id: str, token: str, confirmed: bool, **kwargs) -> bytes:
        from rapport.datasets.exports import export

        if self.get(dataset_id).get("version") != VERSION:
            raise RapportError("dataset_policy_stale", "旧快照不能作为新训练包导出，请重新构建。", status=409)
        return export(self, dataset_id, token, confirmed, **kwargs)
