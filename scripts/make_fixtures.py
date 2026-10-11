"""Generate synthetic fixtures: chats in every supported format + fake WeChat-style screenshots.

All content is invented. Run:  uv run python ../scripts/make_fixtures.py   (from backend/)
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "fixtures" / "synthetic"
SHOTS = OUT / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)
SHOTS.mkdir(parents=True, exist_ok=True)

random.seed(7)
# Dialogue scenes, not random line picks.
#
# The earlier generator chose each A/B line independently from a list, so 「周末有空吗」 was
# followed by whatever came next — including 「感想是？」. That material had no interaction
# structure: the invitation chain read "6 offers, 0 answers" in *every* fixture, and the affinity
# dimensions had nothing to work with but reply latency and message length. Which is why warm and
# cooling landed 7 points apart and a rule rewrite could not move them for an honest reason.
#
# Each fixture now tells one consistent story across its three weeks, so "the other person accepts
# and settles on a time" is a fact of the material rather than of the scoring formula.
WARM_SCENES: list[list[tuple[str, str]]] = [
    [("A", "周末有空吗"), ("B", "有空呀，怎么啦"), ("A", "想约你去看那个展"), ("B", "好啊，周六下午两点？"), ("A", "就周六两点"), ("B", "那我提前买票")],
    [("A", "晚上一起吃饭吗"), ("B", "好啊，你想吃啥"), ("A", "那家新开的川菜"), ("B", "行，六点半我到楼下等你"), ("A", "好，我准时")],
    [("B", "明天降温，你记得多穿点"), ("B", "我顺路给你带杯热的过去"), ("A", "你别跑一趟"), ("B", "顺路啦")],
    [("A", "我下周要出差"), ("B", "几天呀"), ("A", "三天"), ("B", "回来我去接你"), ("A", "那说定了")],
    [("A", "今天有点累"), ("B", "那早点休息，我陪你聊会儿"), ("A", "好")],
    [("B", "上次那本书我看完了"), ("A", "怎么样"), ("B", "好看，借你"), ("A", "那我周末去拿"), ("B", "好，老地方见")],
    [("A", "明天那个展去吗"), ("B", "明天不行，我要陪我妈去医院"), ("A", "那我改到周日"), ("B", "周日可以，下午三点？"), ("A", "好")],
]
COOLING_SCENES: list[list[tuple[str, str]]] = [
    [("A", "周末有空吗"), ("B", "这周有点忙，改天吧"), ("A", "那下周呢"), ("B", "再说吧")],
    [("A", "晚上一起吃饭吗"), ("B", "我今晚要加班"), ("A", "那明天"), ("B", "明天也说不准")],
    [("A", "我下周要出差"), ("B", "哦"), ("A", "回来找你"), ("B", "嗯")],
    [("A", "那本书好看吗"), ("B", "还没看"), ("A", "我看完了，借你？"), ("B", "不用了")],
    [("A", "今天好累"), ("B", "早点睡")],
    [("A", "周末去看那个展吧"), ("B", "我看看安排"), ("A", "上次你也这么说"), ("B", "真的忙")],
    [("A", "下周三一起吃个饭？"), ("B", "下周再说吧，我这阵子事多"), ("A", "好"), ("B", "嗯")],
]
STOP_SCENES: list[list[tuple[str, str]]] = [
    [("A", "周末有空吗"), ("B", "没空"), ("A", "那下周呢"), ("B", "别问了")],
    [("A", "我给你带了早饭"), ("B", "不用了，别这样")],
    [("A", "还在忙吗"), ("B", "我觉得我们不太合适")],
    [("A", "我们再聊聊"), ("B", "别再联系我了")],
    [("A", "对不起"), ("B", "不用道歉，就这样吧")],
    [("A", "你在吗"), ("B", "别发了")],
]
WARM_FILLER = [("A", "早呀"), ("B", "早～"), ("A", "哈哈哈哈"), ("B", "笑死"), ("A", "晚安"), ("B", "晚安，好梦"), ("A", "吃了吗"), ("B", "吃了，你呢")]
COOLING_FILLER = [("A", "早呀"), ("B", "早"), ("A", "在干嘛"), ("B", "忙"), ("A", "晚安"), ("B", "嗯"), ("A", "吃了吗"), ("B", "吃了")]
STOP_FILLER = [("A", "在吗"), ("B", "嗯"), ("A", "晚安"), ("B", "哦"), ("A", "今天好累"), ("B", "嗯")]
SCENES = {"warm": WARM_SCENES, "cooling": COOLING_SCENES, "stop": STOP_SCENES}
FILLER = {"warm": WARM_FILLER, "cooling": COOLING_FILLER, "stop": STOP_FILLER}
# Minutes the other person takes to answer. The cooling fixture also slows down over time, which is
# the one signal the old material *could* express.
REPLY_DELAY = {"warm": (1, 8), "cooling": (2, 20), "stop": (20, 300)}
SEED = 20260916  # fixed: a fixture that changes on every run makes every report incomparable


def gen_pairs(days: int, kind: str = "warm"):
    random.seed(SEED)
    t = datetime(2026, 8, 1, 20, 0)
    scenes, filler = SCENES[kind], FILLER[kind]
    out = []
    for d in range(days):
        base = t + timedelta(days=d)
        ta = base + timedelta(minutes=random.randint(0, 90))
        for who, txt in scenes[d % len(scenes)]:
            if who == "B":
                lo, hi = REPLY_DELAY[kind]
                if kind == "cooling" and d >= days * 0.6:
                    lo, hi = 90, 600  # the shallow end of the conversation: replies stretch out
                ta += timedelta(minutes=random.randint(lo, hi))
            else:
                ta += timedelta(minutes=random.randint(2, 40))
            out.append((who, ta, txt))
        for who, txt in random.sample(filler, k=2):
            ta += timedelta(minutes=random.randint(5, 120))
            out.append((who, ta, txt))
    return sorted(out, key=lambda p: p[1])


def write_paste(name: str, pairs):
    (OUT / name).write_text("\n".join(f"[{ts:%Y-%m-%d %H:%M}] {who}: {txt}" for who, ts, txt in pairs), encoding="utf-8")


def write_whatsapp(name: str, pairs):
    lines = ["13/07/2026, 19:00 - Messages and calls are end-to-end encrypted."]
    for who, ts, txt in pairs:
        n = "小明" if who == "A" else "小红"
        lines.append(f"{ts:%d/%m/%Y}, {ts:%H:%M} - {n}: {txt}")
        if random.random() < 0.05:
            lines.append(f"{ts:%d/%m/%Y}, {ts:%H:%M} - {n}: <Media omitted>")
    (OUT / name).write_text("\n".join(lines), encoding="utf-8")


def write_qq(name: str, pairs):
    lines = ["消息记录（此消息记录为文本格式，不支持查看图片、语音等）", "=" * 64, "消息分组:我的好友", "=" * 64, "消息对象:小红", "=" * 64, ""]
    for who, ts, txt in pairs:
        head = f"{ts:%Y-%m-%d %H:%M:%S} " + ("我的昵称(10001)" if who == "A" else "小红(20002)")
        lines += [head, txt if random.random() > 0.05 else "[图片]", ""]
    (OUT / name).write_text("\n".join(lines), encoding="utf-8")


def write_wechat_csv(name: str, pairs):
    import csv

    with (OUT / name).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["localId", "TalkerId", "Type", "SubType", "IsSender", "CreateTime", "StrTime", "Remark", "NickName", "StrContent"])
        for i, (who, ts, txt) in enumerate(pairs):
            typ = 1 if random.random() > 0.05 else 47
            w.writerow([i, 2, typ, 0, 1 if who == "A" else 0, int(ts.timestamp()), ts.strftime("%Y-%m-%d %H:%M:%S"), "小红", "小红", txt if typ == 1 else ""])


def write_telegram(name: str, pairs):
    msgs = [{"id": i, "type": "message", "date": ts.strftime("%Y-%m-%dT%H:%M:%S"), "from": "小明" if who == "A" else "小红", "from_id": "user1" if who == "A" else "user2", "text": txt} for i, (who, ts, txt) in enumerate(pairs)]
    (OUT / name).write_text(json.dumps({"name": "小红", "type": "personal_chat", "id": 1, "messages": msgs}, ensure_ascii=False, indent=1), encoding="utf-8")


def draw_screenshots(pairs, n_pages: int = 3, overlap: int = 2):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("Pillow not installed; skipping screenshots")
        return
    W, H = 1080, 1900
    font = None
    for cand in ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf", "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc", "/System/Library/Fonts/PingFang.ttc"]:
        if Path(cand).exists():
            font = ImageFont.truetype(cand, 34)
            break
    if font is None:
        print("No CJK font found; skipping screenshots")
        return
    small = ImageFont.truetype(font.path, 26)
    items = pairs[:30]
    per = len(items) // n_pages + overlap
    start = 0
    for p in range(n_pages):
        chunk = items[start : start + per]
        img = Image.new("RGB", (W, H), (237, 237, 237))
        d = ImageDraw.Draw(img)
        y = 60
        last_day = None
        for who, ts, txt in chunk:
            if ts.date() != last_day:
                label = f"{ts.month}月{ts.day}日 {ts:%H:%M}"
                tw = d.textlength(label, font=small)
                d.text(((W - tw) / 2, y), label, fill=(150, 150, 150), font=small)
                y += 60
                last_day = ts.date()
            tw = d.textlength(txt, font=font)
            bw, bh = int(tw) + 48, 72
            if who == "B":
                x0 = 110
                d.rounded_rectangle([x0, y, x0 + bw, y + bh], radius=16, fill=(255, 255, 255))
                d.text((x0 + 24, y + 16), txt, fill=(0, 0, 0), font=font)
            else:
                x0 = W - 110 - bw
                d.rounded_rectangle([x0, y, x0 + bw, y + bh], radius=16, fill=(149, 236, 105))
                d.text((x0 + 24, y + 16), txt, fill=(0, 0, 0), font=font)
            y += bh + 28
            if y > H - 120:
                break
        img.save(SHOTS / f"wechat_{p + 1:02d}.png")
        start += per - overlap
    print(f"screenshots -> {SHOTS}")


def main():
    warm = gen_pairs(21, "warm")
    cooling = gen_pairs(21, "cooling")
    stop = gen_pairs(21, "stop")
    write_paste("paste_ab_warm.txt", warm)
    write_paste("paste_ab_cooling.txt", cooling)
    # The boundary fixture has to *end* on the boundary, so the stop signal is the last thing said
    # rather than whichever scene happened to land on day 21.
    write_paste("paste_ab_stop_contact.txt", stop + [("B", stop[-1][1] + timedelta(minutes=30), "我觉得我们不太合适，别再联系我了")])
    (OUT / "paste_ab_no_time.txt").write_text("\n".join(f"{who}: {txt}" for who, _, txt in warm[:40]), encoding="utf-8")
    write_whatsapp("whatsapp_android.txt", warm)
    write_qq("qq_export.txt", warm)
    write_wechat_csv("wechat_export.csv", warm)
    write_telegram("telegram_result.json", warm)
    draw_screenshots(warm)
    (OUT / "README.md").write_text(
        "# Synthetic fixtures\n\n"
        "All files here are generated by `scripts/make_fixtures.py` with invented names and content. "
        "No real people, no real chats. Use them for demos, tests and bug reports.\n\n"
        "The three paste fixtures tell three different stories on purpose: in `warm` the other person "
        "accepts invitations and settles on times, in `cooling` they re-schedule and deflect, and "
        "`stop_contact` ends on an explicit request to stop. They are generated with a fixed seed, but "
        "**changing the generator changes the material** — numbers in any report written before that "
        "change are not comparable with numbers after it.\n",
        encoding="utf-8",
    )
    print(f"fixtures -> {OUT}")


if __name__ == "__main__":
    main()
