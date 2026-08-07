from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .._util import sha256_file
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

    content_hash = sha256_file(path) if compute_hash else None

    total_face_count = sum(_face_count(mesh) for _, mesh in geometries)

    asset_metadata = {
        "adapter": "mesh_adapter",
        "format": path.suffix.lower().lstrip("."),
        "representation_name": representation_name,
        "representation_kind": representation_kind,
        "lod": lod,
        "total_face_count": total_face_count,
        "geometry_count": len(geometries),
        "streamed": stream,
        "indexing": (
            "zero_based_face_order_per_registered_geometry_loaded_with_"
            "trimesh_process_false"
        ),
    }

    parts: list[MeshPartRegistration] = []

    with pkg.transaction():
        asset_id = pkg.register_asset(
            uri=uri if uri is not None else str(path),
            asset_kind="mesh",
            media_type=_guess_mesh_media_type(path),
            content_hash=content_hash,
            metadata_json=json.dumps(asset_metadata),
        )

        for index, (geometry_name, mesh) in enumerate(geometries):
            face_count = _face_count(mesh)

            if face_count <= 0:
                continue

            minx, miny, minz, maxx, maxy, maxz = _safe_bounds(mesh)

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