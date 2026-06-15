from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .adapters import (
    CityGMLImportResult,
    LASRegistrationResult,
    MeshRegistrationResult,
    import_citygml_semantics,
    register_las_asset,
    register_mesh_asset,
)
from .core import USAPPackage
from .domain_vocab import seed_vocabulary_file
from .errors import USAPError


@dataclass(frozen=True)
class ProjectBuildResult:
    db_path: Path
    manifest_path: Path | None
    citygml: CityGMLImportResult | None
    las_assets: list[LASRegistrationResult] = field(default_factory=list)
    mesh_assets: list[MeshRegistrationResult] = field(default_factory=list)
    accepted_concept_count: int = 0


def build_project_package_from_file(
    config_path: str | Path,
    *,
    overwrite: bool = True,
) -> ProjectBuildResult:
    path = Path(config_path)

    if not path.exists():
        raise FileNotFoundError(f"Project config not found: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))

    return build_project_package(
        data,
        base_dir=path.parent,
        overwrite=overwrite,
    )


def build_project_package(
    config: dict[str, Any],
    *,
    base_dir: str | Path = ".",
    overwrite: bool = True,
) -> ProjectBuildResult:
    """
    Build a real-project USAP package from a JSON config.

    The builder intentionally does not create annotations.
    It prepares the package so batch annotation files can target known:
      - concepts
      - city objects
      - LAS asset parts
      - mesh asset parts
    """
    base_path = Path(base_dir)

    db_path = _resolve_path(
        _required_str(config, "db_path"),
        base_path=base_path,
    )

    schema_path = _resolve_path(
        config.get("schema_path", "sql/schema.sql"),
        base_path=base_path,
        must_exist=True,
    )

    manifest_path: Path | None = None

    if config.get("manifest_path") is not None:
        manifest_path = _resolve_path(
            _required_str(config, "manifest_path"),
            base_path=base_path,
        )

    db_path.parent.mkdir(parents=True, exist_ok=True)

    if manifest_path is not None:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)

    with USAPPackage.create(
        db_path,
        schema_path=schema_path,
        overwrite=overwrite,
    ) as pkg:
        _seed_config_vocabularies(
            pkg,
            config=config,
            base_path=base_path,
        )

        citygml_result = _import_config_citygml(
            pkg,
            config=config,
            base_path=base_path,
        )

        las_results = _register_config_las(
            pkg,
            config=config,
            base_path=base_path,
        )

        mesh_results = _register_config_meshes(
            pkg,
            config=config,
            base_path=base_path,
        )

        concept_count = len(pkg.list_accepted_concepts())

        report = pkg.validate_report()

        if not report.is_ok:
            formatted = "\n".join(issue.format() for issue in report.issues)

            raise USAPError(
                "Built project package failed validation:\n"
                f"{formatted}"
            )

        if manifest_path is not None:
            manifest = _build_manifest(
                pkg,
                db_path=db_path,
                citygml_result=citygml_result,
                las_results=las_results,
                mesh_results=mesh_results,
            )

            manifest_path.write_text(
                json.dumps(manifest, indent=2),
                encoding="utf-8",
            )

    return ProjectBuildResult(
        db_path=db_path,
        manifest_path=manifest_path,
        citygml=citygml_result,
        las_assets=las_results,
        mesh_assets=mesh_results,
        accepted_concept_count=concept_count,
    )


def _seed_config_vocabularies(
    pkg: USAPPackage,
    *,
    config: dict[str, Any],
    base_path: Path,
) -> None:
    vocabularies = config.get(
        "vocabularies",
        [
            "vocabularies/citygml_3_0_mvp.json",
            "vocabularies/usap_ade_prototype.json",
        ],
    )

    if not isinstance(vocabularies, list):
        raise ValueError("'vocabularies' must be a list.")

    for item in vocabularies:
        if not isinstance(item, str):
            raise ValueError(f"Invalid vocabulary path: {item!r}")

        seed_vocabulary_file(
            pkg,
            _resolve_path(item, base_path=base_path, must_exist=True),
        )


def _import_config_citygml(
    pkg: USAPPackage,
    *,
    config: dict[str, Any],
    base_path: Path,
) -> CityGMLImportResult | None:
    citygml = config.get("citygml")

    if citygml is None:
        return None

    if not isinstance(citygml, dict):
        raise ValueError("'citygml' must be an object when provided.")

    path = _resolve_path(
        _required_str(citygml, "path"),
        base_path=base_path,
        must_exist=True,
    )

    return import_citygml_semantics(
        pkg,
        path,
        uri=citygml.get("uri"),
        compute_hash=bool(citygml.get("compute_hash", True)),
        graph_name=citygml.get("graph_name", "citygml_import"),
        also_usap_default=bool(citygml.get("also_usap_default", True)),
    )


def _register_config_las(
    pkg: USAPPackage,
    *,
    config: dict[str, Any],
    base_path: Path,
) -> list[LASRegistrationResult]:
    items = config.get("las", [])

    if not isinstance(items, list):
        raise ValueError("'las' must be a list.")

    results: list[LASRegistrationResult] = []

    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"Invalid LAS entry: {item!r}")

        path = _resolve_path(
            _required_str(item, "path"),
            base_path=base_path,
            must_exist=True,
        )

        result = register_las_asset(
            pkg,
            path,
            uri=item.get("uri"),
            compute_hash=bool(item.get("compute_hash", True)),
            part_path=item.get("part_path", "points/all"),
        )

        results.append(result)

    return results


def _register_config_meshes(
    pkg: USAPPackage,
    *,
    config: dict[str, Any],
    base_path: Path,
) -> list[MeshRegistrationResult]:
    items = config.get("meshes", [])

    if not isinstance(items, list):
        raise ValueError("'meshes' must be a list.")

    results: list[MeshRegistrationResult] = []

    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"Invalid mesh entry: {item!r}")

        path = _resolve_path(
            _required_str(item, "path"),
            base_path=base_path,
            must_exist=True,
        )

        result = register_mesh_asset(
            pkg,
            path,
            representation_name=_required_str(item, "representation_name"),
            representation_kind=item.get("representation_kind", "mesh"),
            lod=item.get("lod"),
            uri=item.get("uri"),
            compute_hash=bool(item.get("compute_hash", True)),
        )

        results.append(result)

    return results


def _build_manifest(
    pkg: USAPPackage,
    *,
    db_path: Path,
    citygml_result: CityGMLImportResult | None,
    las_results: list[LASRegistrationResult],
    mesh_results: list[MeshRegistrationResult],
) -> dict[str, Any]:
    concepts = pkg.list_accepted_concepts()

    city_objects = pkg.conn.execute(
        """
        SELECT
            co.city_object_id,
            co.object_uid,
            co.gml_id,
            sc.local_name AS semantic_class,
            sc.class_uri AS semantic_class_uri
        FROM usap_city_object AS co
        JOIN usap_semantic_class AS sc
            ON sc.semantic_class_id = co.semantic_class_id
        ORDER BY co.city_object_id
        """
    ).fetchall()

    return {
        "db_path": str(db_path),
        "summary": {
            "accepted_concepts": len(concepts),
            "city_objects": len(city_objects),
            "las_assets": len(las_results),
            "mesh_assets": len(mesh_results),
        },
        "citygml": None
        if citygml_result is None
        else {
            "asset_id": citygml_result.asset_id,
            "path": str(citygml_result.path),
            "object_count": citygml_result.object_count,
            "relationship_count": citygml_result.relationship_count,
        },
        "las": [
            {
                "asset_id": item.asset_id,
                "asset_part_id": item.asset_part_id,
                "path": str(item.path),
                "point_count": item.point_count,
                "element_kind": "point",
                "bounds": {
                    "minx": item.minx,
                    "miny": item.miny,
                    "minz": item.minz,
                    "maxx": item.maxx,
                    "maxy": item.maxy,
                    "maxz": item.maxz,
                },
            }
            for item in las_results
        ],
        "meshes": [
            {
                "asset_id": item.asset_id,
                "path": str(item.path),
                "representation_name": item.representation_name,
                "representation_kind": item.representation_kind,
                "lod": item.lod,
                "total_face_count": item.total_face_count,
                "parts": [
                    {
                        "asset_part_id": part.asset_part_id,
                        "part_path": part.part_path,
                        "geometry_name": part.geometry_name,
                        "face_count": part.face_count,
                        "element_kind": "face",
                        "bounds": {
                            "minx": part.minx,
                            "miny": part.miny,
                            "minz": part.minz,
                            "maxx": part.maxx,
                            "maxy": part.maxy,
                            "maxz": part.maxz,
                        },
                    }
                    for part in item.parts
                ],
            }
            for item in mesh_results
        ],
        "city_objects_sample": [
            dict(row)
            for row in city_objects[:100]
        ],
        "accepted_concepts_sample": concepts[:100],
    }


def _resolve_path(
    value: str | Path,
    *,
    base_path: Path,
    must_exist: bool = False,
) -> Path:
    path = Path(value)

    if path.is_absolute():
        resolved = path

        if must_exist and not resolved.exists():
            raise FileNotFoundError(f"Path does not exist: {resolved}")

        return resolved

    # First interpret relative paths relative to the config file.
    candidate_from_config = base_path / path

    if must_exist:
        if candidate_from_config.exists():
            return candidate_from_config

        # Fallback: allow repo-root-relative paths for tests and scripts.
        if path.exists():
            return path

        raise FileNotFoundError(
            "Path does not exist. Tried both config-relative and "
            f"current-working-directory-relative paths: "
            f"{candidate_from_config} and {path}"
        )

    # For output paths, always use config-relative resolution.
    # The output file usually does not exist yet, so existence-based fallback
    # is wrong here.
    return candidate_from_config


def _required_str(
    data: dict[str, Any],
    key: str,
) -> str:
    value = data.get(key)

    if not isinstance(value, str) or not value:
        raise ValueError(f"Missing required string field: {key}")

    return value