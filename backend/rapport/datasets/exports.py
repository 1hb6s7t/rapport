"""Exact-selection local training exports. Never transports data to a destination.

The token seals the exact ordered sample keys, the immutable snapshot, the export
bytes digest, intended environment and browser owner. Export projections are
allowlisted rather than copying private rows/manifest metadata.
"""

from __future__ import annotations

import io
import json
import re
import time
import zipfile
import zlib
from collections import Counter
from datetime import UTC, datetime

from rapport.datasets.corpus import POOLS, WARNINGS, digest, encode, validate_splits
from rapport.errors import RapportError

SPLITS = ("train", "validation", "test")
MAX_SELECTION = 100_000
MAX_TOKEN_BYTES = 8 * 1024 * 1024


def _alias(dataset_id, kind, value):
    return kind + "_" + digest(encode([dataset_id, kind, str(value)]))[:24]


def _friend(row):
    # The private archive adapter's relationship binding must never be exported.
    return row.get("_relationship_id") or row.get("relationship_id") or row.get("friend_id") or row.get("session_id")


def _identities(dataset_id, row):
    return {"id": _alias(dataset_id, "m", row["id"]),
            "session_id": _alias(dataset_id, "s", row.get("session_id", "")),
            "friend_id": _alias(dataset_id, "f", _friend(row)),
            "conversation_id": _alias(dataset_id, "c", row.get("conversation_id", row.get("session_id", "")))}


def _project(dataset_id, row, pool):
    result = {**_identities(dataset_id, row), "pool": pool}
    for key in ("target_index", "split", "time_split", "text", "response", "quality", "post_hoc", "human_confirmed", "training_eligible", "causal_review_required", "needs_review", "media_unknown", "from_voice", "annotation_version"):
        if key in row:
            result[key] = row[key]
    if isinstance(row.get("labels"), dict):
        result["labels"] = {key: row["labels"][key] for key in ("emotion", "intent") if key in row["labels"]}
    for field in ("context", "evidence"):
        if field in row:
            result[field] = []
            for message in row[field]:
                safe = {key: message[key] for key in ("index", "sender", "text", "type", "ts", "ts_precision", "from_voice") if key in message}
                if "id" in message:
                    safe["id"] = _alias(dataset_id, "m", message["id"])
                result[field].append(safe)
    result["source_ids"] = [_alias(dataset_id, "m", key) for key in row.get("source_ids", [])]
    # Scoped opaque content refs retain duplicate detection without exposing the
    # application's original content hashes.
    result["content_refs"] = [_alias(dataset_id, "r", key) for key in row.get("content_refs", [])]
    if "evidence_indices" in row:
        result["evidence_indices"] = row["evidence_indices"]
    return result


def _built(store, dataset_id):
    manifest = store.get(dataset_id)
    if manifest["status"] != "built":
        raise RapportError("dataset_not_built", "请先构建数据集。", status=409)
    return manifest, store._load(dataset_id)["pools"]


def options(store, dataset_id):
    _, pools = _built(store, dataset_id)
    groups = {key: {} for key in ("sessions", "friends", "conversations")}
    for pool, rows in pools.items():
        for row in rows:
            identities = _identities(dataset_id, row)
            for dest, field, label in (("sessions", "session_id", "会话"), ("friends", "friend_id", "对象"), ("conversations", "conversation_id", "会话段")):
                collection = groups[dest]
                key = identities[field]
                if key not in collection:
                    collection[key] = {"id": key, "label": f"{label} {len(collection) + 1}", "counts": {}, "eligible_counts": {}}
                    if dest == "conversations":
                        collection[key]["session_id"] = identities["session_id"]
                group = collection[key]
                group["counts"][pool] = group["counts"].get(pool, 0) + 1
                group["eligible_counts"][pool] = group["eligible_counts"].get(pool, 0) + int(bool(row.get("training_eligible")))
    return {**{key: list(value.values()) for key, value in groups.items()}, "splits": list(SPLITS), "pools": list(POOLS), "max_samples": MAX_SELECTION}


def _selection(dataset_id, data, pools, selectors, split_policy=None):
    pools = sorted(set(pools))
    if not pools or any(pool not in POOLS for pool in pools):
        raise RapportError("dataset_bad_pool", "请选择有效的数据集用途。", status=422)
    allowed = {"splits", "session_ids", "friend_ids", "conversation_ids", "sample_ids", "eligible_only", "max_samples"}
    if set(selectors) - allowed:
        raise RapportError("dataset_export_selector", "未知导出筛选条件。", status=422)
    filters = {key: list(dict.fromkeys(selectors.get(key) or [])) for key in ("splits", "session_ids", "friend_ids", "conversation_ids", "sample_ids")}
    if any(split not in SPLITS for split in filters["splits"]):
        raise RapportError("dataset_export_selector", "无效的数据划分。", status=422)
    limit = selectors.get("max_samples")
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_SELECTION):
        raise RapportError("dataset_export_selector", "样本上限必须在 1 至 100000 之间。", status=422)
    known = {field: set() for field in ("session_ids", "friend_ids", "conversation_ids", "sample_ids")}
    found = []
    for pool in pools:
        for row in data[pool]:
            ids = _identities(dataset_id, row)
            for field, singular in (("session_ids", "session_id"), ("friend_ids", "friend_id"), ("conversation_ids", "conversation_id")):
                known[field].add(ids[singular])
            known["sample_ids"].update((row["id"], ids["id"]))
            if filters["splits"] and row.get("split") not in filters["splits"]:
                continue
            if any(filters[key] and ids[field] not in filters[key] for key, field in (("session_ids", "session_id"), ("friend_ids", "friend_id"), ("conversation_ids", "conversation_id"))):
                continue
            if filters["sample_ids"] and row["id"] not in filters["sample_ids"] and ids["id"] not in filters["sample_ids"]:
                continue
            if selectors.get("eligible_only") and not row.get("training_eligible"):
                continue
            found.append((pool, row))
    for key, values in known.items():
        if set(filters[key]) - values:
            raise RapportError("dataset_export_selector", "筛选项不属于所选数据用途，请重新选择。", status=422)
    # Never reassign splits to satisfy a request; verify source/content overlap
    # before capping so a cap cannot accidentally hide a broken snapshot.
    selected = {pool: [row for selected_pool, row in found if selected_pool == pool] for pool in pools}
    validate_splits(selected)
    seen_groups = {}
    time_protocol = (split_policy or {}).get("friend_overlap") == "expected_within_friend_for_archive_time_updates"
    for _, row in found:
        for kind, value in (("friend", _friend(row)), ("session", row.get("session_id")), ("conversation", row.get("conversation_id"))):
            if time_protocol and kind in {"friend", "session"}:
                continue
            if value is None:
                continue
            key = (kind, value)
            if key in seen_groups and seen_groups[key] != row["split"]:
                raise RapportError("dataset_split_leakage", "检测到对象或会话跨集合，导出已停止。", status=409)
            seen_groups[key] = row["split"]
    found.sort(key=lambda item: (pools.index(item[0]), SPLITS.index(item[1]["split"]), item[1].get("session_id", ""), item[1].get("target_index") or 0, item[1]["id"]))
    if limit is not None:
        found = found[:limit]
    if len(found) > MAX_SELECTION:
        raise RapportError("dataset_export_too_large", "请缩小到最多 100000 份样本。", status=413)
    if not found:
        raise RapportError("dataset_export_empty", "筛选后没有样本，请调整用途、划分或已复核条件。", status=422)
    normalized = {**filters, "eligible_only": bool(selectors.get("eligible_only", False)), "max_samples": limit}
    return pools, found, normalized


def _rows_digest(rows):
    return digest(b"\n".join(encode(row) for row in rows))


def _manifest(dataset_id, manifest, rows, pools, selectors, target_environment):
    return {"schema": "dataset-selection-v1", "dataset_id": dataset_id, "version": manifest.get("version"), "snapshot_sha256": manifest["snapshot_sha256"], "pools": pools,
            "counts": {pool: sum(row["pool"] == pool for row in rows) for pool in pools},
            "splits": {pool: dict(Counter(row["split"] for row in rows if row["pool"] == pool)) for pool in pools},
            "total": len(rows), "selection_sha256": _rows_digest(rows), "selectors": selectors,
            "target_environment": target_environment, "local_download_only": True,
            "files": ["manifest.json"] + [f"{pool}/{split}.jsonl" for pool in pools for split in (selectors["splits"] or SPLITS)],
            "warnings": WARNINGS + ["此包只下载到本机；填写目标环境不构成外发授权。请复核脱敏原文，导出不保证识别文字中所有个人信息。"],
            "exported_pools": pools}


def preview(store, dataset_id, pools, *, selectors=None, target_environment="local", owner="", page=0, page_size=20):
    target_environment = target_environment.strip()
    if not target_environment or len(target_environment) > 200 or re.search(r"[\x00-\x1f\x7f]", target_environment):
        raise RapportError("dataset_export_environment", "请填写有效的目标环境名称。", status=422)
    manifest, data = _built(store, dataset_id)
    pools, found, selectors = _selection(dataset_id, data, pools, selectors or {}, manifest.get("split_policy"))
    rows = [_project(dataset_id, row, pool) for pool, row in found]
    # Selector IDs are themselves exportable aliases, even if legacy callers
    # selected a raw internal sample ID from the ordinary dataset preview.
    if selectors["sample_ids"]:
        selectors["sample_ids"] = list(dict.fromkeys(row["id"] for row in rows))
    export_manifest = _manifest(dataset_id, manifest, rows, pools, selectors, target_environment)
    expires = int(time.time()) + 600
    claims = {"kind": "dataset_export_selection_v1", "id": dataset_id, "snapshot_sha256": manifest["snapshot_sha256"], "expires": expires, "owner": owner,
              "target_environment": target_environment, "keys": [[pool, row["id"]] for pool, row in found], "manifest": export_manifest}
    token = store.crypto.encrypt(zlib.compress(encode(claims), 6)).decode()
    return {"token": token, "manifest": export_manifest, "selectors": selectors, "target_environment": target_environment,
            "items": rows[page * page_size : (page + 1) * page_size], "total": len(rows), "page": page, "page_size": page_size,
            "expires_at": datetime.fromtimestamp(expires, UTC).isoformat(), "requires_confirmation": True}


def _resolve(store, dataset_id, token, owner):
    try:
        if len(token) > MAX_TOKEN_BYTES:
            raise ValueError("token too long")
        payload = store.crypto.decrypt(token.encode())
        decoder = zlib.decompressobj()
        decoded = decoder.decompress(payload, 40 * 1024 * 1024)
        if decoder.unconsumed_tail or not decoder.eof:
            raise ValueError("oversized claims")
        claims = json.loads(decoded)
    except (ValueError, TypeError, zlib.error) as exc:
        raise RapportError("dataset_export_invalid", "导出预览已失效，请重新预览。", status=409) from exc
    manifest, data = _built(store, dataset_id)
    if claims.get("kind") != "dataset_export_selection_v1" or claims.get("id") != dataset_id or claims.get("expires", 0) < time.time() or claims.get("snapshot_sha256") != manifest["snapshot_sha256"] or claims.get("owner") != owner:
        raise RapportError("dataset_export_invalid", "导出预览已失效，请重新预览。", status=409)
    index = {(pool, row["id"]): row for pool, rows in data.items() for row in rows}
    found = []
    try:
        for pool, sample_id in claims["keys"]:
            found.append((pool, index[(pool, sample_id)]))
        if len({(pool, row["id"]) for pool, row in found}) != len(found):
            raise ValueError("duplicate selection")
    except (KeyError, TypeError, ValueError) as exc:
        raise RapportError("dataset_export_invalid", "导出样本清单已失效。", status=409) from exc
    validate_splits({pool: [row for selected_pool, row in found if pool == selected_pool] for pool in POOLS})
    rows = [_project(dataset_id, row, pool) for pool, row in found]
    if _rows_digest(rows) != claims["manifest"]["selection_sha256"]:
        raise RapportError("dataset_export_invalid", "导出样本内容已变化，请重新预览。", status=409)
    return claims, rows


def preview_page(store, dataset_id, token, *, owner="", page=0, page_size=20):
    if page < 0 or not 1 <= page_size <= 100:
        raise RapportError("dataset_bad_page", "分页参数无效。", status=422)
    claims, rows = _resolve(store, dataset_id, token, owner)
    return {"items": rows[page * page_size : (page + 1) * page_size], "total": len(rows), "page": page, "page_size": page_size,
            "manifest": claims["manifest"], "target_environment": claims["target_environment"], "selectors": claims["manifest"]["selectors"]}


def export(store, dataset_id, token, confirmed, *, target_environment="local", owner=""):
    if not confirmed:
        raise RapportError("dataset_confirmation_required", "请先预览并确认本次导出。", status=409)
    claims, rows = _resolve(store, dataset_id, token, owner)
    if target_environment != claims["target_environment"]:
        raise RapportError("dataset_export_environment", "目标环境与已预览内容不一致，请重新预览确认。", status=409)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", encode(claims["manifest"]))
        selectors = claims["manifest"]["selectors"]
        for pool in claims["manifest"]["pools"]:
            for split in selectors["splits"] or SPLITS:
                archive.writestr(f"{pool}/{split}.jsonl", b"\n".join(encode(row) for row in rows if row["pool"] == pool and row["split"] == split))
    return output.getvalue()
