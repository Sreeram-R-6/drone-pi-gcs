"""Authoritative ASCII 0x20..0x5A, column bytes, bit zero at top."""

import textwrap

GLYPHS = """
0000000000 00005F0000 0007000700 147F147F14 242A7F2A12 2313086462 3649552250 0005030000
001C224100 0041221C00 14083E0814 08083E0808 0050300000 0808080808 0060600000 2010080402
3E5149453E 00427F4000 4261514946 2141454B31 1814127F10 2745454539 3C4A494930 0171090503
3649494936 064949291E 0036360000 0056360000 0814224100 1414141414 0041221408 0201510906
324979413E 7E1111117E 7F49494936 3E41414122 7F4141221C 7F49494941 7F09090901 3E4149497A
7F0808087F 00417F4100 2040413F01 7F08142241 7F40404040 7F020C027F 7F0408107F 3E4141413E
7F09090906 3E4151215E 7F09192946 4649494931 01017F0101 3F4040403F 1F2040201F 3F4038403F
6314081463 0708700807 6151494543
""".split()
FONT = {chr(i + 32): bytes.fromhex(s) for i, s in enumerate(GLYPHS)}
# FC parameter identifiers use underscores; preserve their spelling on screen.
FONT["_"] = bytes([0x40] * 5)


def width(text: str, scale: int = 1) -> int:
    return (len(text) * 6 - 1) * scale if text else 0


def fit(text: str, pixels: int, scale: int = 1) -> bool:
    return all(c in FONT for c in text) and width(text, scale) <= pixels


def compact(text: str, pixels: int, scale: int = 1) -> str:
    text = "".join(c if c in FONT else "?" for c in str(text).upper())
    count = max(0, (pixels // scale + 1) // 6)
    return text if len(text) <= count else (text[: max(0, count - 1)] + ">" if count else "")


def wrap_text(text: str, columns: int = 20) -> list[str]:
    """Keep the complete message, wrapping at words and splitting oversized tokens."""
    clean = "".join(c if c in FONT or c == "\n" else "?" for c in str(text).upper())
    lines = []
    for paragraph in clean.splitlines():
        lines.extend(textwrap.wrap(paragraph, width=columns, break_on_hyphens=False) or [""])
    return lines or [""]
