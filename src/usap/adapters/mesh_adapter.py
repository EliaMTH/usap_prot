from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import trimesh

from ..constants import ELEMENT_KIND_FACE
from ..core import USAPPackage


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


def _sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()

    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)

            if not chunk:
                break

            digest.update(chunk)

    return digest.hexdigest()


def _guess_mesh_media_type(path: Path) -> str:
    suffix = path.suffix.lower()

    if suffix == ".obj":
        return "model/obj"

    if suffix == ".ply":
        return "application/ply"

    if suffix == ".stl":
        return "model/stl"

    if suffix == ".glb":
        return "model/gltf-binary"

    if suffix == ".gltf":
        return "model/gltf+json"

    return "application/octet-stream"


def _safe_bounds(mesh: trimesh.Trimesh) -> tuple[
    float | None,
    float | None,
    float | None,
    float | None,
    float | None,
    float | None,
]:
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
) -> MeshRegistrationResult:
    """
    Register a mesh-like file as USAP face-indexable asset parts.

    Prototype convention:
        one mesh file = one USAP asset
        each Trimesh geometry = one USAP asset part
        face index = zero-based face order in that geometry

    This works for LoD1, LoD2, generic city triangulations, terrain meshes,
    reconstructed surfaces, or any other stable triangular mesh.
    """
    path = Path(mesh_path)

    if not path.exists():
        raise FileNotFoundError(f"Mesh file not found: {path}")

    loaded = trimesh.load(
        str(path),
        process=False,
    )

    geometries = _iter_mesh_geometries(loaded)

    if not geometries:
        raise ValueError(f"No triangular mesh geometries found in: {path}")

    content_hash = _sha256_file(path) if compute_hash else None

    total_face_count = sum(int(len(mesh.faces)) for _, mesh in geometries)

    asset_metadata = {
        "adapter": "mesh_adapter",
        "format": path.suffix.lower().lstrip("."),
        "representation_name": representation_name,
        "representation_kind": representation_kind,
        "lod": lod,
        "total_face_count": total_face_count,
        "geometry_count": len(geometries),
        "indexing": (
            "zero_based_face_order_per_registered_geometry_loaded_with_"
            "trimesh_process_false"
        ),
    }

    asset_id = pkg.register_asset(
        uri=uri if uri is not None else str(path),
        asset_kind="mesh",
        media_type=_guess_mesh_media_type(path),
        content_hash=content_hash,
        metadata_json=json.dumps(asset_metadata),
    )

    parts: list[MeshPartRegistration] = []

    for index, (geometry_name, mesh) in enumerate(geometries):
        face_count = int(len(mesh.faces))

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