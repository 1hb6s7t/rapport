"""Desktop integration (Windows accessibility). Optional extra: `uv sync --extra desktop`."""

from rapport.desktop.paste import available, inspect_focus, paste_into_focus

__all__ = ["available", "inspect_focus", "paste_into_focus"]
