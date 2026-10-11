"""Private-evaluation guard: results built from the user's real material never land in the repo.

Every script that takes real chats, feedback exports, outcome labels or rating sheets must write
under `~/.rapport/eval/` (or wherever `RAPPORT_DATA_DIR` points). `ensure_private_out` refuses
any path inside the repository checkout so the guarantee is enforced by code, not by memory.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def private_eval_dir() -> Path:
    base = Path(os.environ.get("RAPPORT_DATA_DIR", str(Path.home() / ".rapport")))
    d = base / "eval"
    d.mkdir(parents=True, exist_ok=True)
    return d


def is_inside_repo(path: Path) -> bool:
    try:
        return path.resolve().is_relative_to(REPO_ROOT)
    except (OSError, ValueError):
        return False


def ensure_private_out(path: str | Path | None, default_name: str) -> Path:
    """Resolve an output path for private results; refuse anything inside the repo."""
    target = Path(path) if path else private_eval_dir() / default_name
    if is_inside_repo(target):
        raise SystemExit(
            f"拒绝写入仓库内路径 {target}：真实材料的评测结果只能写到 {private_eval_dir()}（或 RAPPORT_DATA_DIR/eval）。"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    return target
