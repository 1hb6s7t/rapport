"""GoldMixin: a small human-labelled set (人工金标) over the built dataset, for honest evaluation.

Every number the offline layer reports today is *agreement with AI-assisted labels*. This mixin
lets the user label a stratified sample of their own messages by hand, without seeing the AI
label first (no anchoring), and keeps the answers in app.db so ``tools/evaluate_grounded.py
--gold`` can score every method against a human reference.

Rules that keep the set useful:

- rows come from the validation/test splits only, so the kNN anchors (train split) never overlap
- the plan stores sample ids, never text; text is read from the encrypted snapshot on demand
- the AI label is written to the row *after* the human answered, for agreement statistics
- one plan per dataset; replacing it keeps already-stored labels (they are keyed by sample id)
"""

from __future__ import annotations

import random
from collections import Counter
from datetime import UTC, datetime

from rapport.db import GoldLabel, Setting
from rapport.errors import RapportError
from rapport.schemas.report_v1 import EMOTIONS, PRAGMATIC_TAGS

PLAN_KEY = "gold.plan"
GOLD_SPLITS = ("validation", "test")
MIN_PER_CLASS = 30
CONTEXT_MESSAGES = 6
MAX_TEXT_CHARS = 400
SIZE_RANGE = (50, 2000)


def _eligible(row: dict) -> bool:
    if row.get("split") not in GOLD_SPLITS or not row.get("training_eligible"):
        return False
    text = (row.get("text") or "").strip()
    labels = (row.get("labels") or {}).get("emotion") or []
    return len(text) >= 2 and len(labels) == 1 and labels[0] in EMOTIONS


def stratified_ids(rows: list[dict], size: int, seed: int) -> tuple[list[str], dict[str, int]]:
    """Per-weak-label quotas with a floor for minority classes, then random fill; shuffled."""
    rng = random.Random(seed)
    by_label: dict[str, list[str]] = {}
    for row in rows:
        if _eligible(row):
            by_label.setdefault(row["labels"]["emotion"][0], []).append(str(row["id"]))
    if not by_label:
        return [], {}
    for ids in by_label.values():
        rng.shuffle(ids)
    base = max(1, size // len(by_label))
    chosen: list[str] = []
    quotas: dict[str, int] = {}
    leftovers: list[str] = []
    for label, ids in sorted(by_label.items()):
        take = min(len(ids), max(MIN_PER_CLASS, base))
        chosen.extend(ids[:take])
        quotas[label] = take
        leftovers.extend(ids[take:])
    rng.shuffle(leftovers)
    for sample_id in leftovers[: max(0, size - len(chosen))]:
        chosen.append(sample_id)
    rng.shuffle(chosen)
    return chosen[:size] if len(chosen) > size else chosen, quotas


class GoldMixin:
    _gold_cache: dict[tuple[str, str], dict[str, dict]] = {}

    # --- data access ----------------------------------------------------------------------------

    def _gold_rows(self, dataset_id: str, pool: str) -> dict[str, dict]:
        key = (dataset_id, pool)
        if key not in self._gold_cache:
            from rapport.datasets import DatasetStore

            store = DatasetStore(self.settings.data_dir / "datasets", self.crypto)
            rows = store.records(dataset_id, pool)
            self._gold_cache.clear()  # one dataset in memory at a time
            self._gold_cache[key] = {str(r["id"]): r for r in rows}
        return self._gold_cache[key]

    def gold_plan(self, dataset_id: str) -> dict | None:
        with self.db.session() as s:
            row = s.get(Setting, PLAN_KEY)
            plan = dict(row.value) if row and isinstance(row.value, dict) else {}
        return plan if plan.get("dataset_id") == dataset_id else None

    def create_gold_plan(
        self, dataset_id: str, pool: str = "offline_review", size: int = 400, seed: int = 20261010, replace: bool = False
    ) -> dict:
        existing = self.gold_plan(dataset_id)
        if existing and not replace:
            raise RapportError("gold_plan_exists", "这个数据集已经有金标计划；要重抽请选择替换。", status=409)
        lo, hi = SIZE_RANGE
        size = max(lo, min(hi, int(size)))
        rows = self._gold_rows(dataset_id, pool)
        ids, quotas = stratified_ids(list(rows.values()), size, int(seed))
        if len(ids) < lo:
            raise RapportError("gold_plan_too_small", "可抽样的样本不足，无法建立金标计划。", status=422)
        plan = {
            "dataset_id": dataset_id,
            "pool": pool,
            "size": len(ids),
            "seed": int(seed),
            "quotas": quotas,
            "splits": list(GOLD_SPLITS),
            "sample_ids": ids,
            "created_at": datetime.now(UTC).isoformat(),
        }
        with self.db.session() as s:
            row = s.get(Setting, PLAN_KEY) or Setting(key=PLAN_KEY, value={})
            row.value = plan
            s.merge(row)
        return self._plan_view(plan)

    @staticmethod
    def _plan_view(plan: dict) -> dict:
        return {k: v for k, v in plan.items() if k != "sample_ids"}

    # --- labelling loop -------------------------------------------------------------------------

    def _gold_labels(self, dataset_id: str) -> dict[str, GoldLabel]:
        with self.db.session() as s:
            rows = s.query(GoldLabel).filter(GoldLabel.dataset_id == dataset_id).all()
        return {r.sample_id: r for r in rows}

    def gold_progress(self, dataset_id: str) -> dict:
        plan = self.gold_plan(dataset_id)
        labels = self._gold_labels(dataset_id)
        planned = plan["sample_ids"] if plan else []
        done = [labels[i] for i in planned if i in labels]
        labelled = [r for r in done if not r.skipped and r.emotion]
        with_weak = [r for r in labelled if r.weak_emotion]
        per_emotion = Counter(r.emotion for r in labelled)
        agree = Counter(r.weak_emotion for r in with_weak if r.weak_emotion == r.emotion)
        weak_counts = Counter(r.weak_emotion for r in with_weak)
        return {
            "has_plan": plan is not None,
            "plan": self._plan_view(plan) if plan else None,
            "total": len(planned),
            "labelled": len(labelled),
            "skipped": sum(1 for r in done if r.skipped),
            "remaining": len(planned) - len(done),
            "per_emotion": dict(per_emotion),
            "agreement_with_ai": round(sum(agree.values()) / len(with_weak), 4) if with_weak else None,
            "agreement_per_ai_label": {k: round(agree[k] / v, 4) for k, v in weak_counts.items()},
            "stored_labels_total": len(labels),
        }

    def gold_next(self, dataset_id: str) -> dict:
        plan = self.gold_plan(dataset_id)
        if not plan:
            raise RapportError("gold_plan_missing", "还没有金标计划，请先抽样。", status=404)
        labels = self._gold_labels(dataset_id)
        rows = self._gold_rows(dataset_id, plan["pool"])
        for position, sample_id in enumerate(plan["sample_ids"]):
            if sample_id in labels:
                continue
            row = rows.get(sample_id)
            if row is None:
                continue
            return {"done": False, "sample": self._sample_view(row, position), "progress": self.gold_progress(dataset_id)}
        return {"done": True, "sample": None, "progress": self.gold_progress(dataset_id)}

    @staticmethod
    def _sample_view(row: dict, position: int) -> dict:
        target_index = row.get("target_index")
        context = []
        for message in row.get("context") or []:
            index = message.get("index")
            if index is not None and target_index is not None and index >= target_index:
                continue
            text = (message.get("text") or "").strip()
            kind = message.get("type") or "text"
            if not text and kind == "text":
                continue
            context.append({
                "sender": "me" if message.get("sender") == "me" else "other",
                "text": text[:MAX_TEXT_CHARS] if text else f"[{kind}]",
                "ts": message.get("ts"),
            })
        return {
            "sample_id": str(row["id"]),
            "position": position,
            "text": (row.get("text") or "")[:MAX_TEXT_CHARS],
            "context": context[-CONTEXT_MESSAGES:],
            "split": row.get("split"),
            "emotions": list(EMOTIONS),
            "intents": list(PRAGMATIC_TAGS),
        }

    def gold_label(
        self, dataset_id: str, sample_id: str, emotion: str | None, intent: str | None, skipped: bool = False, note: str = ""
    ) -> dict:
        plan = self.gold_plan(dataset_id)
        if not plan:
            raise RapportError("gold_plan_missing", "还没有金标计划，请先抽样。", status=404)
        if sample_id not in set(plan["sample_ids"]):
            raise RapportError("gold_sample_unknown", "这条样本不在金标计划里。", status=422)
        if not skipped and emotion not in EMOTIONS:
            raise RapportError("gold_bad_emotion", "请选择固定词表中的情绪标签。", status=422)
        if intent is not None and intent not in PRAGMATIC_TAGS:
            raise RapportError("gold_bad_intent", "请选择固定词表中的语用标签。", status=422)
        row = self._gold_rows(dataset_id, plan["pool"]).get(sample_id) or {}
        weak = ((row.get("labels") or {}).get("emotion") or [""])[0]
        with self.db.session() as s:
            key = f"{dataset_id}:{sample_id}"
            record = s.get(GoldLabel, key) or GoldLabel(id=key, dataset_id=dataset_id, sample_id=sample_id, pool=plan["pool"])
            record.emotion = None if skipped else emotion
            record.intent = intent
            record.skipped = bool(skipped)
            record.note = (note or "")[:500]
            record.weak_emotion = weak
            record.split = str(row.get("split") or "")
            record.updated_at = datetime.now()
            s.merge(record)
        return self.gold_progress(dataset_id)

    def gold_undo(self, dataset_id: str) -> dict:
        with self.db.session() as s:
            last = (
                s.query(GoldLabel)
                .filter(GoldLabel.dataset_id == dataset_id)
                .order_by(GoldLabel.updated_at.desc(), GoldLabel.created_at.desc())
                .first()
            )
            if last is not None:
                s.delete(last)
        return self.gold_progress(dataset_id)

    def gold_export(self, dataset_id: str) -> list[dict]:
        return [
            {
                "sample_id": r.sample_id,
                "emotion": r.emotion,
                "intent": r.intent,
                "skipped": r.skipped,
                "weak_emotion": r.weak_emotion,
                "split": r.split,
                "note": r.note,
            }
            for r in sorted(self._gold_labels(dataset_id).values(), key=lambda r: r.created_at)
        ]
