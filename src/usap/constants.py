from typing import Any

ELEMENT_KIND_FACE = 1
ELEMENT_KIND_POINT = 2
ELEMENT_KIND_VERTEX = 3
ELEMENT_KIND_FEATURE = 4

DEFAULT_BLOCK_SIZE = 4096
DEFAULT_ENCODING = "u32-zlib"

DEFAULT_GRAPH_NAME = "usap_default"


def normalize_element_kind(value: Any) -> Any:
    """
    Convert human-readable element kind names to USAP internal constants.

    Accepted user-facing values:
      - "point", "points"
      - "face", "faces", "triangle", "triangles"

    Internal constants are passed through unchanged.
    """
    if isinstance(value, int):
        return value

    if isinstance(value, str):
        text = value.strip().lower()

        if text in {"point", "points"}:
            return ELEMENT_KIND_POINT

        if text in {"face", "faces", "triangle", "triangles"}:
            return ELEMENT_KIND_FACE

        try:
            return int(text)
        except ValueError:
            pass

    raise ValueError(
        f"Unsupported element_kind {value!r}. "
        "Use 'point', 'face', or a USAP element-kind constant."
    )