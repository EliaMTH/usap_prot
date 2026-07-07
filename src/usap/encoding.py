from __future__ import annotations

import struct
import zlib
from collections import defaultdict

import numpy as np

from .constants import VALUE_DTYPES
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


def encode_value_block(values: np.ndarray, value_dtype: str) -> bytes:
    """
    Encode one dense value block: little-endian typed bytes, zlib-compressed.

    `values` must already be numerically valid for `value_dtype` (the caller
    rejects NaN before casting to integer dtypes).
    """
    if value_dtype not in VALUE_DTYPES:
        raise USAPError(f"Unsupported value_dtype: {value_dtype!r}")

    little_endian = np.ascontiguousarray(values, dtype=np.dtype("<" + value_dtype))

    return zlib.compress(little_endian.tobytes())


def decode_value_block(
    payload: bytes,
    value_dtype: str,
    element_count: int,
) -> np.ndarray:
    """
    Decode one value block back to a numpy array of exactly element_count
    values.
    """
    if value_dtype not in VALUE_DTYPES:
        raise USAPError(f"Unsupported value_dtype: {value_dtype!r}")

    try:
        raw = zlib.decompress(payload)
    except zlib.error as exc:
        raise USAPError(f"Corrupt value-block payload: {exc}") from exc

    dtype = np.dtype("<" + value_dtype)

    if len(raw) != element_count * dtype.itemsize:
        raise USAPError(
            f"Value-block payload holds {len(raw) // dtype.itemsize} values, "
            f"declared element_count is {element_count}."
        )

    return np.frombuffer(raw, dtype=dtype)


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
