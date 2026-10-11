"""Pixel evidence for chat content that is not chat text: pictures, file cards and avatars.

OCR reads the words inside a screenshot someone sent, a photo of a sign or a stylised avatar as if
they were chat text. The chat crop is split into blocks that differ from the chat background and
each block is measured instead:

- an avatar is a small square at the outer edge of the chat;
- a file card carries a size line such as ``467.8K`` under its file name;
- a picture is a block without any recognised words, a block whose pixels are far from one flat
  bubble colour, or one whose words are much smaller than chat text (a phone screenshot).

Words inside those blocks are removed. Pictures and file cards come back as placeholder regions on
the side they were sent from; flat bubbles and words on the background (time labels) are left
alone. Pure pixel arithmetic on the crop that was recognised; no accessibility text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from statistics import median

from rapport.ocr.engine import OcrBox

FILE_SIZE = re.compile(r"^\d{1,4}(?:[.,]\d{1,2})?(?:[KMGT]B?|B)$", re.I)


@dataclass
class Region:
    kind: str  # image | file
    side: str  # left | right | center
    x0: int
    y0: int
    x1: int
    y1: int
    name: str | None = None

    @property
    def placeholder(self) -> str:
        if self.kind == "transfer":
            return "[转账]"
        if self.kind == "card":
            return "[卡片，内容待核对]"
        if self.kind == "file":
            return f"[文件] {self.name}" if self.name else "[文件]"
        return "[图片]"


@dataclass
class Block:
    x0: int
    y0: int
    x1: int
    y1: int
    uniform: float  # share of the block's pixels within a few levels of its most common colour
    on_background: bool  # most common colour is the chat background: words drawn on it, no bubble
    boxes: list[OcrBox] = field(default_factory=list)
    colour: tuple[int, int, int] | None = None
    fill_ratio: float = 1.0

    @property
    def w(self) -> int:
        return self.x1 - self.x0

    @property
    def h(self) -> int:
        return self.y1 - self.y0

    def holds(self, box: OcrBox) -> bool:
        return self.x0 <= box.cx <= self.x1 and self.y0 <= box.cy <= self.y1


def dominant(pixels):
    """Most common colour of an (n, 3) int array, to the nearest two levels."""
    import numpy as np

    q = pixels // 2
    packed = (q[:, 0].astype(np.int32) << 14) | (q[:, 1].astype(np.int32) << 7) | q[:, 2].astype(np.int32)
    values, counts = np.unique(packed, return_counts=True)
    top = int(values[counts.argmax()])
    return np.array([(top >> 14) & 127, (top >> 7) & 127, top & 127], dtype=np.int16) * 2


def blocks(image) -> tuple[list[Block], int, int]:
    import cv2
    import numpy as np

    rgb = np.asarray(image.convert("RGB"), dtype=np.int16)
    height, width = rgb.shape[:2]
    background = dominant(rgb[::3, ::3].reshape(-1, 3))
    mask = (np.abs(rgb - background).max(axis=2) > 3).astype(np.uint8)
    # Close anti-aliasing holes only: avatars, bubbles and cards stay apart (their gaps are wider).
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    found = []
    for index in range(1, count):
        x, y, w, h, _ = (int(v) for v in stats[index])
        if w < 16 or h < 16:
            continue
        pixels = rgb[y:y + h, x:x + w][labels[y:y + h, x:x + w] == index]
        colour = dominant(pixels)
        found.append(Block(x, y, x + w, y + h, float((np.abs(pixels - colour).max(axis=1) <= 4).mean()),
                           bool(np.abs(colour - background).max() <= 2), colour=tuple(int(v) for v in colour),
                           fill_ratio=len(pixels) / (w * h)))
    return found, width, height


def side_of(block, width: int) -> str:
    left, right = block.x0 <= .15 * width, block.x1 >= .85 * width
    return "left" if left and not right else "right" if right and not left else "center"


def columns(boxes: list[OcrBox], unit: float) -> bool:
    """Two words side by side with a wide gap: a table or app screen, never one chat bubble."""
    rows = sorted(boxes, key=lambda box: box.cy)
    for a in rows:
        for b in rows:
            if a is not b and abs(a.cy - b.cy) < .5 * min(a.h, b.h) and b.x0 - a.x1 > 2 * unit:
                return True
    return False


def split(image, boxes: list[OcrBox]) -> tuple[list[OcrBox], list[Region], int]:
    """(boxes that are chat text, picture/file placeholders, recognised boxes that were not chat text)."""
    if image is None or image.width < 64 or image.height < 64:
        return list(boxes), [], 0
    found, width, height = blocks(image)
    # A thin viewport outline may surround every bubble. It is not an image
    # region and must not swallow all OCR boxes merely by its bounding rectangle.
    found = [b for b in found if not (b.x0 <= 1 and b.y0 <= 1 and b.x1 >= width - 1
                                      and b.y1 >= height - 1 and b.fill_ratio < .15)]
    for box in boxes:
        holder = min((block for block in found if block.holds(box)), key=lambda b: b.w * b.h, default=None)
        if holder is not None:
            holder.boxes.append(box)
    avatars = [b for b in found if .75 <= b.w / b.h <= 1.33 and b.w <= .1 * width
               and (b.x0 <= .06 * width or b.x1 >= .94 * width)]
    # Chat text is about half an avatar tall (WeChat 4.1: 24-28 px beside 52 px avatars); text in a
    # sent phone screenshot is scaled down to a third of an avatar or less.
    text_height = (median(b.w for b in avatars) * .5 if avatars
                   else median(b.h for b in boxes if b.score >= .8) if any(b.score >= .8 for b in boxes) else None)
    big = text_height * 3.2 if text_height else 80
    dropped: set[int] = set()
    anchored: dict[int, OcrBox] = {}
    regions: list[Region] = []
    for block in found:
        if any(block is avatar for avatar in avatars):
            dropped.update(id(box) for box in block.boxes)
            continue
        if block.on_background:
            continue  # time labels and notices are words on the background, not bubbles
        red, green, blue = block.colour or (0, 0, 0)
        footer = any(box.text.replace(" ", "") == "微信转账" for box in block.boxes)
        amount = any(re.fullmatch(r"[￥¥]\s*\d+(?:[.,]\d{2})?", box.text.strip()) for box in block.boxes)
        status = any(box.text.replace(" ", "") in {"已被接收", "已收款", "已被退还", "已退还", "待收款"}
                     for box in block.boxes)
        orange = red > green + 8 and green > blue + 15
        if footer and (amount or status) and orange and side_of(block, width) != "center":
            regions.append(Region("transfer", side_of(block, width), block.x0, block.y0, block.x1, block.y1))
            dropped.update(id(box) for box in block.boxes)
            continue
        if orange and block.w >= big and block.h >= big and 1.5 < block.w / block.h < 5.5 and side_of(block, width) != "center":
            # Missing/illegible footer or status: retain an opaque card, never
            # convert its amount or button labels into an ordinary chat message.
            regions.append(Region("card", side_of(block, width), block.x0, block.y0, block.x1, block.y1))
            dropped.update(id(box) for box in block.boxes)
            continue
        size = next((box for box in block.boxes if FILE_SIZE.match(box.text.replace(" ", ""))), None)
        above = [box for box in block.boxes if size is not None and box.cy < size.y0 and box.x0 < size.x0 + 2 * size.h]
        if size is not None and above:
            name = "".join(box.text for box in sorted(above, key=lambda box: (box.y0, box.x0))).strip()[:80]
            regions.append(Region("file", side_of(block, width), block.x0, block.y0, block.x1, block.y1, name or None))
            dropped.update(id(box) for box in block.boxes)
            continue
        if not block.boxes:
            # A bubble always carries words; a large wordless block is a picture, sticker or video.
            if block.w >= big and block.h >= big:
                regions.append(Region("image", side_of(block, width), block.x0, block.y0, block.x1, block.y1))
            continue
        unit = text_height or median(box.h for box in block.boxes)
        small = text_height is not None and median(box.h for box in block.boxes) < .72 * text_height
        if block.uniform < .45 or (block.uniform < .9 and small) or (block.uniform < .8 and columns(block.boxes, unit)):
            regions.append(Region("image", side_of(block, width), block.x0, block.y0, block.x1, block.y1))
            dropped.update(id(box) for box in block.boxes)
            continue
        side = side_of(block, width)
        if side != "center":
            # All lines inherit their actual bubble's side and bounds. A short
            # continuation of a wide right-hand bubble must not become B's reply.
            ident = f"{block.x0}:{block.y0}:{block.x1}:{block.y1}"
            for box in block.boxes:
                anchored[id(box)] = replace(box, side=side, bubble_id=ident,
                                            bubble_y0=block.y0, bubble_y1=block.y1)
    return [anchored.get(id(box), box) for box in boxes if id(box) not in dropped], regions, len(dropped)
