from PIL import Image, ImageDraw

from rapport.desktop.wechat_history import HistoryAssembler
from rapport.ocr.engine import OcrBox
from rapport.ocr.layout import Page, cluster_lines, lines_to_bubbles, pages_to_conversation
from rapport.ocr.regions import split


def box(x0, y0, x1, y1, text):
    return OcrBox(x0, y0, x1, y1, text, .99)


def canvas():
    image = Image.new('RGB', (800, 500), (245, 245, 245))
    return image, ImageDraw.Draw(image)


def test_short_continuation_in_wide_right_bubble_keeps_sender_and_one_message():
    image, draw = canvas()
    draw.rectangle((180, 50, 735, 200), fill=(149, 236, 103))
    rows = [box(205, 65, 700, 90, '这是一条很长的消息'), box(205, 110, 325, 135, '后半句话')]
    kept, media, _ = split(image, rows)
    bubbles, _ = lines_to_bubbles(cluster_lines(kept), 800, media=media)
    assert len(bubbles) == 1 and bubbles[0].side == 'right'
    assert bubbles[0].text == '这是一条很长的消息后半句话'
    assert bubbles[0].y0 <= 50 and bubbles[0].y1 >= 200
    assert all(b.side is None for b in rows), 'Cached raw OCR boxes must remain unmodified'


def test_short_rightward_line_in_left_bubble_keeps_other_sender():
    image, draw = canvas()
    draw.rectangle((60, 60, 660, 200), fill=(255, 255, 255))
    kept, media, _ = split(image, [box(80, 80, 630, 105, '左侧长消息'), box(460, 125, 620, 150, '续行内容')])
    bubbles, _ = lines_to_bubbles(cluster_lines(kept), 800, media=media)
    assert len(bubbles) == 1 and bubbles[0].side == 'left'


def test_distinct_close_bubbles_are_not_merged():
    image, draw = canvas()
    draw.rectangle((590, 50, 735, 90), fill=(149, 236, 103))
    draw.rectangle((590, 97, 735, 137), fill=(149, 236, 103))
    kept, media, _ = split(image, [box(605, 61, 720, 82, '第一条'), box(605, 100, 720, 121, '第二条')])
    bubbles, _ = lines_to_bubbles(cluster_lines(kept), 800, media=media)
    assert [b.text for b in bubbles] == ['第一条', '第二条']


def test_transfer_card_is_a_transfer_placeholder_not_spoken_text():
    image, draw = canvas()
    draw.rectangle((550, 50, 735, 170), fill=(251, 205, 153))
    rows = [box(580, 65, 700, 87, '￥50.00'), box(580, 94, 710, 116, '已收款'), box(565, 137, 695, 159, '微信转账')]
    kept, media, removed = split(image, rows)
    assert kept == [] and removed == 3
    assert [(r.kind, r.placeholder, r.side) for r in media] == [('transfer', '[转账]', 'right')]
    conv = pages_to_conversation([Page('test', kept, 800, 500, media=media)], 'test')
    assert conv.messages[0].type.value == 'transfer' and conv.messages[0].text is None


def test_plain_chat_discussing_transfer_is_not_a_card():
    image, draw = canvas()
    draw.rectangle((550, 50, 735, 170), fill=(149, 236, 103))
    kept, media, _ = split(image, [box(570, 65, 720, 87, '微信转账'), box(570, 100, 700, 122, '￥50.00')])
    assert len(kept) == 2 and not media


def test_incomplete_orange_card_is_not_promoted_to_chat_or_confirmed_transfer():
    image, draw = canvas()
    draw.rectangle((500, 50, 735, 170), fill=(251, 205, 153))
    kept, media, _ = split(image, [box(550, 65, 700, 87, '￥50.00')])
    assert not kept and len(media) == 1 and media[0].kind == 'card'


def test_viewport_outline_does_not_swallow_chat_text():
    image, draw = canvas()
    draw.rectangle((0, 0, 799, 499), outline=(180, 180, 180))
    draw.rectangle((590, 50, 735, 100), fill=(149, 236, 103))
    kept, media, _ = split(image, [box(605, 65, 720, 87, '正常聊天')])
    assert len(kept) == 1 and kept[0].side == 'right' and not media


def test_top_clipped_bubble_cannot_be_counted_as_a_complete_message():
    image, draw = canvas()
    draw.rectangle((180, 0, 735, 180), fill=(149, 236, 103))
    draw.rectangle((70, 260, 270, 310), fill=(255, 255, 255))
    kept, media, _ = split(image, [box(205, 20, 650, 45, '被截断的长消息下半段'), box(85, 275, 255, 300, '完整的新消息')])
    assembler = HistoryAssembler()
    assembler.prepend(Page('test', kept, 800, 500, media=media))
    assert assembler.count == 1 and assembler.rows[0].text == '完整的新消息'
