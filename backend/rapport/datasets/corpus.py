"""Read only explicitly allowed corpus files; never load aliases, secrets or databases."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from rapport.datasets.credentials import redact_text
from rapport.errors import RapportError
from rapport.parsers import ParseOptions, parse
from rapport.schemas.message_v1 import Sender

VERSION = "personal-dataset-v8"
# A reply target is the first self message after the other person spoke, plus our
# own immediate follow-up bubbles (same turn, each within REPLY_BUBBLE_GAP_S).
REPLY_MAX_BUBBLES = 3
REPLY_BUBBLE_GAP_S = 180
POOLS = ("offline_review", "realtime_review", "personal_reply")
MAX_FILE_BYTES = 200 * 1024 * 1024
WARNINGS = [
    "标注是 AI 辅助标签，较高质量层不代表人工金标准。",
    "未引用后文不证明标注者未看过后文；实时标签仍需因果复核。",
    "本批资料已用于开发评测，测试划分仅作回归，不能声称独立盲测。",
    "这里只构建数据，不执行模型训练、不启用模型、不向外部发送内容。",
    "媒体记录只标记类型；本数据集不包含媒体原文件。",
]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encode(data: object) -> bytes:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _read(path: Path, root: Path) -> tuple[dict, bytes]:
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise RapportError("dataset_source_link", "数据源不能通过链接指向其他目录。", status=400)
    if path.stat().st_size > MAX_FILE_BYTES:
        raise RapportError("dataset_source_large", "单份数据文件超过 200 MiB。", status=413)
    raw = path.read_bytes()
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeError) as exc:
        raise RapportError("dataset_source_invalid", "数据源包含无法解析的 JSON。", status=422) from exc
    if not isinstance(data, dict):
        raise RapportError("dataset_source_invalid", "数据源不是预期的对象结构。", status=422)
    return data, raw


def _safe_files(root: Path, pattern: str) -> list[Path]:
    return sorted(p for p in root.glob(pattern) if p.is_file() and not p.name.startswith("_"))


def _message(m, source_id: str, index: int) -> dict:
    cleaned, redacted = redact_text(m.text or "")
    result = {
        "id": f"{source_id}:{index}", "index": index, "sender": m.sender.value,
        "text": cleaned, "credential_redacted": redacted, "type": m.type.value,
        "ts": m.ts.isoformat() if m.ts else None,
        "ts_precision": m.ts_precision.value, "from_voice": bool(m.meta.get("from_voice")),
    }
    result["content_ref"] = digest(encode({k: result[k] for k in ("sender", "text", "type", "ts")}))
    return result


def _annotation_body(raw: dict) -> str:
    markers = {"图片消息": "[图片]", "动画表情": "[动画表情]", "视频消息": "[视频]", "通话消息": "[通话]"}
    return markers.get(raw.get("type"), str(raw.get("content") or "").strip() or "[空]")


class Mapper:
    """Use the exact current product map, independent of stale case snapshots."""

    def __init__(self):
        path = Path(__file__).with_name("label_map_v2.json")
        if not path.exists():
            path = Path(__file__).resolve().parents[3] / "docs" / "label_map_v2.json"
        self.raw = path.read_bytes()
        self.data = json.loads(self.raw)

    def labels(self, annotation: dict) -> tuple[dict, bool]:
        result = {}
        unmapped = False
        for field, dest, mapping in (("pragmatic_tags", "intent", "pragmatic"), ("emotions", "emotion", "emotion")):
            values = []
            for label in annotation.get(field) or []:
                table = self.data[mapping]
                if label in table:
                    value = table[label]
                elif mapping == "pragmatic":
                    matches = [v for prefix, v in self.data.get("pragmatic_prefix_rules", []) if prefix in label]
                    if not matches:
                        unmapped = True
                        continue
                    value = matches[0]
                else:
                    unmapped = True
                    continue
                if value is not None and value not in values:
                    values.append(value)
            result[dest] = values or (["none"] if not unmapped else [])
        positive, negative = {"开心", "亲近"}, {"不满", "伤心", "担忧"}
        emotions = result["emotion"]
        if positive.intersection(emotions) and negative.intersection(emotions):
            result["emotion"] = ["混合"] + [e for e in emotions if e not in positive | negative]
        return result, unmapped


def load_corpus(source_root: Path) -> dict:
    root = source_root.expanduser().resolve()
    if not root.is_dir() or not (root / "redacted").is_dir() or not (root / "annotations").is_dir():
        raise RapportError("dataset_source_missing", "需要包含 redacted 和 annotations 的本地 eval 目录。", status=404)
    mapper = Mapper()
    counts = Counter()
    sources, sessions, annotations = [], [], []
    fingerprints: set[str] = set()
    for path in _safe_files(root / "redacted", "*.json"):
        data, raw = _read(path, root)
        if "session" not in data or not isinstance(data.get("messages"), list):
            continue
        content_hash = digest(raw)
        if content_hash in fingerprints:
            counts["duplicate_sources"] += 1
            continue
        fingerprints.add(content_hash)
        sid = "s_" + content_hash[:20]
        try:
            conv, _ = parse(raw.decode("utf-8-sig"), sid, "wx_session_json", None, ParseOptions())
        except Exception as exc:
            raise RapportError("dataset_parse_failed", "脱敏聊天解析失败，请检查单聊导出格式。", status=422) from exc
        if len(conv.messages) != len(data["messages"]):
            raise RapportError("dataset_index_mismatch", "解析后的序号与源文件不一致，已停止以免错配标注。", status=422)
        msgs = [_message(m, sid, i) for i, m in enumerate(conv.messages, 1)]
        sessions.append({"id": sid, "messages": msgs})
        sources.append({"kind": "redacted", "source_id": sid, "sha256": content_hash, "version": conv.parser_version})
        counts["sessions"] += 1
        counts["messages"] += len(msgs)
        counts["personal_text"] += sum(m.sender == Sender.me and m.is_text for m in conv.messages)
        counts["credential_redactions"] += sum(bool(m["credential_redacted"]) for m in msgs)
        counts["media_messages"] += sum(m["type"] in {"image", "video", "sticker", "voice", "file"} for m in msgs)
        counts["reliable_timestamps"] += sum(m.has_reliable_ts for m in conv.messages)
        seen: dict[int, dict] = {}
        ann_dir = root / "annotations" / path.stem
        for batch_path in _safe_files(ann_dir, "batch*.json"):
            batch, batch_raw = _read(batch_path, root)
            ver = batch.get("semantic_review_version") or batch.get("annotation_version") or "unknown"
            sources.append({"kind": "annotation", "source_id": sid, "sha256": digest(batch_raw), "version": ver})
            counts["annotation_batches"] += 1
            if ver != "context-v4":
                counts["non_current_batches"] += 1
            for ann in batch.get("messages") or []:
                idx = ann.get("index")
                if ann.get("speaker") != "other" or not isinstance(idx, int) or isinstance(idx, bool):
                    continue
                if idx in seen:
                    if encode({k: v for k, v in seen[idx].items() if not k.startswith("_")}) != encode(ann):
                        counts["conflicting_duplicate_annotations"] += 1
                        seen[idx]["_duplicate_conflict"] = True
                    else:
                        counts["duplicate_annotations"] += 1
                    continue
                seen[idx] = dict(ann, _version=ver)
        # Compare content only, avoiding the internal _version field in duplicate checks.
        for idx, ann in sorted(seen.items()):
            counts["annotations"] += 1
            if not 1 <= idx <= len(msgs) or msgs[idx - 1]["sender"] != "other":
                counts["orphan_annotations"] += 1
                continue
            message = msgs[idx - 1]
            expected_text = _annotation_body(data["messages"][idx - 1])
            ann_text = str(ann.get("text") or "").strip()
            text_match = bool(ann_text) and (expected_text == ann_text or expected_text.startswith(ann_text))
            labels, unmapped = mapper.labels(ann)
            evidence = (ann.get("annotation_basis") or {}).get("evidence_indices") or []
            evidence_indices = sorted({i for i in evidence if isinstance(i, int) and not isinstance(i, bool)})
            invalid_evidence = any(i < 1 or i > len(msgs) for i in evidence_indices)
            future = any(i > idx for i in evidence_indices)
            review = bool(ann.get("needs_review"))
            media = bool(ann.get("media_unknown")) or message["type"] != "text"
            bad = review or media or unmapped or not text_match or invalid_evidence or bool(ann.get("_duplicate_conflict")) or ann["_version"] != "context-v4"
            counts["joined_annotations"] += 1
            counts["needs_review"] += review
            counts["future_evidence"] += future
            counts["media_unknown"] += media
            counts["unmapped"] += unmapped
            counts["text_mismatch"] += not text_match
            counts["invalid_evidence"] += invalid_evidence
            counts["weak" if bad else "ai_assisted_high"] += 1
            annotations.append({
                "id": message["id"], "session_id": sid, "target_index": idx,
                "labels": labels, "evidence_indices": evidence_indices,
                "quality": "weak" if bad else "ai_assisted_high", "post_hoc": future,
                "human_confirmed": False, "causal_review_required": True,
                "needs_review": review, "media_unknown": media,
                "training_eligible": not bad, "annotation_version": ann["_version"],
                "boundary": ann.get("boundary") or "none",
            })
    if not sessions:
        raise RapportError("dataset_empty", "没有找到可用的脱敏单聊 JSON。", status=422)
    # v2 is provenance/reference only. Never treat it or v1 as an independent split.
    cases_path = root / "gold" / "cases-v2.json"
    if cases_path.is_file():
        mapped, mapped_raw = _read(cases_path, root)
        sources.append({"kind": "reference_cases_v2", "source_id": "mapped-v2", "sha256": digest(mapped_raw), "version": str(mapped.get("schema", "v2"))})
        counts["reference_cases_v2"] = len(mapped.get("cases") or [])
        counts["reference_quality"] = sum(c.get("stratum") == "gold" for c in mapped.get("cases") or [])
        counts["reference_weak"] = sum(c.get("stratum") == "weak" for c in mapped.get("cases") or [])
    sources.append({"kind": "label_map", "source_id": "product-map", "sha256": digest(mapper.raw), "version": mapper.data["version"]})
    return {"counts": dict(counts), "provenance": sources, "sessions": sessions, "annotations": annotations}


def _time(ts: str | None) -> float | None:
    try:
        return datetime.fromisoformat(ts).timestamp() if ts else None
    except ValueError:
        return None


def reply_split(row: dict) -> str | None:
    """Personal reply models learn each friend's earlier chats and are tested on later ones.

    ``time_split`` is chronological within a friend over whole 6-hour segments; rows on a
    segment boundary are ``excluded_boundary`` and belong to no partition.
    """
    return row.get("time_split") or row.get("split")


def _continues(previous: dict, current: dict) -> bool:
    a, b = _time(previous["ts"]), _time(current["ts"])
    return a is not None and b is not None and 0 <= b - a <= REPLY_BUBBLE_GAP_S


def informative_reply(text: str) -> bool:
    """Reserve short acknowledgements/placeholders for evaluation, not LoRA fit."""
    value = str(text or "").strip()
    return (6 <= len(value) <= 160
            and not re.fullmatch(r"(?:\[[^\]]+\]\s*)+", value)
            and not value.startswith(("[链接]", "http://", "https://")))


def _reply_context(context: list[dict], *, allow_media_prefix: bool = False) -> list[dict] | None:
    """Optional augmentation keeps only known text after older media.

    The entire original context must be credential-checked before calling this.
    A mixed-media incoming turn cannot be made answerable by dropping its media.
    The default remains text-only; expansion callers must review new targets.
    """
    if any(c["sender"] not in {"me", "other"} for c in context):
        return None
    nontext = [i for i, c in enumerate(context) if c["type"] != "text"]
    if not nontext:
        return context
    if not allow_media_prefix or any(context[i]["type"] not in {"image", "video", "sticker", "voice", "file"} for i in nontext):
        return None
    for c in reversed(context):
        if c["sender"] != "other":
            break
        if c["type"] != "text":
            return None
    return context[nontext[-1] + 1:]


def build_records(corpus: dict, *, allow_media_prefix: bool = False) -> tuple[dict, dict]:
    sessions = corpus["sessions"]
    for session in sessions:
        for message in session["messages"]:
            message.setdefault("content_ref", digest(encode({k: message[k] for k in ("sender", "text", "type", "ts")})))
    # Friend-level isolation across ALL pools. Time orders friends deterministically;
    # complete 6-hour conversation segments never cross split boundaries.
    parents = {s["id"]: s["id"] for s in sessions}
    def leader(sid: str) -> str:
        while parents[sid] != sid:
            sid = parents[sid]
        return sid
    anchors = {}
    for session in sessions:
        for message in session["messages"]:
            # Overlapping exports with changed metadata still belong to one split.
            if not message["ts"]:
                continue
            anchor = message["content_ref"]
            previous = anchors.setdefault(anchor, session["id"])
            a, b = leader(previous), leader(session["id"])
            if a != b:
                parents[max(a, b)] = min(a, b)
    groups: dict[str, list] = {}
    for session in sessions:
        groups.setdefault(leader(session["id"]), []).append(session)
    ordered = sorted(groups, key=lambda g: (min((m["ts"] for s in groups[g] for m in s["messages"] if m["ts"]), default=""), g))
    n = len(ordered)
    n_train = max(1, int(n * .7)) if n >= 3 else n
    n_validation = max(1, int(n * .15)) if n >= 3 else 0
    assignments = {s["id"]: ("train" if i < n_train else "validation" if i < n_train + n_validation else "test") for i, g in enumerate(ordered) for s in groups[g]}
    ann_by_session: dict[str, list] = {}
    for ann in corpus["annotations"]:
        ann_by_session.setdefault(ann["session_id"], []).append(ann)
    from rapport.llm.reply import first_person_claims, unbacked_specifics

    pools = {p: [] for p in POOLS}
    reply_counts = Counter()
    seen_targets: set[tuple[str, ...]] = set()
    for session in sessions:
        sid, messages = session["id"], session["messages"]
        by_index = {m["index"]: m for m in messages}
        group_of, group_start, previous, group = {}, {}, None, 0
        for m in messages:
            ts = _time(m["ts"])
            if previous is None or ts is None or ts - previous > 6 * 3600 or ts < previous:
                group += 1
                group_start[group] = m["index"]
            group_of[m["index"]] = group
            previous = ts
        # An alternate within-friend chronological protocol. Whole conversation
        # segments move together. Never mix this protocol with friend holdout.
        group_sizes = Counter(group_of.values())
        chronological = {}
        elapsed = 0
        for number in sorted(group_sizes):
            fraction = elapsed / max(len(messages), 1)
            chronological[number] = "train" if fraction < .7 else "validation" if fraction < .85 else "test"
            elapsed += group_sizes[number]
        def context_for(index: int, group_start=group_start, group_of=group_of, by_index=by_index) -> list[dict]:
            start = max(group_start[group_of[index]], index - 12)
            return [by_index[i] for i in range(start, index)]
        for ann in ann_by_session.get(sid, []):
            idx = ann["target_index"]
            context = context_for(idx)
            evidence = [by_index[i] for i in ann["evidence_indices"] if i in by_index]
            row = dict(ann, context=context, text=by_index[idx]["text"],
                       conversation_id=f"{sid}:c{group_of[idx]}", split=assignments[sid],
                       source_ids=sorted({m["id"] for m in context + evidence} | {ann["id"]}),
                       content_refs=sorted({m["content_ref"] for m in context + evidence + [by_index[idx]]}),
                       evidence=evidence)
            if any(m.get("credential_redacted") for m in context + evidence + [by_index[idx]]):
                row.update(training_eligible=False, quality="weak", credential_redacted=True)
            pools["offline_review"].append(row)
            if not ann["post_hoc"] and ann["quality"] == "ai_assisted_high":
                pools["realtime_review"].append(dict(row, training_eligible=False, evidence=[m for m in evidence if m["index"] <= idx]))
        for m in messages:
            if m["sender"] != "me" or m["type"] != "text" or not m["text"].strip():
                continue
            context = context_for(m["index"])
            if m.get("credential_redacted") or any(c.get("credential_redacted") for c in context):
                continue
            # A reply target must be the first observable self message after the
            # other person spoke. Consecutive self messages are continuations of
            # our own turn, not replies to a fresh incoming message.
            if not context or context[-1]["sender"] != "other":
                continue
            context = _reply_context(context, allow_media_prefix=allow_media_prefix)
            if not context:
                continue
            if len(m["text"]) > 2000 or re.search(r"\[(?:AI草稿|未确认草稿)\]", m["text"]):
                continue
            bubbles = [m]
            for following in range(m["index"] + 1, m["index"] + REPLY_MAX_BUBBLES):
                nxt = by_index.get(following)
                if (not nxt or nxt["sender"] != "me" or nxt["type"] != "text" or not nxt["text"].strip()
                        or nxt.get("credential_redacted") or len(nxt["text"]) > 2000
                        or group_of[following] != group_of[m["index"]] or not _continues(bubbles[-1], nxt)):
                    break
                bubbles.append(nxt)
            target = tuple(b["content_ref"] for b in bubbles)
            if target in seen_targets:
                # The same turn seen again through an overlapping export version.
                reply_counts["duplicate_targets"] += 1
                continue
            seen_targets.add(target)
            response = "\n".join(b["text"].strip() for b in bubbles)
            # The model must not learn to assert what we did or experienced: at reply time
            # such claims have no authorised backing and are withheld anyway.
            unbacked = bool(first_person_claims(response))
            # Numbers and dates the chat never mentioned are private knowledge of ours; learning to
            # produce them teaches the model to invent them.
            specifics = bool(unbacked_specifics(response, "\n".join(c["text"] for c in context)))
            eligible = informative_reply(response) and not unbacked and not specifics
            reply_counts["targets"] += 1
            reply_counts["multi_bubble_targets"] += len(bubbles) > 1
            reply_counts["unbacked_first_person_targets"] += unbacked
            reply_counts["unbacked_specific_targets"] += specifics
            reply_counts["training_eligible"] += eligible
            pools["personal_reply"].append({
                "id": m["id"], "session_id": sid, "conversation_id": f"{sid}:c{group_of[m['index']]}",
                "target_index": m["index"], "split": assignments[sid], "context": context,
                "text": response, "response": response, "bubbles": len(bubbles), "quality": "observed_self_reply",
                "training_eligible": eligible, "evaluation_eligible": True,
                "unbacked_first_person": unbacked, "unbacked_specifics": specifics, "human_confirmed": False,
                "source_ids": [c["id"] for c in context] + [b["id"] for b in bubbles],
                "content_refs": [c["content_ref"] for c in context] + list(target),
                "post_hoc": False, "from_voice": any(b["from_voice"] for b in bubbles),
            })
        for rows in pools.values():
            for row in rows:
                if row["session_id"] != sid:
                    continue
                expected = chronological[group_of[row["target_index"]]]
                ref_indices = [int(ref.rsplit(":", 1)[1]) for ref in row["source_ids"]]
                row["time_split"] = expected if all(chronological[group_of[i]] == expected for i in ref_indices) else "excluded_boundary"
    leakage = validate_splits(pools)
    # Remove every conflicting chronological row across overlapping export versions.
    seen_time: dict[str, set] = {}
    for rows in pools.values():
        for row in rows:
            if row["time_split"] != "excluded_boundary":
                for ref in row["content_refs"]:
                    seen_time.setdefault(ref, set()).add(row["time_split"])
    conflicting_time = {ref for ref, splits in seen_time.items() if len(splits) > 1}
    for rows in pools.values():
        for row in rows:
            if conflicting_time.intersection(row["content_refs"]):
                row["time_split"] = "excluded_boundary"
    time_leakage = validate_splits(pools, "time_split")
    return pools, {"method": "friend_group_then_time_v1", "assignments": assignments, "leakage": leakage,
                   "alternate_protocol": "time_split: chronological within each friend, complete 6-hour sessions; never mix protocols",
                   "time_leakage": time_leakage,
                   "personal_reply_protocol": {"split": "time_split", "max_bubbles": REPLY_MAX_BUBBLES,
                                               "context_policy": "text_after_older_media_v1" if allow_media_prefix else "text_only",
                                               "bubble_gap_s": REPLY_BUBBLE_GAP_S, "counts": dict(reply_counts)}}


def validate_splits(pools: dict, split_key: str = "split") -> dict:
    seen: dict[str, str] = {}
    conflicts = set()
    for rows in pools.values():
        for row in rows:
            split = row.get(split_key)
            if split == "excluded_boundary":
                continue
            for source_id in row["source_ids"] + row.get("content_refs", []):
                if source_id in seen and seen[source_id] != split:
                    conflicts.add(source_id)
                seen[source_id] = split
    if conflicts:
        raise RapportError("dataset_split_leakage", "检测到跨集合的重复来源，已阻止构建。", status=409)
    return {"source_overlap": 0, "friend_overlap": 0 if split_key == "split" else "expected_within_friend", "checked_sources": len(seen), "independent_blind_test": False}
