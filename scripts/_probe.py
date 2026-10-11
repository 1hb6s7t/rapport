"""One progress probe for every long-running script.

Piped stdout is block-buffered, which is why an early 10-minute evaluation run showed nothing
until it finished. Every case now prints with flush=True, and `--progress <file>` appends the same
line so a run can be tailed from outside. A run ends with a `DONE ...` line carrying the counts, so
"it stopped" and "it finished" are distinguishable without reading the whole log.
"""

from __future__ import annotations

from pathlib import Path


class Probe:
    def __init__(self, path: str | None = None):
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("", encoding="utf-8")

    def line(self, text: str) -> None:
        print(text, flush=True)
        if self.path:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(text + "\n")

    def done(self, text: str) -> None:
        self.line(text)
