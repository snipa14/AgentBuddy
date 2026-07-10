# skills/frontend-review/eval/fixtures/generate_broken_layout.py
"""One-time generator for broken_layout.png -- a minimal, hand-built PNG.

Run directly (`python skills/frontend-review/eval/fixtures/generate_broken_layout.py`)
to regenerate the checked-in fixture. Uses only the stdlib (struct + zlib) so
this doesn't add Pillow as a project dependency just to build one test
fixture. See broken_layout.md for what the image depicts and why.
"""

import struct
import zlib
from pathlib import Path

WIDTH = 240
HEIGHT = 160

WHITE = (255, 255, 255)
CARD_GRAY = (225, 225, 225)
INPUT_GRAY = (200, 200, 200)
ERROR_RED = (220, 40, 40)
BUTTON_BLUE = (40, 90, 220)


def _make_pixel_rows() -> list[bytearray]:
    """Builds a WIDTHxHEIGHT RGB canvas with two intentional layout defects."""
    rows = [bytearray(WHITE * WIDTH) for _ in range(HEIGHT)]

    def fill_rect(x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int]) -> None:
        for y in range(max(y0, 0), min(y1, HEIGHT)):
            for x in range(max(x0, 0), min(x1, WIDTH)):
                rows[y][x * 3 : x * 3 + 3] = bytes(color)

    # The "card" containing a form.
    fill_rect(20, 20, 220, 140, CARD_GRAY)
    # An input field.
    fill_rect(35, 45, 205, 65, INPUT_GRAY)
    # DEFECT 1: an error message overlapping the input field it describes,
    # instead of appearing below it.
    fill_rect(35, 55, 150, 75, ERROR_RED)
    # DEFECT 2: the submit button is positioned mostly off the right edge
    # of the canvas -- only its leftmost sliver is visible/clickable.
    fill_rect(200, 100, 280, 130, BUTTON_BLUE)

    return rows


def write_png(path: Path, width: int, height: int, rows: list[bytearray]) -> None:
    """Writes a minimal, uncompressed-filter RGB PNG using only stdlib zlib/struct."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(
            ">I", zlib.crc32(tag + data)
        )

    raw = b"".join(b"\x00" + bytes(row) for row in rows)
    compressed = zlib.compress(raw, 9)
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", compressed)
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


if __name__ == "__main__":
    write_png(Path(__file__).parent / "broken_layout.png", WIDTH, HEIGHT, _make_pixel_rows())
    print("wrote broken_layout.png")
