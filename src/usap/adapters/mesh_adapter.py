from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from numbers import Integral, Real
from pathlib import Path
from typing import TYPE_CHECKING

from .._util import canonical_hash
from ..constants import ELEMENT_KIND_FACE
from ..core import USAPPackage
from ..errors import USAPError
from .mesh_stream import StreamedMeshPart, read_streamed_parts

if TYPE_CHECKING:
    import trimesh


# Above this size, reading the whole mesh to extract a face count and a
# bounding box stops being reasonable (a 10 GB OBJ needs 10-20 GB of RAM);
# the streaming readers get the same two facts in bounded memory.
MESH_STREAM_THRESHOLD_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class MeshPartRegistration:
    asset_part_id: int
    part_path: str
    geometry_name: str
    face_count: int
    minx: float | None
    miny: float | None
    minz: float | None
    maxx: float | None
    maxy: float | None
    maxz: float | None


@dataclass(frozen=True)
class MeshRegistrationResult:
    asset_id: int
    path: Path
    representation_name: str
    representation_kind: str
    lod: str | None
    total_face_count: int
    parts: list[MeshPartRegistration]

    @property
    def primary_asset_part_id(self) -> int:
        """
        Convenience for the common prototype case where the mesh has one part.

        If the source file contains multiple geometries, this returns the first
        registered part. Use `parts` directly when you need exact control.
        """
        if not self.parts:
            raise ValueError("Mesh registration has no parts.")

        return self.parts[0].asset_part_id


# glTF carries a scene graph: node transforms and repeated instances of one
# geometry. This adapter reads geometries, not scenes, so a translated or
# instanced glTF would be registered with the wrong bounds and with one part
# where the scene has several instances — plausible rows stating something
# false. Refused until scene-graph support exists (see REFERENCE.md).
_UNSUPPORTED_MESH_SUFFIXES = {".glb", ".gltf"}


def _guess_mesh_media_type(path: Path) -> str:
    suffix = path.suffix.lower()

    if suffix == ".obj":
        return "model/obj"

    if suffix == ".ply":
        return "application/ply"

    if suffix == ".stl":
        return "model/stl"

    return "application/octet-stream"


# Which convention assigned the face indices, per format: face i is the i-th
# face record in the file, in file order. Recorded so a later reader can tell
# whether it agrees; what each token means normatively is not yet specified.
_INDEXING_PROFILE_BY_SUFFIX = {
    ".obj": "usap:obj-face-record-order-v1",
    ".ply": "usap:ply-face-record-order-v1",
    ".stl": "usap:stl-face-record-order-v1",
}


def _indexing_profile_for(path: Path) -> str | None:
    return _INDEXING_PROFILE_BY_SUFFIX.get(path.suffix.lower())


def _face_count(mesh: trimesh.Trimesh | StreamedMeshPart) -> int:
    if isinstance(mesh, StreamedMeshPart):
        return mesh.face_count

    return int(len(mesh.faces))


def _safe_bounds(mesh: trimesh.Trimesh | StreamedMeshPart) -> tuple[
    float | None,
    float | None,
    float | None,
    float | None,
    float | None,
    float | None,
]:
    if isinstance(mesh, StreamedMeshPart):
        return (
            mesh.minx, mesh.miny, mesh.minz,
            mesh.maxx, mesh.maxy, mesh.maxz,
        )

    if mesh.bounds is None:
        return None, None, None, None, None, None

    bounds = mesh.bounds

    return (
        float(bounds[0][0]),
        float(bounds[0][1]),
        float(bounds[0][2]),
        float(bounds[1][0]),
        float(bounds[1][1]),
        float(bounds[1][2]),
    )


def _iter_mesh_geometries(
    loaded: object,
) -> list[tuple[str, trimesh.Trimesh]]:
    """
    Return named triangular mesh geometries from a trimesh-loaded object.

    We load with process=False in register_mesh_asset so that trimesh does not
    intentionally clean/reorder the mesh during import.
    """
    import trimesh

    if isinstance(loaded, trimesh.Trimesh):
        return [("default", loaded)]

    if isinstance(loaded, trimesh.Scene):
        items: list[tuple[str, trimesh.Trimesh]] = []

        for name, geometry in loaded.geometry.items():
            if isinstance(geometry, trimesh.Trimesh):
                items.append((str(name), geometry))

        return items

    return []


# (minx, miny, minz, maxx, maxy, maxz); all None when a geometry has none.
_Bounds = tuple[
    float | None, float | None, float | None,
    float | None, float | None, float | None,
]


def _check_mesh_path(path: Path) -> None:
    """The file checks both registration entry points make before anything else."""
    if not path.exists():
        raise FileNotFoundError(f"Mesh file not found: {path}")

    if path.suffix.lower() in _UNSUPPORTED_MESH_SUFFIXES:
        raise USAPError(
            f"glTF-family meshes are not supported: {path.name}. The adapter "
            "reads geometries, not scenes, so node transforms and repeated "
            "instances of one geometry would be dropped — bounds and part "
            "identity would be wrong rather than missing. Export to .ply or "
            ".obj with transforms baked in."
        )


def _write_mesh_registration(
    pkg: USAPPackage,
    path: Path,
    geometries: Sequence[tuple[str, int, _Bounds]],
    *,
    representation_name: str,
    representation_kind: str,
    lod: str | None,
    uri: str | None,
    content_hash: str | None,
    streamed: bool | None,
    facts_source: str | None,
) -> MeshRegistrationResult:
    """
    Write the asset and one part per geometry with faces, from facts already
    known: each geometry's name, face count and bounds.

    Shared by register_mesh_asset, which reads those facts from the file, and
    register_mesh_asset_with_geometries, which takes them from its caller, so
    the two write the same rows and cannot drift apart. facts_source, when
    given, is recorded in the asset's and every part's metadata.
    """
    total_face_count = sum(face_count for _, face_count, _ in geometries)

    asset_metadata = {
        "adapter": "mesh_adapter",
        "format": path.suffix.lower().lstrip("."),
        "representation_name": representation_name,
        "representation_kind": representation_kind,
        "lod": lod,
        "total_face_count": total_face_count,
        "geometry_count": len(geometries),
        "streamed": streamed,
        "indexing": (
            "zero_based_face_order_per_registered_geometry_loaded_with_"
            "trimesh_process_false"
        ),
    }

    if facts_source is not None:
        asset_metadata["facts_source"] = facts_source

    parts: list[MeshPartRegistration] = []

    with pkg.transaction():
        asset_id = pkg.register_asset(
            uri=uri if uri is not None else str(path),
            asset_kind="mesh",
            media_type=_guess_mesh_media_type(path),
            content_hash=content_hash,
            metadata_json=json.dumps(asset_metadata),
        )

        for index, (geometry_name, face_count, bounds) in enumerate(geometries):
            if face_count <= 0:
                continue

            minx, miny, minz, maxx, maxy, maxz = bounds

            part_path = f"geometry/{index}:{geometry_name}"

            part_metadata = {
                "adapter": "mesh_adapter",
                "representation_name": representation_name,
                "representation_kind": representation_kind,
                "lod": lod,
                "geometry_name": geometry_name,
                "geometry_index": index,
                "face_count": face_count,
                "indexing": "zero_based_face_order_in_this_geometry",
                "note": (
                    "Prototype convention: face index means the zero-based face "
                    "position in this registered geometry. The mesh file should "
                    "be treated as immutable after annotation."
                ),
            }

            if facts_source is not None:
                part_metadata["facts_source"] = facts_source

            asset_part_id = pkg.register_asset_part(
                asset_id=asset_id,
                part_path=part_path,
                element_kind=ELEMENT_KIND_FACE,
                element_count=face_count,
                index_origin="zero_based",
                minx=minx,
                miny=miny,
                minz=minz,
                maxx=maxx,
                maxy=maxy,
                maxz=maxz,
                metadata_json=json.dumps(part_metadata),
                indexing_profile=_indexing_profile_for(path),
            )

            parts.append(
                MeshPartRegistration(
                    asset_part_id=asset_part_id,
                    part_path=part_path,
                    geometry_name=geometry_name,
                    face_count=face_count,
                    minx=minx,
                    miny=miny,
                    minz=minz,
                    maxx=maxx,
                    maxy=maxy,
                    maxz=maxz,
                )
            )

        if not parts:
            raise ValueError(f"No mesh parts with faces were registered for: {path}")

    return MeshRegistrationResult(
        asset_id=asset_id,
        path=path,
        representation_name=representation_name,
        representation_kind=representation_kind,
        lod=lod,
        total_face_count=total_face_count,
        parts=parts,
    )


def register_mesh_asset(
    pkg: USAPPackage,
    mesh_path: str | Path,
    *,
    representation_name: str,
    representation_kind: str = "mesh",
    lod: str | None = None,
    uri: str | None = None,
    compute_hash: bool = True,
    stream: bool | None = None,
) -> MeshRegistrationResult:
    """
    Register a mesh file (.obj / .ply / .stl) as USAP face-indexable parts.

    Prototype convention:
        one mesh file = one USAP asset
        each Trimesh geometry = one USAP asset part
        face index = zero-based face order in that geometry

    This works for LoD1, LoD2, generic city triangulations, terrain meshes,
    reconstructed surfaces — any stable triangular mesh whose vertices are
    already in their final coordinates. Formats carrying a scene graph
    (.glb/.gltf) are refused: their node transforms and instancing would be
    silently dropped.

    Registration is the only step that opens the mesh at all: everything
    afterwards (annotating, editing, querying) works from the stored element
    count. It needs just the face count and the bounding box, so files above
    MESH_STREAM_THRESHOLD_BYTES are read in a streaming pass instead of being
    materialized — trimesh.load on a 10 GB city mesh needs 10-20 GB of RAM to
    produce two numbers per part. Pass stream=True/False to decide explicitly;
    see mesh_stream for what the streaming readers do and do not accept.

    compute_hash=True re-reads the whole file to SHA-256 it. That is minutes
    on a 10 GB asset, and it is what makes a later change detectable
    (validate_report(level="external")); pass False to skip it knowingly.
    """
    path = Path(mesh_path)
    _check_mesh_path(path)

    if stream is None:
        stream = path.stat().st_size > MESH_STREAM_THRESHOLD_BYTES

    if stream:
        geometries = [
            (part.name, part)
            for part in read_streamed_parts(path)
        ]
    else:
        import trimesh

        loaded = trimesh.load(
            str(path),
            process=False,
        )

        geometries = _iter_mesh_geometries(loaded)

    if not geometries:
        raise ValueError(f"No triangular mesh geometries found in: {path}")

    content_hash = canonical_hash(path) if compute_hash else None

    return _write_mesh_registration(
        pkg,
        path,
        [
            (geometry_name, _face_count(mesh), _safe_bounds(mesh))
            for geometry_name, mesh in geometries
        ],
        representation_name=representation_name,
        representation_kind=representation_kind,
        lod=lod,
        uri=uri,
        content_hash=content_hash,
        streamed=stream,
        facts_source=None,
    )


def register_mesh_asset_with_geometries(
    pkg: USAPPackage,
    mesh_path: str | Path,
    geometries: Sequence[
        tuple[str, int, tuple[float, float, float, float, float, float] | None]
    ],
    *,
    representation_name: str,
    representation_kind: str = "mesh",
    lod: str | None = None,
    uri: str | None = None,
    compute_hash: bool = True,
) -> MeshRegistrationResult:
    """
    Register a mesh whose geometries the caller has already counted.

    For an application that has the mesh loaded anyway: register_mesh_asset
    would read the whole file again just to count faces and take bounds --
    tens of seconds for a city mesh of a few GB -- while the loader already
    holds both. Here the file is read only to hash it, and not at all with
    compute_hash=False; it must still exist, and glTF is refused as in
    register_mesh_asset.

    Each entry of `geometries` is (name, face_count, bounds), with bounds
    (minx, miny, minz, maxx, maxy, maxz) or None. The rows written are the
    ones register_mesh_asset writes for the same facts -- same part_path
    convention (geometry/{index}:{name}), same indexing_profile per suffix,
    geometries with no faces skipped -- except that the metadata of the asset
    and of every part says "facts_source": "caller", so what the caller
    declared can be told apart from what the SDK read, and the asset's
    "streamed" is null, the file not having been read. Re-registering with the
    same facts returns the same asset; registering the same file through
    register_mesh_asset as well raises, as any re-registration with different
    metadata does.

    **The face count is the index space every membership on the part is
    written against, and nothing here can check it.** The caller guarantees
    the SDK's convention, the one the indexing_profile records: face i is the
    i-th face record of the file, in file order
    (usap:obj-face-record-order-v1 and its .ply / .stl siblings). With quads
    and polygons that means counting records, not the triangles a renderer
    splits them into. A loader that triangulates, reorders or drops faces
    must register through register_asset + register_asset_part with an
    indexing_profile of its own instead.

    Raises USAPError for facts no part could be trusted with -- a name that is
    not a string or repeats, a face count that is not a non-negative integer,
    bounds that are not six finite numbers with each min <= max -- and
    ValueError, like register_mesh_asset, when there is no geometry or none
    has faces.
    """
    path = Path(mesh_path)
    _check_mesh_path(path)

    facts = _validated_geometries(geometries)

    if not facts:
        raise ValueError(f"No triangular mesh geometries found in: {path}")

    content_hash = canonical_hash(path) if compute_hash else None

    return _write_mesh_registration(
        pkg,
        path,
        facts,
        representation_name=representation_name,
        representation_kind=representation_kind,
        lod=lod,
        uri=uri,
        content_hash=content_hash,
        streamed=None,
        facts_source="caller",
    )


def _validated_geometries(
    geometries: Sequence[tuple[str, int, Sequence[float] | None]],
) -> list[tuple[str, int, _Bounds]]:
    """
    The caller's (name, face_count, bounds) entries, checked and normalized
    to Python ints and floats.
    """
    facts: list[tuple[str, int, _Bounds]] = []
    names: set[str] = set()

    for position, entry in enumerate(geometries):
        try:
            name, face_count, bounds = entry
        except (TypeError, ValueError):
            raise USAPError(
                f"Geometry {position} must be (name, face_count, bounds), "
                f"got {entry!r}."
            ) from None

        if not isinstance(name, str):
            raise USAPError(
                f"Geometry {position}: the name must be a string, got {name!r}."
            )

        if name in names:
            raise USAPError(
                f"Geometry name {name!r} appears twice. Names must be unique: "
                "they are how a part is told apart from its siblings."
            )

        names.add(name)

        # bool is an Integral too, and True is not a face count.
        if isinstance(face_count, bool) or not isinstance(face_count, Integral):
            raise USAPError(
                f"Geometry {name!r}: face_count must be an integer, "
                f"got {face_count!r}."
            )

        if face_count < 0:
            raise USAPError(
                f"Geometry {name!r}: face_count cannot be negative, "
                f"got {face_count}."
            )

        facts.append((name, int(face_count), _validated_bounds(name, bounds)))

    return facts


def _validated_bounds(name: str, bounds: Sequence[float] | None) -> _Bounds:
    if bounds is None:
        return (None,) * 6

    values = tuple(bounds) if not isinstance(bounds, (str, bytes)) else ()

    if len(values) != 6 or not all(
        isinstance(value, Real)
        and not isinstance(value, bool)
        and math.isfinite(value)
        for value in values
    ):
        raise USAPError(
            f"Geometry {name!r}: bounds must be six finite numbers "
            f"(minx, miny, minz, maxx, maxy, maxz) or None, got {bounds!r}. "
            "trimesh's (2, 3) mesh.bounds flattens to that order with "
            "tuple(mesh.bounds.ravel())."
        )

    if any(values[axis] > values[axis + 3] for axis in range(3)):
        raise USAPError(
            f"Geometry {name!r}: bounds have a min greater than its max: "
            f"{bounds!r}."
        )

    return tuple(float(value) for value in values)