"""Redact a chat export on this machine, so the cleaned text can be annotated elsewhere.

Why rules and not a model for the main pass: redaction is a safety step, and a model that misses
one name, or paraphrases a sentence while "cleaning" it, defeats the purpose. Regex plus a
name dictionary built from the export itself is deterministic, repeatable, and — most importantly —
checkable: this script re-scans its own output and reports anything it could not account for.

What gets replaced:
  - the two participants' names/nicknames/remarks, plus every name the export itself mentions
    (sender display names and avatar keys), consistently: one person → one alias everywhere, so the
    conversation stays readable and the annotator can still tell who is who;
  - account identifiers (`wxid_…`, sender usernames, message ids);
  - the shared patterns from `rapport.llm.redact` (phone, id card, bank card, email, URL, address,
    plate, wechat/qq ids);
  - media file paths, keeping the type and the order but dropping the recognisable filename.

Nothing leaves the machine. Output goes under `~/.rapport/eval/` (enforced), and the mapping table
— which contains the real names — is written there too and must never be sent anywhere.

Usage:
    uv run python ../scripts/private_redact.py --material "D:/exports"
    uv run python ../scripts/private_redact.py --material "D:/exports/私聊_某位" --keep-emoji-md5
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _private import ensure_private_out, private_eval_dir  # noqa: E402

from rapport.llm.redact import PATTERNS, RedactStats, redact_names, redact_text  # noqa: E402

# Identifiers that are personal even though no shared pattern catches them. Matched by field name
# so the substitution is deliberate rather than a 19-digit id happening to look like a bank card.
ID_FIELDS = {
    "platformMessageId": "[消息ID]",
    "senderAvatarKey": "[头像标识]",
    "senderUsername": "[账号]",
    "wxid": "[微信号]",
    "emojiCdnUrl": "[链接]",
    "avatar": "[头像链接]",
}
WXID_RE = re.compile(r"\bwxid_[A-Za-z0-9_-]{4,}\b")
# Filenames may be Chinese; the class must allow anything that is not a JSON delimiter.
PATH_RE = re.compile(
    r"media[\\/][^\s\"'<>|*?]+\.(?:jpg|jpeg|png|gif|webp|bmp|mp4|mov|avi|wav|amr|silk|mp3|m4a|pdf|docx?|xlsx?|pptx?|zip|rar|7z)",
    re.I,
)
MD5_RE = re.compile(r"\b[0-9a-f]{32}\b")
# A filename mentioned *inside a message* (no `media/` prefix) still carries information — course
# names, school names, paperwork. It has to go too, and it is the one the first pass missed.
BARE_FILE_RE = re.compile(
    r"(?<![\w/\\])[^\s\"'<>|*?\\/]{2,60}\.(?:docx?|xlsx?|pptx?|pdf|zip|rar|7z|txt|csv|jpg|jpeg|png|gif|bmp|mp4|mov|avi|wav|mp3|amr|silk)(?![\w])",
    re.I,
)
# What this script itself produces — so the self-check does not flag its own aliases as leftovers.
ALIAS_RE = re.compile(r"^media/(?:images|emojis|videos|voices|files)/file_\d{4}\.[a-z0-9]+$", re.I)


def _name_candidates(data: dict) -> tuple[list[str], list[str]]:
    """(my_names, other_names) — what the export says the two sides are called."""
    sess = data.get("session") or {}
    other = [str(sess.get(k) or "").strip() for k in ("remark", "displayName", "nickname")]
    mine: list[str] = []
    other += [str(k) for k in (data.get("avatars") or {})]
    others_seen: set[str] = set()
    for m in data.get("messages") or []:
        dn = str(m.get("senderDisplayName") or "").strip()
        un = str(m.get("senderUsername") or "").strip()
        if not dn:
            continue
        if m.get("isSend") == 1:
            mine.append(dn)
            if un.startswith("wxid_"):
                other.append(un)
        else:
            others_seen.add(dn)
    return [n for n in dict.fromkeys(mine) if n], [n for n in dict.fromkeys(other) if n] + sorted(others_seen)


def build_alias_map(data: dict, stats: RedactStats) -> list[tuple[str, str]]:
    """Longest-first list of (real, alias). One person keeps one alias across the whole file."""
    mine, other = _name_candidates(data)
    pairs: list[tuple[str, str]] = []
    for n in mine:
        if len(n) >= 2:
            pairs.append((n, "我"))
    for n in other:
        if len(n) >= 2 and not n.startswith("wxid_"):
            pairs.append((n, "对方"))
    # Anything else that shows up as a display name is a third party: keep them distinguishable.
    known = {p[0] for p in pairs}
    extra = 0
    for m in data.get("messages") or []:
        for key in ("senderDisplayName",):
            v = str(m.get(key) or "").strip()
            if len(v) >= 2 and v not in known and not v.startswith("wxid_"):
                known.add(v)
                extra += 1
                pairs.append((v, f"某人{extra}"))
    stats.names_replaced += len(pairs)
    return pairs


def redact_obj(obj, aliases: list[tuple[str, str]], media_map: dict[str, str], stats: RedactStats, drop_md5: bool):
    """Walk the export and clean every string, keeping the structure byte-for-byte compatible.

    Keys are cleaned too, not just values: `avatars` is keyed by wxid, and leaving the keys alone
    shipped the account ids straight through while the check said "values are clean".
    """
    if isinstance(obj, dict):
        out: dict = {}
        for k, v in obj.items():
            nk = redact_obj(k, aliases, media_map, stats, drop_md5) if isinstance(k, str) else k
            out[nk] = ID_FIELDS[k] if (k in ID_FIELDS and isinstance(v, str) and v) else redact_obj(v, aliases, media_map, stats, drop_md5)
        return out
    if isinstance(obj, list):
        return [redact_obj(v, aliases, media_map, stats, drop_md5) for v in obj]
    if not isinstance(obj, str):
        return obj
    s = PATH_RE.sub(lambda m: media_map.setdefault(m.group(0), _media_alias(m.group(0), len(media_map))), obj)
    s = WXID_RE.sub("[微信号]", s)
    if drop_md5:
        s = MD5_RE.sub("[md5]", s)
    s = redact_names(s, aliases, stats)
    s = redact_text(s, stats)
    return BARE_FILE_RE.sub("[文件]", s)


def _media_alias(original: str, n: int) -> str:
    ext = Path(original.replace("\\", "/")).suffix.lower()
    kind = "images"
    low = original.lower()
    if "emoji" in low or ext == ".gif":
        kind = "emojis"
    elif "video" in low or ext == ".mp4":
        kind = "videos"
    elif ext in (".wav", ".amr", ".silk", ".mp3"):
        kind = "voices"
    elif ext in (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".rar"):
        kind = "files"
    return f"media/{kind}/file_{n + 1:04d}{ext}"


def residual_scan(out: dict, aliases: list[tuple[str, str]]) -> list[str]:
    """Re-scan what we are about to hand over. Anything found here must be dealt with, not shipped."""
    blob = json.dumps(out, ensure_ascii=False)
    found: list[str] = []
    for real, _alias in aliases:
        if real and real in blob:
            found.append(f"人名残留：{real}")
    if WXID_RE.search(blob):
        found.append("仍有 wxid_ 形式的账号")
    for name, pat, _repl in PATTERNS:
        n = len(pat.findall(blob))
        if n:
            found.append(f"仍有 {name} × {n}")
    for m in PATH_RE.finditer(blob):
        if not ALIAS_RE.match(m.group(0)):
            found.append(f"仍有原始媒体路径：{m.group(0)[:40]}")
    for m in BARE_FILE_RE.finditer(blob):
        found.append(f"仍有裸文件名：{m.group(0)[:40]}")
    return found


MEDIA_MARK = {"图片消息": "[图片]", "动画表情": "[动画表情]", "视频消息": "[视频]", "通话消息": "[通话]"}


def _lines_for(data: dict) -> list[str]:
    """The numbered lines, in the form the annotation prompt expects. Media becomes a marker, never
    a filename: the annotator cannot see the file and must not guess it, and the filename is exactly
    what redaction removed."""
    import datetime as _dt

    lines: list[str] = []
    for i, m in enumerate(data.get("messages") or [], 1):
        typ = m.get("type", "")
        body = MEDIA_MARK.get(typ) or (str(m.get("content") or "").strip() or f"[{typ}]")
        who = "我" if m.get("isSend") == 1 else "对方"
        ts = m.get("createTime")
        when = _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if isinstance(ts, int) else ""
        lines.append(f"[{i}] {when} {who}：{body}")
    return lines


def emit_lines(data: dict, out_dir: Path, stem: str) -> int:
    lines = _lines_for(data)
    (out_dir / f"{stem}.lines.txt").write_text("\n".join(lines), encoding="utf-8")
    return len(lines)


def emit_batches(data: dict, out_dir: Path, stem: str, size: int, carry: int = 3) -> dict:
    """One file per request. Each file repeats the previous file's last `carry` lines, so the batch
    boundary does not turn an answer into an isolated line — a loss that is invisible in the output
    and quietly lowers every metric."""
    lines = _lines_for(data)
    chunks: list[list[str]] = []
    for start in range(0, len(lines), size):
        head = lines[max(0, start - carry) : start] if start else []
        chunks.append(head + lines[start : start + size])
    d = out_dir / "batches" / stem
    d.mkdir(parents=True, exist_ok=True)
    for f in d.glob("batch_*.txt"):
        f.unlink()
    for i, ch in enumerate(chunks, 1):
        (d / f"batch_{i:04d}.txt").write_text("\n".join(ch), encoding="utf-8")
    return {"batches": len(chunks), "dir": str(d), "lines": len(lines)}


def process(path: Path, out_dir: Path, args) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    stats = RedactStats()
    aliases = build_alias_map(data, stats)
    media_map: dict[str, str] = {}
    out = redact_obj(data, aliases, media_map, stats, not args.keep_emoji_md5)
    resid = residual_scan(out, aliases)
    target = out_dir / path.name
    target.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    n_lines = emit_lines(out, out_dir, path.stem) if args.emit_lines else 0
    batch_info = emit_batches(out, out_dir, path.stem, args.emit_batches) if args.emit_batches else {}
    return {
        "file": path.name,
        "messages": len(data.get("messages") or []),
        "aliases": len(aliases),
        "media_paths": len(media_map),
        "lines": n_lines,
        "batches": batch_info.get("batches", 0),
        "counts": dict(sorted(stats.counts.items(), key=lambda kv: -kv[1])),
        "residual": resid,
        "out": str(target),
        "alias_table": aliases,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--material", required=True, help="a single-chat export JSON, or a directory of them")
    ap.add_argument("--out-dir", default=None, help="must be outside the repo; defaults to ~/.rapport/eval/redacted/")
    ap.add_argument("--copy-media", action="store_true", help="also copy the media folder next to the redacted JSON (renamed files only)")
    ap.add_argument("--emit-lines", action="store_true", help="also write <name>.lines.txt — the numbered form the annotation prompt expects")
    ap.add_argument("--emit-batches", type=int, default=0, metavar="N", help="also split into batches of N lines (with 3 lines of carry-over) under batches/<name>/")
    ap.add_argument("--keep-emoji-md5", action="store_true", help="keep 32-hex sticker hashes (they identify a sticker, not a person)")
    args = ap.parse_args()

    material = Path(args.material)
    if not material.exists():
        ap.error(f"找不到材料：{material}")
    exports = sorted(material.glob("*/*.json")) or sorted(material.glob("*.json")) if material.is_dir() else [material]
    if not exports:
        ap.error(f"{material} 下没有可识别的单聊导出 JSON")

    out_dir = ensure_private_out(args.out_dir, "redacted") if args.out_dir else (private_eval_dir() / "redacted")
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"出口目录：{out_dir}（本机，不进仓库）")

    reports = []
    all_aliases: list[tuple[str, str]] = []
    for p in exports:
        r = process(p, out_dir, args)
        reports.append(r)
        all_aliases += r["alias_table"]
        print(f"\n=== {r['file']}：{r['messages']} 条消息 ===")
        print(f"  人名/别名 {r['aliases']} 个 · 媒体路径 {r['media_paths']} 个")
        print(f"  替换统计：{r['counts'] or '（无共享模式命中）'}")
        if r["residual"]:
            print("  ⚠️ 残留（**不要外发**，先处理）：")
            for x in r["residual"][:10]:
                print("     - " + x)
        else:
            print("  ✅ 自检：未发现人名、账号、共享模式或原始媒体路径的残留")

    table = out_dir / "_alias-table.json"
    table.write_text(
        json.dumps({"note": "真实名字 → 代号。本文件只在本机保留，**绝不能发给任何模型或上传**。", "aliases": sorted(set(all_aliases))}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    summary = out_dir / "_redaction-report.json"
    summary.write_text(json.dumps({"reports": [{k: v for k, v in r.items() if k != "alias_table"} for r in reports]}, ensure_ascii=False, indent=1), encoding="utf-8")

    if args.copy_media:
        src_media = material if material.is_dir() and (material / "media").exists() else material.parent / "media"
        if src_media.exists():
            dst = out_dir / "media"
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src_media, dst)
            print(f"\n已复制媒体到 {dst}（文件名仍是原始的，**不要随脱敏文本一起外发**）")

    bad = sum(len(r["residual"]) for r in reports)
    print(f"\n映射表 {table}（含真实名字，只在本机）")
    print(f"报告 {summary}")
    print(f"DONE exports={len(reports)} residual={bad} 别名的真实名字千万不要外发")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
