from __future__ import annotations

import argparse

from usap import build_project_package_from_file


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a real-project USAP package from a JSON config."
    )

    parser.add_argument("config_json")

    parser.add_argument(
        "--no-overwrite",
        action="store_true",
        help="Fail if the output package already exists.",
    )

    args = parser.parse_args()

    result = build_project_package_from_file(
        args.config_json,
        overwrite=not args.no_overwrite,
    )

    print("Built USAP project package")
    print("  db:", result.db_path)

    if result.manifest_path is not None:
        print("  manifest:", result.manifest_path)

    print()
    print("Summary")
    print("  accepted concepts:", result.accepted_concept_count)

    if result.citygml is not None:
        print("  CityGML objects:", result.citygml.object_count)
        print("  CityGML relationships:", result.citygml.relationship_count)
    else:
        print("  CityGML objects: none")

    print("  LAS assets:", len(result.las_assets))
    print("  mesh assets:", len(result.mesh_assets))

    print()
    print("LAS asset parts")

    for item in result.las_assets:
        print(
            "  asset_id=",
            item.asset_id,
            "asset_part_id=",
            item.asset_part_id,
            "points=",
            item.point_count,
            "path=",
            item.path,
        )

    print()
    print("Mesh asset parts")

    for mesh in result.mesh_assets:
        for part in mesh.parts:
            print(
                "  asset_id=",
                mesh.asset_id,
                "asset_part_id=",
                part.asset_part_id,
                "faces=",
                part.face_count,
                "representation=",
                mesh.representation_name,
                "lod=",
                mesh.lod,
                "part=",
                part.part_path,
            )


if __name__ == "__main__":
    main()