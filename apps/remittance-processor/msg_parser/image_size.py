"""Pixel size of an image attachment, read from its header (no imaging library).

Used to skip images Content Understanding cannot analyze: the service requires at least 50 x 50
pixels, and an image that small (a signature icon, a social-media badge) cannot hold a remittance.
Returns None for formats or files it cannot read - those are treated as before.
"""

from __future__ import annotations

import struct


def image_size(content: bytes) -> tuple[int, int] | None:
    """(width, height) in pixels for PNG, GIF, BMP and JPEG, else None."""
    try:
        if content[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", content[16:24])
        if content[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", content[6:10])
        if content[:2] == b"BM":
            width, height = struct.unpack("<ii", content[18:26])
            return width, abs(height)
        if content[:2] == b"\xff\xd8":
            return _jpeg_size(content)
    except struct.error:
        return None
    return None


def _jpeg_size(content: bytes) -> tuple[int, int] | None:
    """Walk the JPEG segments to the first start-of-frame marker, which carries the dimensions."""
    position = 2
    while position + 9 < len(content):
        if content[position] != 0xFF:
            return None
        marker = content[position + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:  # markers without a length
            position += 2
            continue
        length = struct.unpack(">H", content[position + 2 : position + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):  # SOFn
            height, width = struct.unpack(">HH", content[position + 5 : position + 9])
            return width, height
        position += 2 + length
    return None
