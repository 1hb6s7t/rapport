"""Generic message-v1 JSON parser (strict)."""

from __future__ import annotations

import json

from pydantic import ValidationError

from rapport.parsers.base import ParseError, ParseOptions, Parser
from rapport.schemas.message_v1 import Conversation, Source


class GenericJsonParser(Parser):
    id = "generic_json"
    label = "标准 message-v1 JSON"
    source = Source.generic_json
    accepts_files = (".json",)

    def sniff(self, text: str, filename: str | None = None) -> float:
        head = text[:400]
        return 0.98 if '"schema_version"' in head and "message-v1" in head else 0.0

    def parse(self, text: str, conv_id: str, options: ParseOptions | None = None) -> Conversation:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ParseError("bad_json", f"JSON 语法错误：{e.msg}（第 {e.lineno} 行，第 {e.colno} 列）") from e
        if not isinstance(data, dict):
            raise ParseError("bad_json", "顶层必须是对象。")
        data["id"] = conv_id
        data.setdefault("source", "generic_json")
        try:
            conv = Conversation.model_validate(data)
        except ValidationError as e:
            first = e.errors()[0]
            loc = ".".join(str(x) for x in first["loc"])
            raise ParseError("schema", f"字段 {loc}：{first['msg']}", "请对照 message-v1 合同修正。") from e
        return conv
