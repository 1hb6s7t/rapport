"""Pure-Python BERT WordPiece tokenizer (the reference slow-tokenizer algorithm).

The packaged app ships no `tokenizers`/`transformers`; ONNX Runtime is already present for ASR,
so the tokenizer is reimplemented here (~150 lines, zero new dependencies). Parity with the
reference `BertTokenizerFast` is checked sample by sample at model install time
(`tools/emotion_model.py verify`) before the manifest is written; the runtime trusts that.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

MAX_CHARS_PER_WORD = 100


def _is_whitespace(ch: str) -> bool:
    return ch in " \t\n\r" or unicodedata.category(ch) == "Zs"


def _is_control(ch: str) -> bool:
    return ch in "\t\n\r" or unicodedata.category(ch) in ("Cc", "Cf")


def _is_punctuation(ch: str) -> bool:
    cp = ord(ch)
    if (33 <= cp <= 47) or (58 <= cp <= 64) or (91 <= cp <= 96) or (123 <= cp <= 126):
        return True
    return unicodedata.category(ch).startswith("P")


def _is_chinese_char(cp: int) -> bool:
    return (
        (0x4E00 <= cp <= 0x9FFF)
        or (0x3400 <= cp <= 0x4DBF)
        or (0x20000 <= cp <= 0x2A6DF)
        or (0x2A700 <= cp <= 0x2B73F)
        or (0x2B740 <= cp <= 0x2B81F)
        or (0x2B820 <= cp <= 0x2CEAF)
        or (0xF900 <= cp <= 0xFAFF)
        or (0x2F800 <= cp <= 0x2FA1F)
    )


class BasicTokenizer:
    def __init__(self, do_lower_case: bool = True, strip_accents: bool | None = None):
        self.do_lower_case = do_lower_case
        self.strip_accents = do_lower_case if strip_accents is None else strip_accents

    @staticmethod
    def _clean_text(text: str) -> str:
        out = []
        for ch in text:
            cp = ord(ch)
            if cp == 0 or cp == 0xFFFD or _is_control(ch):
                out.append(" ")
            elif _is_whitespace(ch):
                out.append(" ")
            else:
                out.append(ch)
        return "".join(out)

    @staticmethod
    def _tokenize_chinese_chars(text: str) -> str:
        out = []
        for ch in text:
            if _is_chinese_char(ord(ch)):
                out.append(" ")
                out.append(ch)
                out.append(" ")
            else:
                out.append(ch)
        return "".join(out)

    @staticmethod
    def _strip_accents(text: str) -> str:
        return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")

    @staticmethod
    def _split_on_punc(token: str) -> list[str]:
        chars = list(token)
        out: list[str] = []
        start = 0
        for i, ch in enumerate(chars):
            if _is_punctuation(ch):
                if start < i:
                    out.append("".join(chars[start:i]))
                out.append(ch)
                start = i + 1
        if start < len(chars):
            out.append("".join(chars[start:]))
        return out

    def tokenize(self, text: str) -> list[str]:
        text = self._tokenize_chinese_chars(self._clean_text(text))
        tokens: list[str] = []
        for token in text.split():
            if self.do_lower_case:
                token = token.lower()
                if self.strip_accents:
                    token = self._strip_accents(token)
            tokens.extend(self._split_on_punc(token))
        return " ".join(tokens).split()


class WordPieceTokenizer:
    def __init__(self, vocab: dict[str, int], unk_token: str = "[UNK]", max_chars: int = MAX_CHARS_PER_WORD):
        self.vocab = vocab
        self.unk_token = unk_token
        self.max_chars = max_chars

    def tokenize(self, token: str) -> list[str]:
        chars = list(token)
        if len(chars) > self.max_chars:
            return [self.unk_token]
        sub_tokens: list[str] = []
        start = 0
        while start < len(chars):
            end = len(chars)
            current = None
            while start < end:
                piece = "".join(chars[start:end])
                if start > 0:
                    piece = "##" + piece
                if piece in self.vocab:
                    current = piece
                    break
                end -= 1
            if current is None:
                return [self.unk_token]
            sub_tokens.append(current)
            start = end
        return sub_tokens


class BertTokenizer:
    """vocab.txt -> ids with [CLS]/[SEP] and truncation, matching BertTokenizerFast defaults."""

    def __init__(self, vocab_file: str | Path, do_lower_case: bool = True):
        vocab: dict[str, int] = {}
        with Path(vocab_file).open(encoding="utf-8") as fh:
            for index, line in enumerate(fh):
                vocab[line.rstrip("\n")] = index
        self.vocab = vocab
        self.cls_id = vocab["[CLS]"]
        self.sep_id = vocab["[SEP]"]
        self.unk_id = vocab["[UNK]"]
        self._basic = BasicTokenizer(do_lower_case=do_lower_case)
        self._wordpiece = WordPieceTokenizer(vocab)

    def tokenize(self, text: str) -> list[str]:
        return ["[CLS]", *self._wordpiece.tokenize_batch(self._basic.tokenize(text)), "[SEP]"]

    def encode(self, text: str, max_len: int = 128) -> list[int]:
        tokens = self._basic.tokenize(text)
        pieces: list[str] = []
        for token in tokens:
            pieces.extend(self._wordpiece.tokenize(token))
        ids = [self.cls_id, *(self.vocab.get(p, self.unk_id) for p in pieces), self.sep_id]
        if len(ids) > max_len:
            ids = ids[: max_len - 1] + [self.sep_id]
        return ids
