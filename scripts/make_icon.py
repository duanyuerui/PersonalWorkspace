#!/usr/bin/env python3
"""一次性工具：由 ico.svg 的构图用 Pillow 重绘并生成多尺寸 ico.ico。

    python scripts/make_icon.py
"""
import os
import sys

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def rounded(draw, box, radius, fill):
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def build(size):
    """按 256 基准构图等比绘制。s = size/256。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 256.0

    def sc(v):
        return int(round(v * s))

    # 背景圆角方
    rounded(d, [sc(16), sc(16), sc(240), sc(240)], sc(52), (185, 84, 53, 255))
    # 日历页
    rounded(d, [sc(60), sc(72), sc(196), sc(184)], sc(18), (255, 247, 242, 255))
    # 页头（带圆角，底部用矩形补齐）
    rounded(d, [sc(60), sc(72), sc(196), sc(112)], sc(18), (185, 84, 53, 255))
    d.rectangle([sc(60), sc(94), sc(196), sc(112)], fill=(185, 84, 53, 255))
    # 装订环（延伸进页头）
    rounded(d, [sc(88), sc(56), sc(100), sc(100)], sc(6), (255, 247, 242, 255))
    rounded(d, [sc(156), sc(56), sc(168), sc(100)], sc(6), (255, 247, 242, 255))
    # 对勾
    lw = max(2, sc(18))
    pts = [(sc(96), sc(140)), (sc(120), sc(164)), (sc(164), sc(116))]
    d.line(pts, fill=(185, 84, 53, 255), width=lw, joint="curve")
    for p in (pts[0], pts[-1]):
        r = lw // 2
        d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r],
                  fill=(185, 84, 53, 255))
    return img


def main():
    out = os.path.join(ROOT, "ico.ico")
    imgs = [build(sz) for sz in (256, 128, 64, 48, 32, 24, 16)]
    imgs[0].save(out, sizes=[(i.width, i.height) for i in imgs],
                 append_images=imgs[1:])
    print("已生成 %s (%d bytes)" % (out, os.path.getsize(out)))
    # 预览图，便于肉眼确认
    prev = os.path.join(ROOT, "scripts", "icon-preview.png")
    build(256).save(prev)
    print("预览：%s" % prev)
    return 0


if __name__ == "__main__":
    sys.exit(main())
