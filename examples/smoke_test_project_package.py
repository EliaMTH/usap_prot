from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from usap import USAPPackage, apply_annotation_batch


def _load_manifest(path: str | Path) -> dict[str, Any]:
    manifest_path = Path(path)

    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")

    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _find_city_object(
    pkg: USAPPackage,
    *,
    city_object_uid: str | None,
    preferred_class: str,
) -> dict[str, Any]:
    if city_object_uid is not None:
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
               OR co.gml_id = ?
            ORDER BY co.city_object_id
            LIMIT 1
            """,
            (city_object_uid, city_object_uid),
        ).fetchone()

        if row is None:
            raise ValueError(f"City object not found: {city_object_uid}")

        return dict(row)

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
        (preferred_class,),
    ).fetchone()

    if row is not None:
        return dict(row)

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
        ORDER BY co.city_object_id
        LIMIT 1
        """
    ).fetchone()

    if row is None:
        raise ValueError("No city objects found in package.")

    return dict(row)


def _choose_las_part(
    manifest: dict[str, Any],
) -> dict[str, Any]:
    las_items = manifest.get("las", [])

    if not las_items:
        raise ValueError("Manifest contains no LAS assets.")

    item = las_items[0]

    if int(item["point_count"]) <= 0:
        raise ValueError("Selected LAS asset has no points.")

    return item


def _choose_mesh_part(
    manifest: dict[str, Any],
    *,
    representation_name: str | None,
) -> dict[str, Any]:
    meshes = manifest.get("meshes", [])

    if not meshes:
        raise ValueError("Manifest contains no mesh assets.")

    candidates = meshes

    if representation_name is not None:
        candidates = [
            mesh
            for mesh in meshes
            if mesh.get("representation_name") == representation_name
        ]

        if not candidates:
            known = [
                mesh.get("representation_name")
                for mesh in meshes
            ]

            raise ValueError(
                f"No mesh representation named {representation_name!r}. "
                f"Known representations: {known}"
            )

    for mesh in candidates:
        parts = mesh.get("parts", [])

        for part in parts:
            if int(part["face_count"]) > 0:
                return {
                    "mesh": mesh,
                    "part": part,
                }

    raise ValueError("No mesh part with faces found in manifest.")


def _make_indices(
    count: int,
    *,
    max_items: int,
) -> list[int]:
    if count <= 0:
        return []

    return list(range(min(count, max_items)))


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run a real-project USAP smoke test: create one annotation batch, "
            "apply it, query it back, and validate the package."
        )
    )

    parser.add_argument("db")
    parser.add_argument("manifest_json")

    parser.add_argument(
        "--annotation-uid",
        default="ann_smoke_test_001",
    )

    parser.add_argument(
        "--concept",
        default="EnergyRoof",
        help=(
            "Accepted concept to annotate with. Examples: EnergyRoof, "
            "RoofSurface, citygml-3.0:building:RoofSurface."
        ),
    )

    parser.add_argument(
        "--preferred-city-class",
        default="RoofSurface",
        help="Used only when --city-object-uid is omitted.",
    )

    parser.add_argument(
        "--city-object-uid",
        default=None,
        help="Optional exact CityGML object_uid/gml:id to annotate.",
    )

    parser.add_argument(
        "--mesh-representation-name",
        default=None,
        help="Optional mesh representation name from the manifest.",
    )

    parser.add_argument(
        "--point-count",
        type=int,
        default=10,
        help="How many initial LAS point indices to annotate.",
    )

    parser.add_argument(
        "--face-count",
        type=int,
        default=5,
        help="How many initial mesh face indices to annotate.",
    )

    parser.add_argument(
        "--batch-out",
        default=None,
        help="Optional path where the generated smoke batch JSON is written.",
    )

    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help="Replace an existing smoke annotation with the same annotation_uid.",
    )

    args = parser.parse_args()

    manifest = _load_manifest(args.manifest_json)

    with USAPPackage.open(args.db) as pkg:
        city_object = _find_city_object(
            pkg,
            city_object_uid=args.city_object_uid,
            preferred_class=args.preferred_city_class,
        )

        las = _choose_las_part(manifest)

        mesh_choice = _choose_mesh_part(
            manifest,
            representation_name=args.mesh_representation_name,
        )

        mesh = mesh_choice["mesh"]
        mesh_part = mesh_choice["part"]

        point_indices = _make_indices(
            int(las["point_count"]),
            max_items=args.point_count,
        )

        face_indices = _make_indices(
            int(mesh_part["face_count"]),
            max_items=args.face_count,
        )

        if not point_indices:
            raise ValueError("No LAS point indices selected.")

        if not face_indices:
            raise ValueError("No mesh face indices selected.")

        batch = {
            "annotations": [
                {
                    "annotation_uid": args.annotation_uid,
                    "concept": args.concept,
                    "city_object_uid": city_object["object_uid"],
                    "label": "USAP real-project smoke test annotation",
                    "status": "draft",
                    "confidence": 1.0,
                    "attributes": {
                        "source": "smoke_test_project_package.py",
                        "purpose": "end_to_end_mvp_check",
                        "city_object": {
                            "object_uid": city_object["object_uid"],
                            "semantic_class": city_object["semantic_class"],
                        },
                        "selected_assets": {
                            "las_asset_part_id": las["asset_part_id"],
                            "mesh_asset_part_id": mesh_part["asset_part_id"],
                            "mesh_representation_name": mesh["representation_name"],
                            "mesh_lod": mesh.get("lod"),
                        },
                    },
                    "memberships": [
                        {
                            "asset_part_id": int(las["asset_part_id"]),
                            "element_kind": "point",
                            "element_indices": point_indices,
                        },
                        {
                            "asset_part_id": int(mesh_part["asset_part_id"]),
                            "element_kind": "face",
                            "element_indices": face_indices,
                        },
                    ],
                }
            ]
        }

        if args.batch_out is not None:
            batch_out = Path(args.batch_out)
            batch_out.parent.mkdir(parents=True, exist_ok=True)
            batch_out.write_text(
                json.dumps(batch, indent=2),
                encoding="utf-8",
            )

        result = apply_annotation_batch(
            pkg,
            batch,
            replace_existing=args.replace_existing,
        )

        annotation = pkg.get_annotation(
            annotation_uid=args.annotation_uid,
            include_membership_summary=True,
        )

        if annotation is None:
            raise RuntimeError("Smoke annotation was not created.")

        las_matches = pkg.annotations_for_elements(
            asset_part_id=int(las["asset_part_id"]),
            element_kind="point",
            selected_indices=[point_indices[0]],
        )

        mesh_matches = pkg.annotations_for_elements(
            asset_part_id=int(mesh_part["asset_part_id"]),
            element_kind="face",
            selected_indices=[face_indices[0]],
        )

        report = pkg.validate_report()

        print("USAP real-project smoke test")
        print("  db:", args.db)
        print("  manifest:", args.manifest_json)

        if args.batch_out is not None:
            print("  generated batch:", args.batch_out)

        print()
        print("Selected city object")
        print("  object_uid:", city_object["object_uid"])
        print("  semantic_class:", city_object["semantic_class"])

        print()
        print("Selected LAS asset part")
        print("  asset_part_id:", las["asset_part_id"])
        print("  point_count:", las["point_count"])
        print("  tested point index:", point_indices[0])
        print("  annotated point indices:", point_indices)

        print()
        print("Selected mesh asset part")
        print("  representation_name:", mesh["representation_name"])
        print("  lod:", mesh.get("lod"))
        print("  asset_part_id:", mesh_part["asset_part_id"])
        print("  face_count:", mesh_part["face_count"])
        print("  tested face index:", face_indices[0])
        print("  annotated face indices:", face_indices)

        print()
        print("Created/applied annotation")
        print("  annotations applied:", result.annotation_count)
        print("  memberships applied:", result.membership_count)
        print("  annotation_id:", annotation["annotation_id"])
        print("  annotation_uid:", annotation["annotation_uid"])
        print("  concept:", annotation["semantic_class"])
        print("  primary_city_object:", annotation["primary_city_object_uid"])
        print("  membership parts:", len(annotation["membership_summary"]))

        print()
        print("Query check")
        print("  LAS point matches:", len(las_matches))
        for match in las_matches:
            print("    -", match["annotation_uid"], match["semantic_class"])

        print("  mesh face matches:", len(mesh_matches))
        for match in mesh_matches:
            print("    -", match["annotation_uid"], match["semantic_class"])

        if not any(
            match["annotation_uid"] == args.annotation_uid
            for match in las_matches
        ):
            raise RuntimeError("Smoke annotation was not found from LAS query.")

        if not any(
            match["annotation_uid"] == args.annotation_uid
            for match in mesh_matches
        ):
            raise RuntimeError("Smoke annotation was not found from mesh query.")

        print()
        report.print()

        if not report.is_ok:
            raise RuntimeError("Package validation failed after smoke test.")


if __name__ == "__main__":
    main()