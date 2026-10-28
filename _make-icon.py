# -*- coding: utf-8 -*-
"""生成托盘/快捷方式用的图标：写一份 icon.svg，再栅格化成 icon.ico。

图标寓意：终端提示符 `>` 加一条光标 `_`，蓝青色 = 通道，绿色 = 通。
纯图形无文字，16px 下也认得出。

用法：python _make-icon.py
产出：icon.svg（可编辑的源）、icon.ico（多尺寸）、icon-256.png（预览）
"""

import io
import os
import sys

from PIL import Image, ImageDraw

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))

BG_TOP = (30, 42, 62)
BG_BOT = (13, 20, 31)
EDGE = (52, 70, 100)
CYAN = (53, 214, 232)
GREEN = (61, 220, 132)

S = 1024                      # 4 倍超采样，缩到 16px 才不会糊
R = 56 * 4                    # 圆角
CHEV = [(88, 78), (150, 128), (88, 178)]
CHEV_W = 22
UNDER = [(170, 178), (208, 178)]
UNDER_W = 20

SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#1e2a3e"/>
      <stop offset="1" stop-color="#0d141f"/>
    </linearGradient>
  </defs>
  <rect x="0" y="0" width="256" height="256" rx="56" fill="url(#bg)"/>
  <rect x="1.5" y="1.5" width="253" height="253" rx="55" fill="none"
        stroke="#344664" stroke-width="3"/>
  <path d="M88 78 L150 128 L88 178" fill="none" stroke="#35d6e8"
        stroke-width="22" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="M170 178 H208" fill="none" stroke="#3ddc84"
        stroke-width="20" stroke-linecap="round"/>
</svg>
"""


def build_master():
    # 竖向渐变底
    grad = Image.new('RGB', (S, S))
    gd = ImageDraw.Draw(grad)
    for y in range(S):
        t = y / (S - 1.0)
        # 轻微非线性，底部压得更深，小尺寸下发虚
        t = t ** 0.85
        gd.line([(0, y), (S, y)],
                fill=tuple(int(BG_TOP[i] + (BG_BOT[i] - BG_TOP[i]) * t)
                           for i in range(3)))
    mask = Image.new('L', (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], R, fill=255)
    img = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, S - 1, S - 1], R, outline=EDGE + (255,),
                        width=6)

    # 提示符：折线 + 两端补圆（PIL 只圆化折点，不圆化端点）
    pts = [(x * 4, y * 4) for x, y in CHEV]
    d.line(pts, fill=CYAN + (255,), width=CHEV_W * 4, joint='curve')
    for x, y in (pts[0], pts[-1]):
        rr = CHEV_W * 2
        d.ellipse([x - rr, y - rr, x + rr, y + rr], fill=CYAN + (255,))

    # 光标下划线
    a, b = [(x * 4, y * 4) for x, y in UNDER]
    d.line([a, b], fill=GREEN + (255,), width=UNDER_W * 4)
    for x, y in (a, b):
        rr = UNDER_W * 2
        d.ellipse([x - rr, y - rr, x + rr, y + rr], fill=GREEN + (255,))
    return img


def main():
    svg_path = os.path.join(HERE, 'icon.svg')
    with open(svg_path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(SVG)
    print('svg  %s' % svg_path)

    master = build_master()
    sizes = [256, 128, 96, 64, 48, 40, 32, 24, 20, 16]
    frames = [master.resize((n, n), Image.LANCZOS) for n in sizes]

    ico = os.path.join(HERE, 'icon.ico')
    frames[0].save(ico, format='ICO',
                   sizes=[(n, n) for n in sorted(sizes, reverse=True)])
    print('ico  %s  %d 字节' % (ico, os.path.getsize(ico)))

    png = os.path.join(HERE, 'icon-256.png')
    frames[0].save(png)
    print('png  %s' % png)

    # 16px 的观感先自己看一眼：放大 8 倍存一张，方便检查是否糊成一团
    frames[-1].resize((128, 128), Image.NEAREST).save(
        os.path.join(HERE, 'icon-16x8.png'))
    print('预览 %s' % os.path.join(HERE, 'icon-16x8.png'))


if __name__ == '__main__':
    main()
