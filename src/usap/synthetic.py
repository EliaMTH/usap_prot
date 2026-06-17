from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .constants import ELEMENT_KIND_FACE
from .core import USAPPackage


@dataclass(frozen=True)
class SyntheticConfig:
    """
    Configuration for a synthetic USAP package.

    This creates fake face indices. It does not create a real mesh file.
    """

    building_count: int = 100
    roof_faces_per_building: int = 120
    wall_faces_per_building: int = 300
    ground_faces_per_building: int = 80
    mesh_uri: str = "synthetic_city_mesh.glb"
    mesh_part_path: str = "node=0/mesh=0/primitive=0"


@dataclass(frozen=True)
class SyntheticResult:
    db_path: Path
    asset_id: int
    asset_part_id: int
    building_class_id: int
    roof_class_id: int
    wall_class_id: int
    ground_class_id: int
    building_count: int
    annotation_count: int
    total_face_count: int


def create_synthetic_package(
    db_path: str | Path,
    schema_path: str | Path = "sql/schema.sql",
    config: SyntheticConfig | None = None,
    overwrite: bool = True,
) -> SyntheticResult:
    """
    Create a synthetic USAP package.

    The generated data model is:

        building_i
          building_i_roof
          building_i_wall
          building_i_ground

    Each surface receives one annotation and a deterministic range of face IDs.
    """

    if config is None:
        config = SyntheticConfig()

    if config.building_count <= 0:
        raise ValueError("building_count must be positive")

    if config.roof_faces_per_building <= 0:
        raise ValueError("roof_faces_per_building must be positive")

    if config.wall_faces_per_building <= 0:
        raise ValueError("wall_faces_per_building must be positive")

    if config.ground_faces_per_building <= 0:
        raise ValueError("ground_faces_per_building must be positive")

    faces_per_building = (
        config.roof_faces_per_building
        + config.wall_faces_per_building
        + config.ground_faces_per_building
    )

    total_face_count = config.building_count * faces_per_building

    db_path = Path(db_path)

    with USAPPackage.create(
        db_path,
        schema_path=schema_path,
        overwrite=overwrite,
    ) as pkg:
        with pkg.transaction():
            # ------------------------------------------------------------
            # 1. One fake external mesh asset
            # ------------------------------------------------------------
            asset_id = pkg.register_asset(
                uri=config.mesh_uri,
                asset_kind="mesh",
                media_type="model/gltf-binary",
                content_hash=(
                    f"synthetic_buildings_{config.building_count}_"
                    f"faces_{total_face_count}"
                ),
            )

            # everything else in the synthetic generation stays indented
            # inside this transaction block
            
            # ------------------------------------------------------------
            # 2. One fake mesh primitive / asset part
            # ------------------------------------------------------------
            asset_part_id = pkg.register_asset_part(
                asset_id=asset_id,
                part_path=config.mesh_part_path,
                element_kind=ELEMENT_KIND_FACE,
                element_count=total_face_count,
            )

            # ------------------------------------------------------------
            # 3. Semantic classes
            # ------------------------------------------------------------
            building_class_id = pkg.create_semantic_class(
                scheme="citygml",
                scheme_version="3.0",
                class_uri="citygml-3.0:building:Building",
                local_name="Building",
            )

            roof_class_id = pkg.create_semantic_class(
                scheme="citygml",
                scheme_version="3.0",
                class_uri="citygml-3.0:building:RoofSurface",
                local_name="RoofSurface",
            )

            wall_class_id = pkg.create_semantic_class(
                scheme="citygml",
                scheme_version="3.0",
                class_uri="citygml-3.0:building:WallSurface",
                local_name="WallSurface",
            )

            ground_class_id = pkg.create_semantic_class(
                scheme="citygml",
                scheme_version="3.0",
                class_uri="citygml-3.0:building:GroundSurface",
                local_name="GroundSurface",
            )

            annotation_count = 0

            # ------------------------------------------------------------
            # 4. Create synthetic city objects, graph edges, annotations,
            #    and membership blocks.
            # ------------------------------------------------------------
            for i in range(config.building_count):
                building_uid = f"building_{i:06d}"
                roof_uid = f"{building_uid}_roof"
                wall_uid = f"{building_uid}_wall"
                ground_uid = f"{building_uid}_ground"

                building_id = pkg.create_city_object(
                    object_uid=building_uid,
                    semantic_class_id=building_class_id,
                )

                roof_id = pkg.create_city_object(
                    object_uid=roof_uid,
                    semantic_class_id=roof_class_id,
                )

                wall_id = pkg.create_city_object(
                    object_uid=wall_uid,
                    semantic_class_id=wall_class_id,
                )

                ground_id = pkg.create_city_object(
                    object_uid=ground_uid,
                    semantic_class_id=ground_class_id,
                )

                # Avoid rebuilding closure after every edge.
                # We rebuild once at the end.
                pkg.link_city_objects(
                    parent_city_object_id=building_id,
                    child_city_object_id=roof_id,
                    relationship_type="boundedBy",
                    role="roof",
                    graph_name="usap_default",
                    rebuild_closure=False,
                )

                pkg.link_city_objects(
                    parent_city_object_id=building_id,
                    child_city_object_id=wall_id,
                    relationship_type="boundedBy",
                    role="wall",
                    graph_name="usap_default",
                    rebuild_closure=False,
                )

                pkg.link_city_objects(
                    parent_city_object_id=building_id,
                    child_city_object_id=ground_id,
                    relationship_type="boundedBy",
                    role="ground",
                    graph_name="usap_default",
                    rebuild_closure=False,
                )

                base = i * faces_per_building

                roof_start = base
                roof_end = roof_start + config.roof_faces_per_building

                wall_start = roof_end
                wall_end = wall_start + config.wall_faces_per_building

                ground_start = wall_end
                ground_end = ground_start + config.ground_faces_per_building

                roof_faces = list(range(roof_start, roof_end))
                wall_faces = list(range(wall_start, wall_end))
                ground_faces = list(range(ground_start, ground_end))

                roof_annotation_id = pkg.create_annotation(
                    annotation_uid=f"ann_{roof_uid}_mesh",
                    semantic_class_id=roof_class_id,
                    primary_city_object_id=roof_id,
                    label=f"Roof annotation for {building_uid}",
                    status="accepted",
                    confidence=1.0,
                )

                pkg.replace_annotation_membership(
                    annotation_id=roof_annotation_id,
                    asset_part_id=asset_part_id,
                    element_kind=ELEMENT_KIND_FACE,
                    element_indices=roof_faces,
                )

                annotation_count += 1

                wall_annotation_id = pkg.create_annotation(
                    annotation_uid=f"ann_{wall_uid}_mesh",
                    semantic_class_id=wall_class_id,
                    primary_city_object_id=wall_id,
                    label=f"Wall annotation for {building_uid}",
                    status="accepted",
                    confidence=1.0,
                )

                pkg.replace_annotation_membership(
                    annotation_id=wall_annotation_id,
                    asset_part_id=asset_part_id,
                    element_kind=ELEMENT_KIND_FACE,
                    element_indices=wall_faces,
                )

                annotation_count += 1

                ground_annotation_id = pkg.create_annotation(
                    annotation_uid=f"ann_{ground_uid}_mesh",
                    semantic_class_id=ground_class_id,
                    primary_city_object_id=ground_id,
                    label=f"Ground annotation for {building_uid}",
                    status="accepted",
                    confidence=1.0,
                )

                pkg.replace_annotation_membership(
                    annotation_id=ground_annotation_id,
                    asset_part_id=asset_part_id,
                    element_kind=ELEMENT_KIND_FACE,
                    element_indices=ground_faces,
                )

                annotation_count += 1

            # One closure rebuild at the end is much faster than one per edge.
            pkg.rebuild_city_object_closure(graph_name="usap_default")

            report = pkg.validate_report()
            if report.issues:
                joined = "\n".join(issue.format() for issue in report.issues)
                raise RuntimeError(f"Synthetic package failed validation:\n{joined}")

    return SyntheticResult(
        db_path=db_path,
        asset_id=asset_id,
        asset_part_id=asset_part_id,
        building_class_id=building_class_id,
        roof_class_id=roof_class_id,
        wall_class_id=wall_class_id,
        ground_class_id=ground_class_id,
        building_count=config.building_count,
        annotation_count=annotation_count,
        total_face_count=total_face_count,
    )