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

# Picked against roaring's container rules, not as a round number.
#
# Roaring stores a chunk as a sorted uint16 array up to 4096 members and as a
# flat 8 KiB bitmap above it. A block only 4096 wide can never hold more than
# 4096 members, so the bitmap container is unreachable and a scattered
# membership falls back to 2 uncompressed bytes per element — *larger* than
# the zlib-compressed uint32 blocks this replaced. 16384 is the narrowest
# width that clears the threshold.
#
# Wider still (32768, 65536) amortizes that fixed 8 KiB bitmap over more
# elements and compresses better again, but block_start is also what the
# reverse query prunes on: at 65536 annotations_for_elements measured 6-10x
# slower than at 4096, worse than the codec this replaced. 16384 keeps the
# reverse query at parity with it while compressing better on every shape.
DEFAULT_BLOCK_SIZE = 16384
DEFAULT_ENCODING = "roaring"

# An annotation is a revisable claim, so its lifecycle state is part of the
# format rather than free text: readers filter on it (list_annotations),
# and an unrecognised value silently drops out of every such filter.
ANNOTATION_STATUSES = ("draft", "accepted", "rejected", "superseded")

# Objects imported from a semantic source are 'accepted'; 'temporary' marks a
# carrier created by a batch, awaiting alignment with a real source object.
CITY_OBJECT_STATUSES = ("accepted", "temporary")

# Confidence is a probability-like score. Anything outside [0, 1] cannot be
# compared against another annotation's, which is the only reason to store it.
CONFIDENCE_RANGE = (0.0, 1.0)

# Only packages written by a profile version this build understands can be
# opened; there is no migration path yet, so opening a newer one would
# silently misread it.
SUPPORTED_PROFILE_VERSIONS = ("0.1.0",)

DEFAULT_GRAPH_NAME = "usap_default"

# usap_city_object_relationship is a *typed* graph, but "an object and its
# parts" is a containment question. These are the edge types descendant
# expansion follows: the four the CityGML adapter emits, each genuinely
# part-of (a window is part of its wall surface, hence 'opening'). Anything
# else (adjacentTo, connectedTo, ...) relates two objects without making one
# part of the other. Callers with their own vocabulary pass containment_types.
CONTAINMENT_RELATIONSHIP_TYPES = (
    "contains",
    "consistsOf",
    "boundedBy",
    "opening",
)

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