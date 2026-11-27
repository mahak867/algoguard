"""Render scripts/demo_week.py's output as a terminal-style animated GIF for the README.

    pip install pillow && python scripts/make_demo_gif.py     # writes docs/demo.gif

The text is produced live by the real AlgoGuard code each time this runs; nothing is typed in by hand.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from scripts.demo_week import Week

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
BG, FG = (14, 22, 33), (214, 222, 232)
COLORS = {"plain": FG, "green": (74, 201, 134), "amber": (242, 162, 58), "red": (238, 99, 99), "blue": (98, 178, 238)}
W, PAD, SIZE, LH, WRAP = 1120, 22, 15, 22, 108


def main(out: str = "docs/demo.gif") -> Path:
    raw = Week().run()
    lines: list[tuple[str, str, bool]] = []
    for i, (text, color) in enumerate(raw):
        indent = "     " if text.startswith("     ") else ""
        for j, part in enumerate(textwrap.wrap(text, WRAP, subsequent_indent=indent + "  ") or [""]):
            lines.append((part, color, i == 0))
    font, bold = ImageFont.truetype(FONT, SIZE), ImageFont.truetype(BOLD, SIZE)
    h = PAD * 2 + 30 + LH * len(lines)

    def frame(n: int) -> Image.Image:
        im = Image.new("RGB", (W, h), BG)
        d = ImageDraw.Draw(im)
        for k, c in enumerate([(238, 99, 99), (242, 162, 58), (74, 201, 134)]):
            d.ellipse((PAD + k * 20, PAD, PAD + 12 + k * 20, PAD + 12), fill=c)
        d.text((PAD + 70, PAD - 3), "python scripts/demo_week.py", font=font, fill=(140, 150, 165))
        for k, (text, color, head) in enumerate(lines[:n]):
            d.text((PAD, PAD + 30 + k * LH), text, font=bold if head else font, fill=COLORS[color])
        return im

    # Reveal one source line at a time (wrapped continuations appear with their line).
    stops, k = [], 0
    for text, _ in raw:
        k += len(textwrap.wrap(text, WRAP) or [""])
        stops.append(k)
    frames = [frame(n).quantize(colors=32, method=Image.Quantize.MEDIANCUT) for n in stops]
    durations = [350 if not raw[i][0].strip() else 900 for i in range(len(raw))]
    durations[-1] = 6000
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=durations, loop=0, optimize=True)
    return path


if __name__ == "__main__":
    p = main()
    print(p, p.stat().st_size // 1024, "KB")
