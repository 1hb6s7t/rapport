"""Audit/build a local redacted corpus without editing its source or transmitting data."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from rapport.datasets import DatasetStore  # noqa: E402
from rapport.security import Crypto  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", default=os.environ.get("RAPPORT_EVAL_SOURCE"), required=not os.environ.get("RAPPORT_EVAL_SOURCE"))
    parser.add_argument("--data-dir", required=True, help="Independent private app data directory, outside the source and repository")
    parser.add_argument("--build", action="store_true")
    args = parser.parse_args()
    root = Path(args.data_dir).expanduser().resolve()
    source = Path(args.source_root).expanduser().resolve()
    protected = source.parent if source.parent.name == ".rapport" else source
    repo = Path(__file__).resolve().parents[1]
    if root.is_relative_to(protected) or root.is_relative_to(repo) or protected.is_relative_to(root):
        parser.error("Output must be outside the source and repository; source remains read-only.")
    store = DatasetStore(root / "datasets", Crypto(root / ".key"))
    result = store.build(source) if args.build else store.audit(source)
    print(json.dumps({k: result[k] for k in ("id", "status", "counts", "pools", "splits")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
