"""Report body upgrades: a stored report is read through here before it is validated.

`report-v1` is the only version so far, so today this is a registry with no steps in it.
Its value is the seam: when a field is renamed or a section restructured, the upgrade is a
pure function registered here, applied on read, and old rows never have to be rewritten.
Unknown versions are refused loudly instead of half-parsed.
"""

from __future__ import annotations

from collections.abc import Callable

from rapport.config import REPORT_SCHEMA_VERSION

Upgrade = Callable[[dict], dict]

# from_version -> (to_version, upgrade)
_UPGRADES: dict[str, tuple[str, Upgrade]] = {}


class UnsupportedReportSchema(ValueError):
    pass


def register_upgrade(from_version: str, to_version: str):
    def deco(fn: Upgrade) -> Upgrade:
        _UPGRADES[from_version] = (to_version, fn)
        return fn

    return deco


def upgrade_report_body(body: dict) -> dict:
    """Return a body at the current schema version (a copy; the input is not mutated)."""
    out = dict(body)
    version = str(out.get("schema_version") or "report-v1")
    seen: set[str] = set()
    while version != REPORT_SCHEMA_VERSION:
        if version in seen or version not in _UPGRADES:
            raise UnsupportedReportSchema(f"no upgrade path from {version} to {REPORT_SCHEMA_VERSION}")
        seen.add(version)
        version, fn = _UPGRADES[version]
        out = fn(out)
        out["schema_version"] = version
    out.setdefault("schema_version", REPORT_SCHEMA_VERSION)
    return out
