"""Load complete, pinned skill sources; never execute repository scripts."""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).parent / "skills" / "goutoujunshi"
MARKER = "\nRAPPORT_SKILL_BUNDLE: goutoujunshi@6db7354a4002dc7c448a9c87ffdad8132570c9d3\n"


@lru_cache(maxsize=1)
def sources():
    manifest = json.loads((ROOT / "manifest.json").read_text("utf-8"))
    output = {}
    for name, expected in manifest["files"].items():
        path = ROOT / name
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError("Pinned skill source changed: " + name)
        output[name] = data.decode("utf-8")
    return output


def augment(system: str, topic: str = "natural") -> tuple[str, list[str]]:
    docs = sources()
    composer = next(n for n in docs if "实战话术编排器" in n)
    extra = next(n for n in docs if ("07-沟通" if topic == "repair" else "场景感、松弛感") in n)
    names = ["SKILL.md", composer, extra]
    system += MARKER
    for name in names:
        system += "\n---参考资料 " + name + "---\n" + docs[name]
    system += "\n当前只做即时回复起草：已有档案中未知项保持未知，不做首次问卷，不启用跨任务记忆。按本次明确指定的输出格式给成品，不附分析或后续分支。"
    return system, names
