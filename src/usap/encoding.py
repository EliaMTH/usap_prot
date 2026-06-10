from __future__ import annotations

import struct
import zlib
from collections import defaultdict

from .errors import USAPError


def encode_u32_zlib(offsets: list[int]) -> bytes:
    offsets = sorted(set(offsets))

    for value in offsets:
        if value < 0:
            raise USAPError(f"Negative offset: {value}")
        if value > 2**32 - 1:
            raise USAPError(f"Offset too large for uint32: {value}")

    raw = struct.pack("<" + "I" * len(offsets), *offsets)
    return zlib.compress(raw)


def decode_u32_zlib(payload: bytes) -> list[int]:
    raw = zlib.decompress(payload)

    if len(raw) % 4 != 0:
        raise USAPError("Invalid u32-zlib payload length")

    count = len(raw) // 4

    if count == 0:
        return []

    return list(struct.unpack("<" + "I" * count, raw))


def block_start_for_index(index: int, block_size: int) -> int:
    return (index // block_size) * block_size


def split_indices_into_blocks(
    indices: list[int],
    block_size: int,
) -> dict[int, list[int]]:
    blocks: dict[int, list[int]] = defaultdict(list)

    for index in sorted(set(indices)):
        if index < 0:
            raise USAPError(f"Negative element index: {index}")

        bs = block_start_for_index(index, block_size)
        offset = index - bs
        blocks[bs].append(offset)

    return dict(blocks)
