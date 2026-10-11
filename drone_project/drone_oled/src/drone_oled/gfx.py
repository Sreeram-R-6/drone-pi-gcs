"""Integer-only monochrome framebuffer primitives."""

from PIL import Image, ImageDraw
from .font import FONT, compact, width

WIFI = ("...#####...", ".##.....##.", "#..#####..#", "..#.....#..", "....###....", ".....#.....")
ETH = (
    "#########",
    "#.......#",
    "#.#.#.#.#",
    "#.#.#.#.#",
    "#.......#",
    "###...###",
    "..#...#..",
    "..#####..",
)
CHECK = ("......#", ".....##", "#...##.", "##.##..", ".###...", "..#....")
CROSS = ("#...#", ".#.#.", "..#..", ".#.#.", "#...#")


class Canvas:
    def __init__(self):
        self.image = Image.new("1", (128, 64))
        self.draw = ImageDraw.Draw(self.image)

    def text(
        self,
        x: int,
        y: int,
        text: str,
        scale: int = 1,
        color: int = 1,
        max_width: int | None = None,
    ) -> None:
        if max_width is not None:
            text = compact(text, max_width, scale)
        for i, char in enumerate(text):
            if char not in FONT:
                raise ValueError(f"Unsupported font character {char!r}")
            for col, bits in enumerate(FONT[char]):
                for row in range(7):
                    if bits & (1 << row):
                        xx, yy = x + (6 * i + col) * scale, y + row * scale
                        self.draw.rectangle((xx, yy, xx + scale - 1, yy + scale - 1), fill=color)

    def center(self, y: int, text: str, scale: int = 1, color: int = 1, pixels: int = 124):
        text = compact(text, pixels, scale)
        self.text((128 - width(text, scale)) // 2, y, text, scale, color)

    def block(self, x: int, y: int, w: int, h: int, filled: bool = False):
        self.draw.rectangle((x, y, x + w - 1, y + h - 1), fill=1 if filled else 0, outline=1)
        for xx, yy in ((x, y), (x + w - 1, y), (x, y + h - 1), (x + w - 1, y + h - 1)):
            self.draw.point((xx, yy), fill=0)
        return 0 if filled else 1

    def icon(self, x: int, y: int, bitmap: tuple, color: int = 1):
        for row, line in enumerate(bitmap):
            for col, char in enumerate(line):
                if char == "#":
                    self.draw.point((x + col, y + row), fill=color)

    def title(self, title: str, page: str = "", back: bool = True, height: int = 10):
        self.draw.rectangle((0, 0, 127, height - 1), fill=1)
        if back:
            self.text(3, 2, "<", color=0)
        reserved = width(page) + 4 if page else 0
        self.text(
            12 if back else 2,
            2,
            title,
            color=0,
            max_width=113 - reserved if back else 123 - reserved,
        )
        if page:
            self.text(125 - width(page), 2, page, color=0)

    def bar(self, x: int, y: int, w: int, h: int, fraction: float):
        self.draw.rectangle((x, y, x + w - 1, y + h - 1), outline=1)
        n = round((w - 4) * max(0, min(1, fraction)))
        if n:
            self.draw.rectangle((x + 2, y + 2, x + 1 + n, y + h - 3), fill=1)

    def scroll(self, index: int, count: int, visible: int, y: int = 12, height: int = 49):
        if count <= visible:
            return
        self.draw.line((125, y, 125, y + height - 1), fill=1)
        thumb = max(6, height * visible // count)
        top = y + round((height - thumb) * index / max(1, count - 1))
        self.draw.rectangle((124, top, 126, top + thumb - 1), fill=1)
