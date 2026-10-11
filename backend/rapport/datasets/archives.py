"""Incremental samples from explicitly authorised, confirmed local chat archives.

Archive identity/revocation bindings stay inside encrypted snapshots. No archive is
uploaded, no draft is treated as an observed reply, and old snapshots are immutable.
"""

from __future__ import annotations

import copy
import re
import sqlite3
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from rapport.archives import get_service
from rapport.datasets.corpus import (
    POOLS,
    REPLY_BUBBLE_GAP_S,
    REPLY_MAX_BUBBLES,
    VERSION,
    WARNINGS,
    digest,
    encode,
    informative_reply,
    validate_splits,
)
from rapport.datasets.credentials import redact_text
from rapport.datasets.store import DatasetStore
from rapport.errors import RapportError

ARCHIVE_VERSION = "confirmed-archive-replies-v6"
ORDERED_PRECISIONS = {"second", "minute", "label"}
OBSERVED_SOURCES = {"wechat_json", "wechat_csv", "ocr"}
GAP = timedelta(hours=6)
DRAFT = re.compile(r"\[(?:AI\s*草稿|未确认草稿|未发送草稿|生成候选)\]", re.I)


def _sid(relationship_id: str) -> str:
    return "a_" + digest(("archive-relationship-v1:" + relationship_id).encode())[:24]


def _dt(value: str | None, timezone: str = "Asia/Shanghai") -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            result = result.replace(tzinfo=ZoneInfo(timezone))
        return result.astimezone(UTC)
    except (ValueError, ZoneInfoNotFoundError):
        return None


def _ref(row: dict) -> str:
    return digest(encode({k: row[k] for k in ("sender", "text", "type", "ts")}))


def _clean_archive(archive: dict, records: list[dict], counts: Counter) -> list[dict] | None:
    if archive.get("source") not in OBSERVED_SOURCES:
        counts["unobserved_source_archives"] += 1
        return None
    result = []
    previous = None
    ordinal = 0
    for original in records:
        ts = _dt(original.get("ts"), archive.get("timezone") or "Asia/Shanghai")
        if ts is None and not result and original.get("ts_precision") == "none":
            # Lines above the first visible time label of a screen capture: context we cannot date.
            counts["undated_leading_messages"] += 1
            continue
        if ts is None or original.get("ts_precision") not in ORDERED_PRECISIONS:
            # Unknown-time lines cannot safely be positioned between overlapping exports.
            counts["unreliable_time_archives"] += 1
            return None
        if previous is not None and ts < previous:
            counts["non_monotonic_archives"] += 1
            return None
        # Bubbles under one WeChat time label share its time; their order inside the label
        # is part of their identity, so a repeated short reply is not mistaken for a duplicate.
        ordinal = ordinal + 1 if previous is not None and ts == previous and original["ts_precision"] == "label" else 0
        previous = ts
        text, text_redacted = redact_text(original.get("text") or "")
        quoted, quoted_redacted = redact_text(original.get("quoted_text") or "")
        if text_redacted or quoted_redacted:
            counts["credential_redactions"] += 1
        row = {"sender": original.get("sender", "unknown"), "text": text,
               "type": original.get("type", "unknown"), "ts": original["ts"],
               "ts_precision": original["ts_precision"], "quoted_text": quoted,
               "credential_redacted": text_redacted or quoted_redacted,
               "epoch": ts.timestamp(), "archive_ids": [archive["id"]]}
        row["content_ref"] = _ref(row) if row["ts_precision"] != "label" else _ref(row | {"ts": f"{row['ts']}#{ordinal}"})
        result.append(row)
    refs = [m["content_ref"] for m in result]
    if len(set(refs)) != len(refs):
        # Especially for minute-granularity OCR, identical repeated lines may be real.
        counts["ambiguous_repetition_archives"] += 1
        return None
    return result


def _merge(stream: list[dict], incoming: list[dict], counts: Counter) -> list[dict]:
    if not stream:
        return incoming
    if not incoming:
        return stream
    known = {m["content_ref"]: m for m in stream}
    overlap = [m for m in incoming if m["content_ref"] in known]
    fresh = [m for m in incoming if m["content_ref"] not in known]
    if not fresh:
        counts["duplicate_messages"] += len(incoming)
        for row in overlap:
            known[row["content_ref"]]["archive_ids"] = sorted(set(known[row["content_ref"]]["archive_ids"] + row["archive_ids"]))
        return stream
    windows_overlap = max(stream[0]["epoch"], incoming[0]["epoch"]) <= min(stream[-1]["epoch"], incoming[-1]["epoch"])
    if windows_overlap:
        old_overlap = [m["content_ref"] for m in stream if m["content_ref"] in {r["content_ref"] for r in overlap}]
        new_overlap = [m["content_ref"] for m in overlap]
        # Require observable order anchors, and never guess placement within equal times.
        occupied = {m["epoch"] for m in stream}
        if len(overlap) < 3 or old_overlap != new_overlap or any(m["epoch"] in occupied for m in fresh):
            counts["ambiguous_overlap_archives"] += 1
            return stream
    counts["duplicate_messages"] += len(overlap)
    for row in overlap:
        known[row["content_ref"]]["archive_ids"] = sorted(set(known[row["content_ref"]]["archive_ids"] + row["archive_ids"]))
    return sorted(stream + fresh, key=lambda m: m["epoch"])


def _segments(messages: list[dict]) -> list[list[dict]]:
    groups: list[list[dict]] = []
    for message in messages:
        if not groups or message["epoch"] - groups[-1][-1]["epoch"] > GAP.total_seconds():
            groups.append([])
        groups[-1].append(message)
    return groups


def _observable(message: dict) -> bool:
    return (message["sender"] in {"me", "other"} and message["type"] == "text"
            and bool(message["text"].strip()) and not message["quoted_text"]
            and not message.get("credential_redacted")
            and not DRAFT.search(message["text"]) and len(message["text"]) <= 2000)


def _base_binding(messages: list[dict], base_rows: list[dict], explicit: str | None) -> str | None:
    known_sessions = {r["session_id"] for r in base_rows}
    if explicit:
        if explicit not in known_sessions:
            raise RapportError("dataset_archive_binding_invalid", "映射的匿名好友不属于所选基础数据集。", status=422)
        return explicit
    anchors: dict[tuple[str, ...], set[str]] = defaultdict(set)
    for row in base_rows:
        context = row.get("context", [])
        for start in range(max(0, len(context) - 2)):
            triple = context[start:start + 3]
            if all(m.get("content_ref") for m in triple):
                anchors[tuple(m["content_ref"] for m in triple)].add(row["session_id"])
    matches = set()
    for start in range(max(0, len(messages) - 2)):
        triple = messages[start:start + 3]
        if {r["sender"] for r in triple} != {"me", "other"}:
            continue
        common = anchors.get(tuple(r["content_ref"] for r in triple), set())
        if len(common) == 1:
            matches.update(common)
    return next(iter(matches)) if len(matches) == 1 else None


def _public_message(message: dict, sid: str) -> dict:
    return {"id": f"{sid}:{message['content_ref'][:24]}", "sender": message["sender"],
            "text": message["text"], "type": message["type"], "ts": message["ts"],
            "ts_precision": message["ts_precision"], "content_ref": message["content_ref"]}


def _counts(pools: dict) -> dict:
    return {"pools": {p: len(rows) for p, rows in pools.items()},
            "splits": {p: dict(Counter(r["split"] for r in rows)) for p, rows in pools.items()},
            "time_splits": {p: dict(Counter(r.get("time_split", r["split"]) for r in rows)) for p, rows in pools.items()},
            "eligible": {p: sum(bool(r.get("training_eligible")) for r in rows) for p, rows in pools.items()}}


def _reply_rules():
    from rapport.llm.reply import first_person_claims, unbacked_specifics

    return first_person_claims, unbacked_specifics


def append_authorized_archives(state, *, relationship_ids: list[str], base_dataset_id: str | None = None,
                               archive_ids: list[str] | None = None, now: datetime | None = None,
                               relationship_session_map: dict[str, str] | None = None) -> dict:
    """Create an immutable delta snapshot from ONLY the supplied authorised scope.

    ``delta.new_eligible`` is the scheduling threshold input. ``included_source_refs``
    is public/anonymous. Call ``learning_source_bindings`` for private revocation IDs.
    """
    if not relationship_ids:
        raise RapportError("dataset_archive_scope_required", "请先授权要更新的好友范围。", status=422)
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise RapportError("dataset_archive_time_invalid", "更新时刻需要时区。", status=422)
    now = now.astimezone(UTC)
    closed_before = now - GAP
    store = DatasetStore(state.settings.data_dir / "datasets", state.crypto)
    archives = get_service(state)
    first_person_claims, unbacked_specifics = _reply_rules()
    selected_rids = sorted({state.resolve_relationship_id(rid) for rid in relationship_ids})
    if set(relationship_session_map or {}) - set(selected_rids):
        raise RapportError("dataset_archive_scope_invalid", "好友映射包含未授权对象。", status=422)
    current = store.get(base_dataset_id) if base_dataset_id else None
    if current and current["status"] != "built":
        raise RapportError("dataset_not_built", "增量更新需要已构建数据集。", status=409)
    if current and current.get("version") != VERSION:
        raise RapportError("dataset_policy_stale", "旧快照使用过期的数据保护规则，请先重新构建。", status=409)
    if current:
        learning_source_bindings(store, base_dataset_id)
    private = store._load(base_dataset_id) if current else {"pools": {p: [] for p in POOLS}}
    old_private = copy.deepcopy(private)
    pools = private["pools"]
    base_rows = [row for rows in pools.values() for row in rows]
    old_refs = {ref for r in base_rows for ref in r.get("content_refs", [])}
    old_ids = {r["id"] for r in base_rows}
    binding_store = private.setdefault("archive_bindings", {})
    policy = private.setdefault("archive_policy", {"version": ARCHIVE_VERSION, "first_cutoff": now.isoformat()})
    first_cutoff = _dt(policy["first_cutoff"])
    counters = Counter()
    pending = []
    included = set(current.get("included_source_refs", []) if current else [])
    archive_provenance = {p["source_id"]: p for p in (current or {}).get("provenance", []) if p.get("kind") == "confirmed_archive"}
    requested = set(archive_ids) if archive_ids is not None else None
    found_requested = set()
    has_legacy_sources = bool(current and any(p.get("kind") == "redacted" for p in current.get("provenance", [])))
    prospective_manifest = list(current.get("prospective_holdouts", []) if current else [])
    prospective_ids = {p["conversation_id"] for p in prospective_manifest}
    for rid in selected_rids:
        sid = _sid(rid)
        stream: list[dict] = []
        allowed = [a for a in archives.list(rid) if requested is None or a["id"] in requested]
        for archive in sorted(allowed, key=lambda a: (a["created_at"], a["id"])):
            found_requested.add(archive["id"])
            archive_provenance["archive:" + archive["id"]] = {"kind": "confirmed_archive", "source_id": "archive:" + archive["id"],
                                                            "sha256": archive["content_hash"], "version": archive["schema_version"]}
            cleaned = _clean_archive(archive, archives.messages(archive["id"], rid), counters)
            counters["inspected_archives"] += 1
            if cleaned is not None:
                stream = _merge(stream, cleaned, counters)
        legacy_sid = binding_store.get(sid, {}).get("legacy_session_id")
        explicit = (relationship_session_map or {}).get(rid)
        if has_legacy_sources and not legacy_sid:
            legacy_sid = _base_binding(stream, base_rows, explicit)
        if explicit and legacy_sid and explicit != legacy_sid:
            raise RapportError("dataset_archive_binding_changed", "该好友已有固定映射，请创建新分支复核，不能静默改写。", status=409)
        old_binding = binding_store.get(sid, {})
        binding_store[sid] = {"relationship_id": rid, "legacy_session_id": legacy_sid,
                              "archive_ids": sorted(set(old_binding.get("archive_ids", [])) | {a["id"] for a in allowed})}
        legacy_splits = {r["split"] for r in base_rows if legacy_sid and r["session_id"] == legacy_sid}
        if len(legacy_splits) > 1:
            raise RapportError("dataset_archive_legacy_split_ambiguous", "旧好友材料已跨不同分区，请先复核其划分协议。", status=409)
        frozen_friend_split = next(iter(legacy_splits)) if legacy_splits else None
        for segment in _segments(stream):
            if segment[-1]["epoch"] > closed_before.timestamp():
                counters["open_conversations"] += 1
                continue
            conversation_id = "ac_" + digest((sid + segment[0]["content_ref"]).encode())[:24]
            bucket = int(digest(conversation_id.encode())[:8], 16) % 100
            split = frozen_friend_split or ("train" if bucket < 70 else "validation" if bucket < 85 else "test")
            future = bool(first_cutoff and segment[0]["epoch"] > first_cutoff.timestamp())
            prospective = future and (bucket >= 85 or split == "test")
            if prospective:
                split = "test"
            # Historical segments with any existing source stay in the original split;
            # newly inferred adjoining context is quarantined instead of leaking.
            segment_old = bool(old_refs.intersection(m["content_ref"] for m in segment))
            context: list[dict] = []
            segment_rows = []
            for index, message in enumerate(segment):
                if not _observable(message):
                    context = []
                    counters["context_barriers"] += 1
                    continue
                public = _public_message(message, sid)
                if message["sender"] == "me" and context and context[-1]["sender"] == "other":
                    # Same target rules as the base dataset (personal-dataset-v7): our whole turn,
                    # i.e. the reply plus our own follow-up bubbles, and nothing asserting our own
                    # experiences or numbers the chat never said is fitted.
                    bubbles, last = [public], message["epoch"]
                    for following in segment[index + 1:index + REPLY_MAX_BUBBLES]:
                        if (following["sender"] != "me" or not _observable(following)
                                or not 0 <= following["epoch"] - last <= REPLY_BUBBLE_GAP_S):
                            break
                        bubbles.append(_public_message(following, sid))
                        last = following["epoch"]
                    response = "\n".join(b["text"].strip() for b in bubbles)
                    unbacked = bool(first_person_claims(response))
                    specifics = bool(unbacked_specifics(response, "\n".join(m["text"] for m in context[-12:])))
                    refs = [m["content_ref"] for m in context] + [b["content_ref"] for b in bubbles]
                    row = {"id": public["id"], "session_id": sid, "conversation_id": conversation_id,
                           "split": split, "time_split": split, "context": context[-12:], "text": response,
                           "response": response, "bubbles": len(bubbles), "target_ts": public["ts"], "target_index": None,
                           "source_ids": [m["id"] for m in context[-12:]] + [b["id"] for b in bubbles],
                           "content_refs": refs, "quality": "observed_self_reply", "human_confirmed": False,
                           "training_eligible": not prospective and informative_reply(response) and not unbacked and not specifics,
                           "unbacked_first_person": unbacked, "unbacked_specifics": specifics,
                           "evaluation_eligible": True,
                           "post_hoc": False, "from_archive": True, "prospective_holdout": prospective,
                           "provenance": {"version": ARCHIVE_VERSION, "observed_in_chat": True}}
                    if row["id"] in old_ids or public["content_ref"] in old_refs:
                        counters["existing_targets"] += 1
                    elif has_legacy_sources and not legacy_sid:
                        pending.append({"row": row, "reason": "friend_binding_required"})
                        counters["pending_binding"] += 1
                    elif segment_old:
                        pending.append({"row": row, "reason": "overlapping_existing_conversation"})
                        counters["pending_overlap"] += 1
                    else:
                        segment_rows.append(row)
                context.append(public)
                context = context[-12:]
            for row in segment_rows:
                pools["personal_reply"].append(row)
                old_ids.add(row["id"])
                counters["new_samples"] += 1
                counters["new_eligible"] += bool(row["training_eligible"] and row["split"] == "train")
                counters["new_validation"] += row["split"] == "validation"
                counters["new_holdout"] += row["split"] == "test"
                counters["new_forward_holdout"] += row["prospective_holdout"]
            if segment_rows:
                archive_refs = {"archive:" + aid for m in segment for aid in m["archive_ids"]}
                included.update(archive_refs)
                private.setdefault("sample_archive_sources", {}).update({r["id"]: sorted(archive_refs | {"relationship:" + rid}) for r in segment_rows})
            if prospective and segment_rows and conversation_id not in prospective_ids:
                prospective_manifest.append({"conversation_id": conversation_id, "reserved_at": now.isoformat(),
                                             "sample_ids": [r["id"] for r in segment_rows], "never_train": True})
                prospective_ids.add(conversation_id)
    if requested is not None and found_requested != requested:
        raise RapportError("dataset_archive_not_authorized", "部分归档不属于本次授权好友范围。", status=404)
    private["pending_archive_reply"] = pending
    # Existing encrypted bindings allow the worker to register revocation sources
    # without exposing real relationship identifiers in JSONL exports.
    public_delta = {key: int(counters[key]) for key in (
        "new_samples", "new_eligible", "new_validation", "new_holdout", "new_forward_holdout",
        "pending_binding", "pending_overlap", "open_conversations", "duplicate_messages",
        "inspected_archives", "unobserved_source_archives", "unreliable_time_archives",
        "non_monotonic_archives", "ambiguous_repetition_archives", "ambiguous_overlap_archives",
        "context_barriers", "credential_redactions", "existing_targets")}
    if current and encode(old_private) == encode(private):
        return {**current, "delta": public_delta, "unchanged": True}
    leakage = validate_splits(pools)
    manifest = {**(current or {"version": VERSION, "counts": {}, "quality": {"annotation_kind": "observed_archive", "human_gold": False},
                              "provenance": [], "warnings": WARNINGS, "label_counts": {}}),
                "id": uuid.uuid4().hex, "status": "built", "created_at": now.isoformat(),
                "parent_id": base_dataset_id, "archive_adapter": ARCHIVE_VERSION,
                "provenance": [p for p in (current or {}).get("provenance", []) if p.get("kind") != "confirmed_archive"]
                              + [archive_provenance[ref] for ref in sorted(included) if ref in archive_provenance],
                "included_source_refs": sorted(included), "delta": public_delta,
                "prospective_holdouts": prospective_manifest,
                "split_policy": {"method": "frozen-base-plus-archive-conversation-v1", "source_overlap": leakage["source_overlap"],
                                 "friend_overlap": "expected_within_friend_for_archive_time_updates",
                                 "independent_blind_test": False, "forward_test_never_train": True}, **_counts(pools)}
    return store._save(manifest, private)


def learning_source_bindings(store: DatasetStore, dataset_id: str, sample_ids: list[str] | None = None, *, verify_present: bool = True) -> list[str]:
    """Private worker-only revocation IDs; never put this list in dataset exports."""
    private = store._load(dataset_id)
    bindings = private.get("sample_archive_sources", {})
    selected = set(sample_ids) if sample_ids is not None else set(bindings)
    refs = sorted({ref for sample, sources in bindings.items() if sample in selected for ref in sources})
    if verify_present and refs:
        # Read only this running application's archive index, never the original corpus.
        expected = {aid: entry["relationship_id"] for entry in private.get("archive_bindings", {}).values() for aid in entry["archive_ids"]}
        required = {ref.removeprefix("archive:") for ref in refs if ref.startswith("archive:")}
        index = store.root.parent / "archive" / "index.sqlite3"
        if not index.is_file() or index.is_symlink() or not index.resolve().is_relative_to(store.root.parent):
            raise RapportError("dataset_archive_source_revoked", "增量样本的长期归档来源已删除或不可用。", status=409)
        connection = sqlite3.connect(index.as_uri() + "?mode=ro", uri=True)
        try:
            present = {aid for aid in required if connection.execute("SELECT 1 FROM archives WHERE id=? AND relationship_id=?", (aid, expected.get(aid))).fetchone()}
        finally:
            connection.close()
        if present != required:
            raise RapportError("dataset_archive_source_revoked", "增量样本的长期归档来源已撤销，不能继续训练。", status=409)
    return refs
