"""Generate a minimal SBOM from the lock files.

Deliberately minimal and honest: it lists exactly what is locked, from the files that actually pin
it, and it does NOT guess licences — those are recorded separately in THIRD_PARTY_NOTICES.md and
still need a full audit before release.

    cd backend && uv run python ../scripts/make_sbom.py
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def from_uv_lock(path: Path) -> list[dict]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    out: list[dict] = []
    for block in text.split("[[package]]")[1:]:
        name = re.search(r'name = "([^"]+)"', block)
        version = re.search(r'version = "([^"]+)"', block)
        if name and version:
            out.append({"ecosystem": "pypi", "name": name.group(1), "version": version.group(1)})
    return out


def from_package_lock(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[dict] = []
    for key, entry in (data.get("packages") or {}).items():
        if not key or not isinstance(entry, dict) or "version" not in entry:
            continue  # the root project entry has no version of its own
        name = entry.get("name") or key.split("node_modules/")[-1]
        if not name:
            continue
        out.append({"ecosystem": "npm", "name": name, "version": str(entry["version"])})
    seen: set[tuple[str, str]] = set()
    unique: list[dict] = []
    for c in sorted(out, key=lambda c: (c["name"], c["version"])):
        key = (c["name"], c["version"])
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return unique


def main() -> int:
    components = from_uv_lock(ROOT / "backend" / "uv.lock") + from_package_lock(ROOT / "frontend" / "package-lock.json")
    sbom = {
        "format": "rapport-sbom-v1",
        "generated_at": date.today().isoformat(),
        "project": "rapport",
        "note": (
            "由 backend/uv.lock 与 frontend/package-lock.json 生成，只包含锁定组件的名称与版本。"
            "许可类别见 THIRD_PARTY_NOTICES.md；本文件不构成完整许可审计。"
        ),
        "counts": {
            "pypi": sum(1 for c in components if c["ecosystem"] == "pypi"),
            "npm": sum(1 for c in components if c["ecosystem"] == "npm"),
        },
        "components": components,
    }
    out = ROOT / "sbom.json"
    out.write_text(json.dumps(sbom, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"sbom.json: {sbom['counts']['pypi']} pypi + {sbom['counts']['npm']} npm components -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
