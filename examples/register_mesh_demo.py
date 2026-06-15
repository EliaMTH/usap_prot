from __future__ import annotations

import argparse

from usap import (
    ELEMENT_KIND_FACE,
    USAPPackage,
    register_mesh_asset,
    seed_citygml_basic_classes,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Register a mesh and create a prototype face annotation."
    )

    parser.add_argument("mesh_path")

    parser.add_argument(
        "--db",
        default="prototype_mesh.usap.gpkg",
    )

    parser.add_argument(
        "--representation-name",
        default="city_triangulation",
    )

    parser.add_argument(
        "--representation-kind",
        default="triangulated_city_surface",
    )

    parser.add_argument(
        "--lod",
        default=None,
        help="Optional LoD label, e.g. LoD1 or LoD2. Leave empty for generic meshes.",
    )

    args = parser.parse_args()

    with USAPPackage.create(
        args.db,
        schema_path="sql/schema.sql",
        overwrite=True,
    ) as pkg:
        classes = seed_citygml_basic_classes(pkg)

        mesh = register_mesh_asset(
            pkg,
            args.mesh_path,
            representation_name=args.representation_name,
            representation_kind=args.representation_kind,
            lod=args.lod,
        )

        print("Registered mesh:")
        print("  asset_id:", mesh.asset_id)
        print("  representation_name:", mesh.representation_name)
        print("  representation_kind:", mesh.representation_kind)
        print("  lod:", mesh.lod)
        print("  total_face_count:", mesh.total_face_count)
        print("  parts:")

        for part in mesh.parts:
            print(
                "   ",
                part.asset_part_id,
                part.part_path,
                "faces=",
                part.face_count,
            )

        first_part = mesh.parts[0]
        demo_faces = list(range(min(3, first_part.face_count)))

        annotation_id = pkg.create_annotation(
            annotation_uid="ann_demo_mesh_roof_faces",
            semantic_class_id=classes.by_name["RoofSurface"],
            label="Demo mesh roof face annotation",
            status="accepted",
            confidence=1.0,
        )

        pkg.replace_annotation_membership(
            annotation_id=annotation_id,
            asset_part_id=first_part.asset_part_id,
            element_kind=ELEMENT_KIND_FACE,
            element_indices=demo_faces,
        )

        matches = pkg.annotations_for_elements(
            asset_part_id=first_part.asset_part_id,
            element_kind=ELEMENT_KIND_FACE,
            selected_indices=[demo_faces[0]],
        )

        print()
        print("Annotations for selected mesh face:", demo_faces[0])

        for match in matches:
            print("  -", match["annotation_uid"], match["semantic_class"])

        print()
        report = pkg.validate_report()
        report.print()


if __name__ == "__main__":
    main()