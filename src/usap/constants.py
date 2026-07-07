from typing import Any

ELEMENT_KIND_FACE = 1
ELEMENT_KIND_POINT = 2
ELEMENT_KIND_VERTEX = 3
ELEMENT_KIND_FEATURE = 4

_ELEMENT_KINDS = {
    ELEMENT_KIND_FACE,
    ELEMENT_KIND_POINT,
    ELEMENT_KIND_VERTEX,
    ELEMENT_KIND_FEATURE,
}

_ELEMENT_KIND_BY_NAME = {
    "point": ELEMENT_KIND_POINT,
    "points": ELEMENT_KIND_POINT,
    "face": ELEMENT_KIND_FACE,
    "faces": ELEMENT_KIND_FACE,
    "triangle": ELEMENT_KIND_FACE,
    "triangles": ELEMENT_KIND_FACE,
    "vertex": ELEMENT_KIND_VERTEX,
    "vertices": ELEMENT_KIND_VERTEX,
    "feature": ELEMENT_KIND_FEATURE,
    "features": ELEMENT_KIND_FEATURE,
}

DEFAULT_BLOCK_SIZE = 4096
DEFAULT_ENCODING = "u32-zlib"

DEFAULT_GRAPH_NAME = "usap_default"

# Per-element value fields. Dtype tags are numpy-style and stored
# little-endian on disk regardless of the host byte order.
VALUE_DTYPES = frozenset({"u1", "i1", "u2", "i2", "u4", "i4", "f2", "f4", "f8"})
DEFAULT_VALUE_DTYPE = "f4"
VALUE_CHUNK_SIZE = 65536  # elements per value block


def normalize_value_dtype(value: Any) -> str:
    """
    Validate a value-field dtype tag against the supported whitelist.
    """
    if isinstance(value, str):
        tag = value.strip().lower()

        if tag in VALUE_DTYPES:
            return tag

    raise ValueError(
        f"Unsupported value_dtype {value!r}. "
        f"Use one of: {', '.join(sorted(VALUE_DTYPES))}."
    )


def normalize_element_kind(value: Any) -> int:
    """
    Convert human-readable element kind names to USAP internal constants.

    Accepted user-facing values:
      - "point", "points"
      - "face", "faces", "triangle", "triangles"
      - "vertex", "vertices"
      - "feature", "features"
      - the ELEMENT_KIND_* integer constants

    Anything else raises ValueError.
    """
    if isinstance(value, int) and value in _ELEMENT_KINDS:
        return value

    if isinstance(value, str):
        kind = _ELEMENT_KIND_BY_NAME.get(value.strip().lower())

        if kind is not None:
            return kind

    raise ValueError(
        f"Unsupported element_kind {value!r}. "
        "Use 'point', 'face', 'vertex', 'feature', "
        "or a USAP element-kind constant."
    )