from __future__ import annotations

import zlib
from collections.abc import Sequence

import numpy as np
from pyroaring import BitMap

from .constants import VALUE_DTYPES
from .errors import USAPError


# Element indices are the one place USAP handles user data at asset scale: a
# selection over a 10 GB point cloud is hundreds of millions of them. They are
# therefore carried as numpy arrays throughout this module — a list of Python
# ints costs ~28 bytes each, and struct.pack("<" + "I" * n) builds a format
# string as long as the data. Public functions still accept and return plain
# lists so callers need not change.
IndexArray = Sequence[int] | np.ndarray

# A value-block payload is decompressed before anything has checked how big it
# will get, so an untrusted package could otherwise hand over a few KB that
# expand to gigabytes. decode_value_block knows its exact expected size and
# passes that instead; this ceiling is the fallback for callers that do not.
#
# Membership payloads no longer need it: roaring is a structural format, not an
# expanding one, so a small payload cannot decode into a large allocation.
MAX_DECOMPRESSED_BYTES = 256 * 1024 * 1024


class _PayloadTooLarge(USAPError):
    """The payload decompressed past the ceiling it was given."""


class _PayloadTruncated(USAPError):
    """The zlib stream ended mid-way."""


def _decompress_bounded(
    payload: bytes,
    max_bytes: int,
    *,
    require_eof: bool = False,
) -> bytes:
    """
    zlib.decompress with a hard ceiling on the output size.

    `require_eof` additionally refuses a *truncated* stream. decompress()
    returns whatever it managed to inflate without raising, so a payload cut
    short mid-stream comes back short and plausible; a caller that then
    compares the length against a declared count reports "wrong count" for what
    is really corruption. Off by default so decode_value_block keeps the exact
    behaviour it shipped with.
    """
    decompressor = zlib.decompressobj()
    raw = decompressor.decompress(payload, max_bytes)

    if decompressor.unconsumed_tail:
        # A subclass, so every existing `except USAPError` still catches it;
        # decode_path needs to tell "longer than declared" (a disagreement with
        # the row) from "will not inflate" (corruption), and they must not
        # arrive as the same message.
        raise _PayloadTooLarge(
            f"Payload decompresses to more than {max_bytes} bytes; refusing "
            "to continue."
        )

    if require_eof and not decompressor.eof:
        raise _PayloadTruncated("Payload is a truncated zlib stream.")

    return raw


def as_index_array(indices: IndexArray) -> np.ndarray:
    """
    Normalize element indices to a sorted, duplicate-free uint32 array.

    Sorting and de-duplication are what the storage format expects, and doing
    them here means every writer gets them for the same cost.

    Deliberately does NOT use np.unique: on numpy 2.4 it costs ~10 s for 10 M
    int64 where np.sort costs ~0.2 s. Sort-then-mask is the same result at
    1/45th the price. An input that is already sorted and duplicate-free
    (which is what every internal caller passes on) skips even that — the
    monotonicity check is a single vectorized pass.
    """
    values = np.asarray(indices)

    if values.size == 0:
        return np.empty(0, dtype=np.uint32)

    if not np.issubdtype(values.dtype, np.integer):
        # Reject 3.5 as an index rather than silently truncating it to 3.
        rounded = np.asarray(values, dtype=np.int64)

        if not np.array_equal(rounded, values):
            raise USAPError("Element indices must be integers.")

        values = rounded

    if values.ndim != 1:
        values = values.reshape(-1)

    # Compared elementwise, not as np.diff(values) > 0: diff subtracts in the
    # input's own dtype, so on an unsigned one a descending step wraps to a
    # huge positive (5 - 500 as uint32 is 4294966801) and every unsigned array
    # read as already sorted, skipping this branch. Also the cheaper of the two.
    if not (values.size == 1 or np.all(values[1:] > values[:-1])):
        values = np.sort(values)
        keep = np.empty(values.size, dtype=bool)
        keep[0] = True
        np.not_equal(values[1:], values[:-1], out=keep[1:])
        values = values[keep]

    if values[0] < 0:
        raise USAPError(f"Negative element index: {int(values[0])}")

    if values[-1] > 2**32 - 1:
        raise USAPError(f"Element index too large for uint32: {int(values[-1])}")

    return values.astype(np.uint32, copy=False)


def as_sequence_array(indices: IndexArray) -> np.ndarray:
    """
    Normalize element indices to a uint32 array, preserving order.

    The deliberate opposite of as_index_array: same bounds rules, but it does
    NOT sort and does NOT de-duplicate. Those two operations are exactly what a
    path stores itself to survive -- a road centreline is faces in traversal
    order, and it may cross the same face twice.

    Never call as_index_array on a path. It would return the right *set* and a
    plausible array, and the feature would be silently gone: a round-trip test
    on an already-ascending path still passes.
    """
    values = np.asarray(indices)

    if values.size == 0:
        return np.empty(0, dtype=np.uint32)

    if not np.issubdtype(values.dtype, np.integer):
        # Reject 3.5 as an index rather than silently truncating it to 3.
        rounded = np.asarray(values, dtype=np.int64)

        if not np.array_equal(rounded, values):
            raise USAPError("Element indices must be integers.")

        values = rounded

    if values.ndim != 1:
        values = values.reshape(-1)

    # Scanned, not values[0] < 0: as_index_array can test the first element
    # alone only because it has already sorted. Here [5, -1] would otherwise
    # pass the check and store 4294967295.
    if np.any(values < 0):
        first = int(values[np.argmax(values < 0)])
        raise USAPError(f"Negative element index: {first}")

    if int(values.max()) > 2**32 - 1:
        raise USAPError(
            f"Element index too large for uint32: {int(values.max())}"
        )

    return values.astype(np.uint32, copy=False)


def encode_path(indices: IndexArray) -> bytes:
    """
    Encode an ordered element sequence as 'u32-seq-zlib'.

    Contiguous little-endian uint32, zlib at the default level, standard zlib
    header, no count prefix -- byte-identical to the historical 'u32-zlib'
    membership layout, which is why a reader that already decodes that one has
    almost nothing to add. What differs is what the values mean: absolute
    element indices, in path order, duplicates allowed.
    """
    sequence = as_sequence_array(indices)

    return zlib.compress(
        np.ascontiguousarray(sequence, dtype=np.dtype("<u4")).tobytes()
    )


def decode_path(payload: bytes, element_count: int | None = None) -> np.ndarray:
    """
    Decode a 'u32-seq-zlib' payload back to element indices, in path order.

    Two modes, and which one a caller wants depends on whether it is *trusting*
    the row or *checking* it:

    - `element_count` given: the declared count is an exact expected size, so
      the decompression ceiling is exact too (as in decode_value_block) and any
      disagreement raises. Every ordinary reader uses this -- handing back a
      sequence whose length contradicts its own row would be worse than failing.

    - `element_count` None: bounded by MAX_DECOMPRESSED_BYTES instead, and no
      count is compared. This is for validation, which is by definition the one
      caller that must tolerate a row lying about its payload, and so the one
      caller that cannot use that row's number as its safety bound. It compares
      the length itself and reports PATH_COUNT_MISMATCH rather than corruption.
    """
    if element_count is not None:
        if element_count < 0:
            raise USAPError(f"Negative element_count: {element_count}")

        # One extra byte already means the payload disagrees with the row.
        ceiling = element_count * 4 + 1
    else:
        ceiling = MAX_DECOMPRESSED_BYTES

    try:
        raw = _decompress_bounded(payload, ceiling, require_eof=True)
    except _PayloadTooLarge as exc:
        # Only reachable in trusting mode, and it is a disagreement with the
        # row rather than corruption -- but the exact count is unknown, because
        # decoding stopped one byte past what the row claimed instead of
        # allocating whatever a hostile payload wanted.
        raise USAPError(
            f"Path payload holds more than {element_count} indices, which is "
            f"its declared element_count."
        ) from exc
    except (zlib.error, USAPError) as exc:
        # _PayloadTruncated from the eof check, zlib's own on a malformed
        # stream. Both are corruption, and both must name the codec.
        raise USAPError(f"Corrupt path payload: {exc}") from exc

    if len(raw) % 4 != 0:
        raise USAPError(
            f"Corrupt path payload: {len(raw)} bytes is not a whole number of "
            "uint32 values."
        )

    if element_count is not None and len(raw) != element_count * 4:
        raise USAPError(
            f"Path payload holds {len(raw) // 4} indices, declared "
            f"element_count is {element_count}."
        )

    return np.frombuffer(raw, dtype=np.dtype("<u4"))


def encode_roaring(offsets: IndexArray) -> bytes:
    """
    Encode within-block offsets as a roaring bitmap.

    The bytes are CRoaring's *portable* serialization — the format the Java,
    Go, C++ and Rust implementations interoperate on — so a membership payload
    is readable outside this SDK rather than being a private blob.

    Roaring picks its own container per block (array / bitmap / run), which is
    the whole point: a wall or roof surface exports as a contiguous face range
    and collapses to a run container of a few bytes, where a compressed uint32
    array of the same run costs kilobytes.
    """
    return BitMap(as_index_array(offsets)).serialize()


def decode_roaring_bitmap(payload: bytes) -> BitMap:
    """
    Decode a roaring payload to a BitMap.

    The reverse query intersects candidate blocks against a selection, and
    roaring intersects two bitmaps natively; going via arrays would throw away
    the structure that makes that cheap.
    """
    try:
        return BitMap.deserialize(payload)
    except Exception as exc:
        # pyroaring raises ValueError for a malformed body and IndexError for
        # a truncated header; neither is worth distinguishing to a caller.
        raise USAPError(f"Corrupt roaring payload: {exc}") from exc


def roaring_to_array(bitmap: BitMap) -> np.ndarray:
    """
    View a BitMap's members as a uint32 array (no per-element Python objects).

    to_array() hands back a C 'I' buffer, so this is a reinterpret rather than
    a copy; np.asarray(bitmap) would build an int64 array element by element
    and costs ~300x more on a full block.
    """
    return np.frombuffer(bitmap.to_array(), dtype=np.uint32)


def decode_roaring_array(payload: bytes) -> np.ndarray:
    """
    Decode a roaring payload to a uint32 array.
    """
    return roaring_to_array(decode_roaring_bitmap(payload))


def decode_roaring(payload: bytes) -> list[int]:
    return decode_roaring_array(payload).tolist()


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

    dtype = np.dtype("<" + value_dtype)

    # The declared element_count is an exact expected size here, so the
    # decompression ceiling can be exact too: one extra byte already means
    # the payload disagrees with the row.
    expected_bytes = element_count * dtype.itemsize

    try:
        raw = _decompress_bounded(payload, expected_bytes + 1)
    except zlib.error as exc:
        raise USAPError(f"Corrupt value-block payload: {exc}") from exc

    if len(raw) != expected_bytes:
        raise USAPError(
            f"Value-block payload holds {len(raw) // dtype.itemsize} values, "
            f"declared element_count is {element_count}."
        )

    return np.frombuffer(raw, dtype=dtype)


def block_start_for_index(index: int, block_size: int) -> int:
    return (index // block_size) * block_size


def split_indices_into_blocks(
    indices: IndexArray,
    block_size: int,
) -> dict[int, np.ndarray]:
    """
    Group element indices by their block, as within-block offsets.

    Returns {block_start: offsets}. Vectorized: the indices arrive sorted from
    as_index_array, so each block is one contiguous slice and the split is a
    search for the boundaries rather than a per-index Python loop.
    """
    values = as_index_array(indices)

    if values.size == 0:
        return {}

    block_starts = (values // block_size) * block_size

    # Sorted input => equal block_starts are adjacent, so the boundaries are
    # exactly the positions where block_start changes.
    boundaries = np.flatnonzero(np.diff(block_starts)) + 1
    groups = np.split(values, boundaries)

    return {
        int(group[0] // block_size) * block_size: (
            group - (group[0] // block_size) * block_size
        )
        for group in groups
    }
