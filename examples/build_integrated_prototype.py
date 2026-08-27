from __future__ import annotations

import argparse
import json

import _bootstrap  # noqa: F401  -- puts src/ on sys.path from a checkout

from usap import (
    ELEMENT_KIND_FACE,
    ELEMENT_KIND_POINT,
    USAPPackage,
    import_citygml_semantics,
    register_las_asset,
    register_mesh_asset,
    seed_default_ade_vocabulary,
)


def _find_city_object_by_uid(
    pkg: USAPPackage,
    object_uid: str,
) -> dict:
    row = pkg.conn.execute(
        """
        SELECT
            co.city_object_id,
            co.object_uid,
            co.gml_id,
            sc.local_name AS semantic_class
        FROM usap_city_object AS co
        JOIN usap_semantic_class AS sc
            ON sc.semantic_class_id = co.semantic_class_id
        WHERE co.object_uid = ?
        """,
        (object_uid,),
    ).fetchone()

    if row is None:
        raise ValueError(f"City object not found: {object_uid}")

    return dict(row)


def _find_first_city_object_by_class(
    pkg: USAPPackage,
    local_name: str,
) -> dict:
    row = pkg.conn.execute(
        """
        SELECT
            co.city_object_id,
            co.object_uid,
            co.gml_id,
            sc.local_name AS semantic_class
        FROM usap_city_object AS co
        JOIN usap_semantic_class AS sc
            ON sc.semantic_class_id = co.semantic_class_id
        WHERE sc.local_name = ?
        ORDER BY co.city_object_id
        LIMIT 1
        """,
        (local_name,),
    ).fetchone()

    if row is None:
        raise ValueError(f"No CityGML object found with class: {local_name}")

    return dict(row)


def _annotation_summary(pkg: USAPPackage, annotation_id: int) -> list[dict]:
    rows = pkg.conn.execute(
        """
        SELECT
            mb.asset_part_id,
            ap.part_path,
            ap.element_kind,
            SUM(mb.element_count) AS selected_count,
            COUNT(*) AS block_count
        FROM usap_membership_block AS mb
        JOIN usap_asset_part AS ap
            ON ap.asset_part_id = mb.asset_part_id
        WHERE mb.annotation_id = ?
        GROUP BY mb.asset_part_id, ap.part_path, ap.element_kind
        ORDER BY mb.asset_part_id
        """,
        (annotation_id,),
    ).fetchall()

    return [dict(row) for row in rows]


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Build an integrated USAP prototype package from CityGML, LAS, "
            "and a mesh representation."
        )
    )

    parser.add_argument("citygml_path")
    parser.add_argument("las_path")
    parser.add_argument("mesh_path")

    parser.add_argument(
        "--db",
        default="integrated_prototype.usap.gpkg",
    )

    parser.add_argument(
        "--city-object-uid",
        default=None,
        help=(
            "Optional CityGML object_uid/gml:id to annotate. "
            "If omitted, the first RoofSurface is used."
        ),
    )

    parser.add_argument(
        "--mesh-representation-name",
        default="city_triangulation",
    )

    parser.add_argument(
        "--mesh-representation-kind",
        default="triangulated_city_surface",
    )

    parser.add_argument(
        "--lod",
        default=None,
        help="Optional LoD label such as LoD1 or LoD2.",
    )

    args = parser.parse_args()

    with USAPPackage.create(
        args.db,
        overwrite=True,
    ) as pkg:
        citygml = import_citygml_semantics(
            pkg,
            args.citygml_path,
        )

        las = register_las_asset(
            pkg,
            args.las_path,
        )

        mesh = register_mesh_asset(
            pkg,
            args.mesh_path,
            representation_name=args.mesh_representation_name,
            representation_kind=args.mesh_representation_kind,
            lod=args.lod,
        )

        ade_classes = seed_default_ade_vocabulary(pkg)

        if args.city_object_uid is not None:
            city_object = _find_city_object_by_uid(
                pkg,
                args.city_object_uid,
            )
        else:
            city_object = _find_first_city_object_by_class(
                pkg,
                "RoofSurface",
            )

        # Attributes hold claim-level metadata only (how/when/by what this
        # annotation was produced). Object properties (footprint, height,
        # use, construction era, ...) belong to the CityGML/ADE source,
        # reachable through the primary city object — never duplicated into
        # USAP, so there is exactly one authority for them.
        energy_attributes = {
            "domain": "energy_emissions",
            "method": "integrated_prototype_demo",
            "assessed_at": "2026-06-30T14:00:00Z",
        }

        annotation_id = pkg.create_annotation(
            annotation_uid=f"ann_energy_{city_object['object_uid']}",
            semantic_class_id=ade_classes.by_name["EnergyRoof"],
            primary_city_object_id=int(city_object["city_object_id"]),
            status="draft",
            confidence=None,
            attributes_json=json.dumps(energy_attributes),
        )

        las_point_indices = list(range(min(10, las.point_count)))

        if not las_point_indices:
            raise ValueError("LAS file contains no points.")

        pkg.replace_annotation_membership(
            annotation_id=annotation_id,
            asset_part_id=las.asset_part_id,
            element_kind=ELEMENT_KIND_POINT,
            element_indices=las_point_indices,
        )

        mesh_part = mesh.parts[0]
        mesh_face_indices = list(range(min(5, mesh_part.face_count)))

        if not mesh_face_indices:
            raise ValueError("Mesh part contains no faces.")

        pkg.replace_annotation_membership(
            annotation_id=annotation_id,
            asset_part_id=mesh_part.asset_part_id,
            element_kind=ELEMENT_KIND_FACE,
            element_indices=mesh_face_indices,
        )

        las_matches = pkg.annotations_for_elements(
            asset_part_id=las.asset_part_id,
            element_kind=ELEMENT_KIND_POINT,
            selected_indices=[las_point_indices[0]],
        )

        mesh_matches = pkg.annotations_for_elements(
            asset_part_id=mesh_part.asset_part_id,
            element_kind=ELEMENT_KIND_FACE,
            selected_indices=[mesh_face_indices[0]],
        )

        print("Integrated USAP prototype created")
        print("  db:", args.db)
        print()
        print("CityGML")
        print("  source asset_id:", citygml.asset_id)
        print("  imported objects:", citygml.object_count)
        print("  imported relationships:", citygml.relationship_count)
        print()
        print("Selected CityGML object")
        print("  city_object_id:", city_object["city_object_id"])
        print("  object_uid:", city_object["object_uid"])
        print("  semantic_class:", city_object["semantic_class"])
        print()
        print("LAS")
        print("  asset_id:", las.asset_id)
        print("  asset_part_id:", las.asset_part_id)
        print("  point_count:", las.point_count)
        print("  annotated point indices:", las_point_indices)
        print()
        print("Mesh")
        print("  asset_id:", mesh.asset_id)
        print("  representation_name:", mesh.representation_name)
        print("  representation_kind:", mesh.representation_kind)
        print("  lod:", mesh.lod)
        print("  selected part:", mesh_part.asset_part_id, mesh_part.part_path)
        print("  total_face_count:", mesh.total_face_count)
        print("  annotated face indices:", mesh_face_indices)
        print()
        print("Annotation")
        print("  annotation_id:", annotation_id)
        print("  annotation_uid:", f"ann_energy_{city_object['object_uid']}")
        print("  semantic_class: EnergyRoof")
        print("  primary_city_object:", city_object["object_uid"])
        print()
        print("Membership summary")

        for row in _annotation_summary(pkg, annotation_id):
            print(
                " ",
                "asset_part_id=",
                row["asset_part_id"],
                "part_path=",
                row["part_path"],
                "element_kind=",
                row["element_kind"],
                "selected_count=",
                row["selected_count"],
                "blocks=",
                row["block_count"],
            )

        print()
        print("Query from selected LAS point:")

        for match in las_matches:
            print("  -", match["annotation_uid"], match["semantic_class"])

        print()
        print("Query from selected mesh face:")

        for match in mesh_matches:
            print("  -", match["annotation_uid"], match["semantic_class"])

        print()
        report = pkg.validate_report()
        report.print()

        annotation = pkg.get_annotation(
            annotation_id,
            include_membership_summary=True,
        )

        print()
        print("CRUD readback")
        print("  annotation_uid:", annotation["annotation_uid"])
        print("  semantic_class:", annotation["semantic_class"])
        print("  status:", annotation["status"])
        print("  membership parts:", len(annotation["membership_summary"]))


if __name__ == "__main__":
    main()

# python examples/build_integrated_prototype.py \
# city_JSON.gml \
# decimated_classified_output.las \
# city_mesh.obj \
# --db integrated_prototype.usap.gpkg \
# --mesh-representation-name city_triangulation \
# --mesh-representation-kind triangulated_city_surface