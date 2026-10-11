"""WeChat third-party export parsers (CSV and JSON row-list exports).

CSV columns (subset, order-insensitive, header names are matched loosely):
    localId, TalkerId, Type, SubType, IsSender, CreateTime, StrTime, Remark, NickName, Sender, StrContent
JSON: a list of objects with the same keys.

Type mapping (WeChat internal):
    1 text · 3 image · 34 voice · 43 video · 47 sticker · 49 link/file/transfer (SubType 6 file, 2000 transfer)
    10000 system · 50 call
"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime

from rapport.parsers.base import Builder, ParseError, ParseOptions, Parser, looks_system
from rapport.schemas.message_v1 import Conversation, MsgType, Participants, Sender, Source, TsPrecision

TYPE_MAP = {
    "1": MsgType.text,
    "3": MsgType.image,
    "34": MsgType.voice,
    "43": MsgType.video,
    "47": MsgType.sticker,
    "50": MsgType.call,
    "10000": MsgType.system,
}
QUOTE_RE = re.compile(r"^(?P<body>.*?)\n?[「“\"](?P<quoted>[^」”\"]{1,200})[」”\"]\s*$", re.S)
QUOTE_PREFIX_RE = re.compile(r"^「(?P<who>[^:：」]{1,30})[:：]\s*(?P<quoted>[^」]{1,300})」\s*[-—]*\s*(?P<body>.*)$", re.S)


def _norm_key(k: str) -> str:
    return re.sub(r"[^a-z]", "", k.lower())


def _pick(row: dict, *cands: str) -> str | None:
    for c in cands:
        for k, v in row.items():
            if _norm_key(k) == _norm_key(c) and v not in (None, ""):
                return str(v)
    return None


def _rows_to_conv(rows: list[dict], conv_id: str, source: Source, options: ParseOptions) -> Conversation:
    b = Builder(source=source, timezone=options.timezone)
    other_nick = None
    n_unknown_type = 0
    for row in rows:
        content = _pick(row, "StrContent", "content", "msg", "text") or ""
        is_sender = _pick(row, "IsSender", "isSend", "is_sender")
        typ_raw = _pick(row, "Type", "type", "MsgType") or "1"
        sub = _pick(row, "SubType", "subType") or "0"
        ts_raw = _pick(row, "StrTime", "CreateTime", "time", "createTime")
        nick = _pick(row, "NickName", "Remark", "nickname", "remark")
        ts, prec = None, TsPrecision.none
        if ts_raw:
            if ts_raw.isdigit():
                ts, prec = datetime.fromtimestamp(int(ts_raw)), TsPrecision.second
            else:
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
                    try:
                        ts = datetime.strptime(ts_raw.strip(), fmt)
                        prec = TsPrecision.second if "%S" in fmt else TsPrecision.minute
                        break
                    except ValueError:
                        continue
        typ = TYPE_MAP.get(str(typ_raw).strip())
        if typ is None:
            if str(typ_raw).strip() == "49":
                typ = {"6": MsgType.file, "2000": MsgType.transfer, "57": MsgType.text}.get(sub, MsgType.link)
            else:
                typ = MsgType.unknown
                n_unknown_type += 1
        if is_sender in ("1", "True", "true"):
            sender = Sender.me
        elif is_sender in ("0", "False", "false"):
            sender = Sender.other
            if nick and not other_nick:
                other_nick = nick
        else:
            sender = Sender.unknown
        if typ == MsgType.system or looks_system(content):
            sender, typ = Sender.system, MsgType.system
        quoted = None
        text = content.strip() if typ == MsgType.text else None
        if text:
            qm = QUOTE_PREFIX_RE.match(text)
            if qm:
                quoted, text = qm.group("quoted").strip(), qm.group("body").strip()
            else:
                qm2 = QUOTE_RE.match(text)
                if qm2 and "\n" in text:
                    quoted, text = qm2.group("quoted").strip(), qm2.group("body").strip()
        m = b.add(sender, text if typ != MsgType.system else content.strip(), ts, prec, typ, quoted=quoted)
        if typ not in (MsgType.text, MsgType.system):
            m.meta["placeholder"] = content.strip()[:60]
    if not b.messages:
        raise ParseError("empty", "没有解析到任何消息行。", "请确认是微信导出工具生成的 CSV 或 JSON。")
    unknown_ids = [m.id for m in b.messages if m.sender == Sender.unknown]
    if unknown_ids:
        b.warn("unknown_sender", f"{len(unknown_ids)} 条消息缺少 IsSender 字段，请在校正页确认发送者。", unknown_ids)
    if n_unknown_type:
        b.warn("unknown_type", f"{n_unknown_type} 条消息类型未识别，按未知事件保留。")
    b.participants = Participants(me=options.me_name or "我", other=other_nick or "对方")
    return b.build(conv_id)


class WeChatCsvParser(Parser):
    id = "wechat_csv"
    label = "微信导出 CSV（第三方工具）"
    source = Source.wechat_csv
    accepts_files = (".csv",)

    def sniff(self, text: str, filename: str | None = None) -> float:
        head = text[:500].lower()
        if "strcontent" in head and ("issender" in head or "createtime" in head):
            return 0.95
        if filename and filename.lower().endswith(".csv") and "content" in head:
            return 0.5
        return 0.0

    def parse(self, text: str, conv_id: str, options: ParseOptions | None = None) -> Conversation:
        options = options or ParseOptions()
        reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
        rows = list(reader)
        return _rows_to_conv(rows, conv_id, self.source, options)


class WeChatJsonParser(Parser):
    id = "wechat_json"
    label = "微信导出 JSON（第三方工具）"
    source = Source.wechat_json
    accepts_files = (".json",)

    def sniff(self, text: str, filename: str | None = None) -> float:
        head = text[:800]
        if head.lstrip().startswith("[") and "StrContent" in head:
            return 0.95
        if head.lstrip().startswith("[") and "IsSender" in head:
            return 0.9
        return 0.0

    def parse(self, text: str, conv_id: str, options: ParseOptions | None = None) -> Conversation:
        options = options or ParseOptions()
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ParseError("bad_json", f"JSON 语法错误：{e.msg}（第 {e.lineno} 行）") from e
        if isinstance(data, dict) and "messages" in data:
            data = data["messages"]
        if not isinstance(data, list):
            raise ParseError("bad_json", "JSON 顶层应为消息数组。")
        return _rows_to_conv(data, conv_id, self.source, options)
