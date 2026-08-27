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
from ._util import require_str
from .batch import BatchImportResult, apply_annotation_batch_file
from .constants import DEFAULT_GRAPH_NAME
from .core import DEFAULT_SCHEMA_PATH, USAPPackage
from .domain_vocab import (
    load_citygml_schema,
    load_vocabulary_folder,
    seed_vocabulary_file,
)
from .errors import USAPError
from .geopackage import epsg_from_wkt, set_package_srs


# Every key each block of a project config understands. A config is written by
# hand, so a key the builder does not read is a key whose intent is silently
# dropped -- see _check_keys and the docstring on build_project_package.
_CONFIG_KEYS = frozenset({
    "db_path", "schema_path", "manifest_path",
    "srs_id", "srs_wkt", "validation_level",
    "vocabulary_folder", "citygml_schema", "citygml_schema_version",
    "vocabularies", "relationship_types",
    "citygml", "las", "meshes", "annotation_batches",
})
_CITYGML_KEYS = frozenset({"path", "uri", "compute_hash", "graph_name",
                           "also_usap_default"})
_LAS_KEYS = frozenset({"path", "uri", "compute_hash", "part_path"})
_MESH_KEYS = frozenset({"path", "uri", "representation_name",
                        "representation_kind", "lod", "compute_hash"})
_RELATIONSHIP_KEYS = frozenset({"local_name", "code_space", "category"})


def _check_keys(block: dict[str, Any], known: frozenset[str], *, where: str) -> None:
    """
    Refuse keys this builder does not read.

    Values are already validated where they are used; what escaped was the key
    *name*. Anything starting with '_' is a comment and is skipped.
    """
    unknown = sorted(
        key for key in block
        if not key.startswith("_") and key not in known
    )

    if unknown:
        raise USAPError(
            f"Unrecognised key(s) in {where}: {', '.join(repr(k) for k in unknown)}. "
            f"Known keys are: {', '.join(sorted(known))}. Nothing reads an "
            "unknown key, so leaving it would silently drop whatever it meant; "
            "prefix a key with '_' to keep it as a comment."
        )


@dataclass(frozen=True)
class ProjectBuildResult:
    db_path: Path
    manifest_path: Path | None
    citygml: CityGMLImportResult | None
    las_assets: list[LASRegistrationResult] = field(default_factory=list)
    mesh_assets: list[MeshRegistrationResult] = field(default_factory=list)
    accepted_concept_count: int = 0
    batches: list[BatchImportResult] = field(default_factory=list)


def build_project_package_from_file(
    config_path: str | Path,
    *,
    overwrite: bool = True,
    update: bool = False,
) -> ProjectBuildResult:
    path = Path(config_path)

    if not path.exists():
        raise FileNotFoundError(f"Project config not found: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))

    return build_project_package(
        data,
        base_dir=path.parent,
        overwrite=overwrite,
        update=update,
    )


def build_project_package(
    config: dict[str, Any],
    *,
    base_dir: str | Path = ".",
    overwrite: bool = True,
    update: bool = False,
) -> ProjectBuildResult:
    """
    Build (or, with update=True, extend) a USAP package from a JSON config.

    The config prepares the package so annotation files can target known
    concepts, city objects, and LAS/mesh asset parts; the optional
    "annotation_batches" key then applies those files in the same run
    (see INGESTION.md for the full procedures).

    update=True opens the existing package instead of creating it
    (overwrite is ignored): every build step is idempotent for entries that
    are unchanged — re-listing an already-registered vocabulary or asset is a
    no-op, while re-listing one whose kind, counts, or bounds changed raises
    (see register_asset) — new entries are added, and annotation batches are
    applied with replace_existing=True. This is the editing procedure.

    The whole build is one transaction. A failure part-way leaves no package
    at all for a fresh build, and an untouched one for update=True; without
    that, a build that died after seeding concepts but before registering
    assets left a package that looked real and was not.

    Unrecognised keys raise. A config is hand-written, and a key the builder
    never reads is a key whose intent was never carried out: 'annotation_batch'
    for 'annotation_batches' built a package with no annotations at all, exit
    code 0 and a clean validation report. Keys beginning with '_' are ignored,
    the usual place to put comments in JSON.
    """
    base_path = Path(base_dir)

    _check_keys(config, _CONFIG_KEYS, where="config")

    db_path = _resolve_path(
        require_str(config, "db_path"),
        base_path=base_path,
    )

    schema_path = _resolve_path(
        config.get("schema_path", DEFAULT_SCHEMA_PATH),
        base_path=base_path,
        must_exist=True,
    )

    manifest_path: Path | None = None

    if config.get("manifest_path") is not None:
        manifest_path = _resolve_path(
            require_str(config, "manifest_path"),
            base_path=base_path,
        )

    db_path.parent.mkdir(parents=True, exist_ok=True)

    if manifest_path is not None:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)

    if update:
        pkg_context = USAPPackage.open(db_path)
    else:
        pkg_context = USAPPackage.create(
            db_path,
            schema_path=schema_path,
            overwrite=overwrite,
        )

    config_srs_id = config.get("srs_id")
    validation_level = config.get("validation_level", "deep")

    try:
        with pkg_context as pkg:
            # One transaction around every step: the package is only ever
            # observable as "before this build" or "after it succeeded".
            # transaction() is re-entrant, so the inner per-step blocks
            # become no-ops and commit with this one.
            with pkg.transaction():
                # Declared package CRS wins and is set before registration so
                # extent blobs are encoded with it from the start.
                if config_srs_id is not None:
                    set_package_srs(
                        pkg.conn,
                        int(config_srs_id),
                        definition_wkt=config.get("srs_wkt"),
                    )

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

                # No declared CRS: when the LAS files agree on exactly one
                # EPSG, promote it to the extents layer (single-CRS-per-
                # package assumption). Mixed or no CRS -> undefined (-1).
                if config_srs_id is None:
                    sniffed = {
                        (epsg_from_wkt(item.crs_wkt), item.crs_wkt)
                        for item in las_results
                        if epsg_from_wkt(item.crs_wkt) is not None
                    }

                    if len({code for code, _ in sniffed}) == 1:
                        code, wkt = next(iter(sniffed))
                        set_package_srs(pkg.conn, code, definition_wkt=wkt)

                batch_results = _apply_config_batches(
                    pkg,
                    config=config,
                    base_path=base_path,
                    replace_existing=update,
                )

                concept_count = len(pkg.list_accepted_concepts())

                # Inside the transaction, so a package that fails validation
                # is rolled back rather than left on disk to be opened.
                report = pkg.validate_report(level=validation_level)

                if not report.is_ok:
                    formatted = "\n".join(
                        issue.format() for issue in report.issues
                    )

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
    except BaseException:
        # A rolled-back new package is an empty schema, not a package: the
        # file itself is this build's output and must go with it. An
        # update=True failure rolled back to the previous valid state, which
        # is exactly what should stay on disk.
        if not update and db_path.exists():
            db_path.unlink()

        raise

    return ProjectBuildResult(
        db_path=db_path,
        manifest_path=manifest_path,
        citygml=citygml_result,
        las_assets=las_results,
        mesh_assets=mesh_results,
        accepted_concept_count=concept_count,
        batches=batch_results,
    )


def _seed_config_vocabularies(
    pkg: USAPPackage,
    *,
    config: dict[str, Any],
    base_path: Path,
) -> None:
    """
    Load the package's concept sources, in dependency order.

    Three ways to name them, and **nothing is loaded that the config did not
    ask for**. A package starts with zero concepts; USAP asserts no taxonomy of
    its own, and a build that quietly seeded one would make a config describe
    less than the package contains.

        'vocabulary_folder'  one configuration directory, dispatched by suffix
                             (.xsd, .owl, .json) in a single pass. This is the
                             application startup path (US-DATA-04), so a config
                             using it exercises what the app will really do.
        'citygml_schema'     the OGC CityGML 3.0 XSDs: base classes plus their
                             substitutionGroup hierarchy, which no other
                             artifact carries.
        'vocabularies'       individual JSON concept registries.

    The folder comes first, then the schema, then the JSON files: CityGML's
    hierarchy has to exist before an ADE class can name a CityGML parent.
    Seeding is idempotent and additive, so naming the same concepts twice is
    harmless and only a genuine contradiction raises.
    """
    folder = config.get("vocabulary_folder")

    if folder is not None:
        if not isinstance(folder, str):
            raise ValueError(f"Invalid 'vocabulary_folder' path: {folder!r}")

        load_vocabulary_folder(
            pkg,
            _resolve_path(folder, base_path=base_path, must_exist=True),
            scheme_version=config.get("citygml_schema_version"),
        )

    schema_path = config.get("citygml_schema")

    if schema_path is not None:
        if not isinstance(schema_path, str):
            raise ValueError(f"Invalid 'citygml_schema' path: {schema_path!r}")

        load_citygml_schema(
            pkg,
            _resolve_path(schema_path, base_path=base_path, must_exist=True),
            scheme_version=config.get("citygml_schema_version"),
        )

    # Deliberately no default. Falling back to the ADE registry that ships
    # inside the package used to seed 15 concepts nobody named, which is the
    # one thing this loader must not do -- and which batch.py already avoids.
    vocabularies = config.get("vocabularies", [])

    if not isinstance(vocabularies, list):
        raise ValueError("'vocabularies' must be a list.")

    for item in vocabularies:
        if not isinstance(item, str):
            raise ValueError(f"Invalid vocabulary path: {item!r}")

        seed_vocabulary_file(
            pkg,
            _resolve_path(item, base_path=base_path, must_exist=True),
        )

    _register_config_relationship_types(pkg, config=config)


def _register_config_relationship_types(
    pkg: USAPPackage,
    *,
    config: dict[str, Any],
) -> None:
    """
    Classify the link types this package's builder treats as containment.

    Which CityGML properties mean "part of" is stated in no CityGML artifact —
    not the XSD, not the conceptual model, not an OWL rendering of it — so it
    has to be asserted by whoever builds the package. Without it every edge is
    still recorded and queryable by name, but nothing is a *part*: an
    'and its parts' query returns the object alone, and validate_report()
    warns UNCLASSIFIED_RELATIONSHIP_TYPE.

        "relationship_types": [
            {"local_name": "boundary",
             "code_space": "http://www.opengis.net/citygml/3.0",
             "category": "containment"}
        ]
    """
    items = config.get("relationship_types", [])

    if not isinstance(items, list):
        raise ValueError("'relationship_types' must be a list.")

    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"Invalid relationship type entry: {item!r}")

        _check_keys(item, _RELATIONSHIP_KEYS, where="a 'relationship_types' entry")

        local_name = item.get("local_name")

        if not isinstance(local_name, str) or not local_name:
            raise ValueError(
                f"Relationship type entry needs a 'local_name': {item!r}"
            )

        pkg.register_relationship_type(
            local_name,
            code_space=item.get("code_space"),
            category=item.get("category"),
        )


def _apply_config_batches(
    pkg: USAPPackage,
    *,
    config: dict[str, Any],
    base_path: Path,
    replace_existing: bool,
) -> list[BatchImportResult]:
    items = config.get("annotation_batches", [])

    if not isinstance(items, list):
        raise ValueError("'annotation_batches' must be a list.")

    results: list[BatchImportResult] = []

    for item in items:
        if not isinstance(item, str):
            raise ValueError(f"Invalid annotation batch path: {item!r}")

        results.append(
            apply_annotation_batch_file(
                pkg,
                _resolve_path(item, base_path=base_path, must_exist=True),
                replace_existing=replace_existing,
            )
        )

    return results


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

    _check_keys(citygml, _CITYGML_KEYS, where="the 'citygml' block")

    path = _resolve_path(
        require_str(citygml, "path"),
        base_path=base_path,
        must_exist=True,
    )

    # Refused rather than ignored. The import used to write every edge twice —
    # once into a named import graph, once mirrored into usap_default — and
    # this key switched the mirror off. It now writes one graph always, so a
    # config still carrying the key is asking for behaviour that no longer
    # exists, and silently dropping it would leave the file lying.
    if "also_usap_default" in citygml:
        raise USAPError(
            "'also_usap_default' was removed in profile 0.3.0: the CityGML "
            "import writes a single graph (usap_default by default) instead "
            "of mirroring every edge. Remove the key; set 'graph_name' if you "
            "need the edges somewhere other than usap_default."
        )

    return import_citygml_semantics(
        pkg,
        path,
        uri=citygml.get("uri"),
        compute_hash=bool(citygml.get("compute_hash", True)),
        graph_name=citygml.get("graph_name", DEFAULT_GRAPH_NAME),
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

        _check_keys(item, _LAS_KEYS, where="a 'las' entry")

        path = _resolve_path(
            require_str(item, "path"),
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

        _check_keys(item, _MESH_KEYS, where="a 'meshes' entry")

        path = _resolve_path(
            require_str(item, "path"),
            base_path=base_path,
            must_exist=True,
        )

        result = register_mesh_asset(
            pkg,
            path,
            representation_name=require_str(item, "representation_name"),
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
        LEFT JOIN usap_semantic_class AS sc
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