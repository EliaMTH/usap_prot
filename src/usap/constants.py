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

# How usap_value_block payloads are compressed (see encode_value_block).
# Membership and value blocks are compressed differently — roaring is a set
# codec and has nothing to say about a dense scalar array — so they name their
# encodings separately.
VALUE_BLOCK_ENCODING = "zlib"

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

# The version stamped on packages this build creates.
#
# 0.3.0 replaced the parent/child edge with a direction-neutral, typed one:
# usap_relationship_type as the link vocabulary, from_/to_city_object_id,
# to_external_uri for an xlink that leaves the document, and traversal driven
# by a relationship category instead of a hardcoded CityGML 2.0 token list.
# CityGML concepts stopped shipping with USAP at the same time; they are read
# from the schema or ontology a package is initialized on.
#
# 0.2.0 added usap_profile.package_iri, the canonical 'algorithm:digest'
# content hash, UTC ISO-8601 timestamps, concept provenance columns, and
# usap_asset_part.indexing_profile.
#
# Neither step has a migration path: the endpoint columns are renamed and the
# type is now a foreign key, so an older package cannot be read as a newer one.
# Packages are experimental and are rebuilt rather than migrated.
CURRENT_PROFILE_VERSION = "0.4.0"

# Only packages written by a profile version this build understands can be
# opened; there is no migration path yet, so opening a newer one would
# silently misread it.
SUPPORTED_PROFILE_VERSIONS = (CURRENT_PROFILE_VERSION,)

DEFAULT_GRAPH_NAME = "usap_default"

# Every CityGML module namespace, in every version, sits under this host path.
# An element outside it (gml:, xAL:, xs:) is not a CityGML concept, and is the
# natural termination of a substitutionGroup chain.
CITYGML_NAMESPACE_MARKER = "opengis.net/citygml"

# The root of the city-object branch. A concept that reaches it by
# substitution is something a .gml can instantiate as a city object; one that
# does not — CityObjectRelation, Role, CityModel, Address, AbstractPointCloud,
# the appearance and versioning classes — is a real CityGML class but never an
# object, and creating one would put a relation object into usap_city_object.
CITY_OBJECT_ROOT_LOCAL_NAME = "AbstractCityObject"

# The real CityGML 3.0 module namespaces, for callers that create a concept by
# hand instead of loading a schema. Spelled out because guessing them is how a
# concept ends up filed under the wrong module: the thematic surfaces belong to
# `construction`, not `building`, and the module tokens are all-lowercase.
CITYGML_3_0_CORE_NS = "http://www.opengis.net/citygml/3.0"
CITYGML_3_0_BUILDING_NS = "http://www.opengis.net/citygml/building/3.0"
CITYGML_3_0_CONSTRUCTION_NS = "http://www.opengis.net/citygml/construction/3.0"

# The handful of classes the synthetic generator and the examples create
# directly. Same identity load_citygml_schema derives, so the two agree and
# a package can hold both without the class arriving twice.
CITYGML_3_0_COMMON_CLASSES = {
    "Building": CITYGML_3_0_BUILDING_NS,
    "BuildingPart": CITYGML_3_0_BUILDING_NS,
    "RoofSurface": CITYGML_3_0_CONSTRUCTION_NS,
    "WallSurface": CITYGML_3_0_CONSTRUCTION_NS,
    "GroundSurface": CITYGML_3_0_CONSTRUCTION_NS,
}


def concept_uri(source_namespace: str, local_name: str) -> str:
    """
    The class_uri form load_citygml_schema derives, as a function.

    A hand-created concept must agree with the schema-derived one, or the same
    class arrives twice under two URIs and resolve_semantic_class starts
    raising on the local name. Build the URI here rather than writing it out.
    """
    return f"{source_namespace}#{local_name}"

# How a link type relates its two endpoints. Stored on usap_relationship_type,
# not on the edge: it is a property of the vocabulary, and every query can
# override it. NULL there means unclassified, which is why this tuple has no
# "unknown" member -- absence is the unclassified state.
#
#   containment     the target is part of the source; what "and its parts"
#                   follows (CityGML boundary, buildingPart, filling, ...)
#   peer            related without either being part of the other
#                   (adjacentTo, predecessor/successor)
#   generalization  the same real-world thing at another level of detail
#                   (generalizesTo)
#   grouping        membership in a user-defined group (groupMember)
#
# This replaces CONTAINMENT_RELATIONSHIP_TYPES, which hardcoded four CityGML
# *2.0* tokens: two of them ('contains', 'opening') are unreachable from any
# CityGML 3.0 property, and the 3.0 properties that do mean part-of were
# recorded and then never traversed.
RELATIONSHIP_CATEGORIES = (
    "containment",
    "peer",
    "generalization",
    "grouping",
)

# What "this object and its parts" means when a query does not say otherwise.
DEFAULT_TRAVERSAL_CATEGORIES = ("containment",)

# Which way an edge is followed. Edges are directed but not hierarchical, so
# the direction is a query argument rather than a property of the data:
# 'out' from_ -> to_, 'in' the reverse, 'both' either way.
RELATIONSHIP_DIRECTIONS = ("out", "in", "both")

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