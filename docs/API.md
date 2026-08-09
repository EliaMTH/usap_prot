# USAP API overview

Every public entry point in one place, with one line on what each is for. This
is a map, not a manual: for *why* something behaves as it does, see
[REFERENCE.md](REFERENCE.md); for how to build a package end to end, see
[INGESTION.md](INGESTION.md). Every method's own docstring is longer than the
line here and is the authority when they disagree.

```python
import usap
from usap import USAPPackage
```

Conventions throughout:

- Every `list_*` returns `list[dict]`; every `get_*` returns a `dict` or `None`.
- Anything named `*_id` is an integer primary key; anything named `*_uid` is the
  stable string identity you chose.
- Creates are **idempotent on their natural key** — re-running an import returns
  the existing row instead of duplicating it.
- Errors are `usap.USAPError`, or its subclass `usap.USAPAmbiguityError` when a
  reference matches more than one record.

---

## Opening and creating

| Call | Does |
|---|---|
| `USAPPackage.create(db_path, overwrite=False, package_iri=None)` | create a new package; mints a `urn:uuid:` identity unless you supply one |
| `USAPPackage.open(db_path)` | open an existing one; refuses a profile this build does not understand |
| `pkg.close()` / `with USAPPackage.open(...) as pkg:` | close the connection |
| `pkg.transaction()` | context manager; re-entrant, so grouped edits commit or roll back together |
| `pkg.get_package_iri()` | the package's stable identity |
| `pkg.get_default_block_size()` | the membership block size this package was written with |
| `pkg.log_edit(operation, ...)` | append a row to the edit log (the SDK does this for you on every write) |

## Assets — the geometry side

An **asset** is an external file; an **asset part** is a stable index space
inside it (all LAS points, one mesh geometry). Element indices only mean
anything relative to a part.

| Call | Does |
|---|---|
| `register_asset(uri, asset_kind, media_type=None, content_hash=None, srs_id=None, metadata_json=None)` | register an external file; idempotent on `(uri, content_hash)` |
| `register_asset_part(asset_id, part_path, element_kind, element_count, ..., indexing_profile=None)` | declare an index space and its element count |
| `update_asset(asset_id, uri=..., content_hash=..., srs_id=..., ...)` | repair a record about the same file (a moved path); never re-indexes |
| `list_assets(asset_kind=None)` | registered assets with part and element counts |
| `list_asset_parts(asset_id=None)` | the index spaces of an asset |
| `resolve_asset(asset)` | asset id or uri → `asset_id` |
| `resolve_asset_part(asset_part, part_path=None)` | id, or asset uri (+ part path) → `asset_part_id` |

File adapters, for when you want the counts read off the file instead:

| Call | Does |
|---|---|
| `register_las_asset(pkg, las_path, uri=None, compute_hash=True)` | register a LAS/LAZ cloud; one part, point-record order |
| `register_mesh_asset(pkg, mesh_path, representation_name=..., lod=None, stream=None)` | register an `.obj`/`.ply`/`.stl` mesh; one part per geometry |

> Registering through `register_asset` + `register_asset_part` from the code that
> already loads the mesh for display is the safer path — see "Integrating USAP
> into an application" in REFERENCE.md.

## Concepts — the vocabulary side

A package starts with **zero** concepts. Everything below reads a source you
supply; USAP asserts no taxonomy of its own.

| Call | Does |
|---|---|
| `load_vocabulary_folder(pkg, path)` | seed a whole config directory: `.xsd`, `.owl`/`.ttl`, `.json` — the one-call startup path |
| `load_citygml_schema(pkg, path)` | CityGML concepts **and their hierarchy** from the OGC XSDs |
| `load_ontology(pkg, path)` | link types, their `usap:category`, and ADE classes from an ontology (`.ttl` needs `usap[ttl]`) |
| `seed_vocabulary_file(pkg, path)` | concepts from a JSON registry |
| `seed_default_ade_vocabulary(pkg)` | the shipped ADE prototype registry (an example, not a standard) |
| `create_semantic_class(scheme, class_uri, local_name, parent_class_id=None, ...)` | register one concept by hand; maintains the subclass closure |
| `list_accepted_concepts(scheme=None, search=None, in_use=None)` | the vocabulary picker, with usage counts |
| `resolve_semantic_class(concept, scheme=None)` | name / URI / id → `semantic_class_id`; **raises if unregistered** |
| `get_semantic_class(concept)` / `concept_exists(concept)` | read one concept / test resolvability without raising |
| `city_object_classes(pkg)` / `is_city_object_class(pkg, id)` | which concepts a `.gml` may instantiate as objects (importer's filter) |

## City objects and the link graph

| Call | Does |
|---|---|
| `create_city_object(object_uid, semantic_class_id=None, gml_id=None, object_status="accepted", ...)` | the semantic instance a claim is about; `object_status="temporary"` marks a carrier |
| `list_city_objects(object_status=None, related_to=None, descendants_of=None, direction="out", ...)` | list, or walk the graph one hop (`related_to`) or transitively (`descendants_of`) |
| `resolve_city_object(city_object)` | id, `object_uid`, or `gml_id` → `city_object_id` |
| `link_city_objects(from_id, to_id, relationship_type, to_external_uri=None, role=None, ...)` | one typed directed edge; the target may be outside the package |
| `related_city_objects(city_object, direction="out", ...)` | the **edges** touching an object — the only view showing external targets |
| `register_relationship_type(local_name, code_space=None, category=None)` | declare a link type and whether it means *part of* |
| `list_relationship_types(category=None)` | the link vocabulary with edge counts; `None` also lists unclassified |
| `resolve_relationship_type(relationship_type, code_space=None)` | name (+ namespace) or id → `relationship_type_id` |
| `import_citygml_semantics(pkg, citygml_path, compute_hash=True)` | import object identities, classes and typed relationships from a `.gml` |

## Annotations — the claim

| Call | Does |
|---|---|
| `annotate_elements(concept=..., asset_part_id=..., element_kind=..., element_indices=..., assessed_at=None, ...)` | **the main entry point**: create a claim and attach its geometry in one call |
| `annotate_value_field(concept=..., asset_part_id=..., values=..., assessed_at=None, ...)` | same, for a per-element scalar field instead of a selection |
| `create_concept_annotation(concept=..., city_object_uid=None, ...)` | create a claim with no geometry yet |
| `create_annotation(annotation_uid, semantic_class_id, ...)` | the low-level form, taking a raw class id |
| `get_annotation(annotation_id \| annotation_uid=...)` | one claim, with its assessment / membership / value summaries |
| `list_annotations(status=None, city_object_uid=None, asset_id=None, asset_part_id=None, limit=None, ...)` | filtered list; `asset_id` is how an app loads the annotations of the asset it just opened |
| `update_annotation(annotation_id, status=..., confidence=..., semantic_class_id=..., ...)` | partial update; omitted fields are preserved |
| `delete_annotation(annotation_id, missing_ok=False)` | delete a claim and everything under it |
| `link_annotation_to_object(annotation_id, city_object_id, relation_type="represents")` | add a secondary object link (`concerns`, `derivedFrom`, …) |

## Assessments — one dated evaluation of a claim

An annotation is the logical claim; an assessment is one evaluation of it, at a
date, against one asset. Callers that never mention assessments get one
implicitly and behave exactly as before they existed.

| Call | Does |
|---|---|
| `create_assessment(annotation_id, asset, assessed_at=None, status=..., attributes=None)` | create or reuse one evaluation; idempotent on annotation + asset + date |
| `list_assessments(annotation_id=None, asset_id=None)` | the evaluations of a claim, undated first then by date |
| `get_assessment(assessment_id \| assessment_uid=...)` | one evaluation with its coverage |
| `update_assessment(assessment_id, assessed_at=..., status=..., ...)` | edit metadata; the **asset cannot be changed** |
| `delete_assessment(assessment_id, missing_ok=False)` | remove one evaluation and its blocks; the claim survives |
| `resolve_assessment(assessment)` | id or uid → `assessment_id` |

## Membership — which elements a claim covers

| Call | Does |
|---|---|
| `attach_annotation_elements(annotation_id=..., asset_part_id=..., element_kind=..., element_indices=..., assessment=None)` | set the selection for one part; other parts and other assessments are untouched |
| `replace_annotation_membership(annotation_id, asset_part_id, element_kind, element_indices, assessment=None)` | the same operation under its lower-level name |
| `elements_for_annotation(annotation_id, expand=True, assessment=None, asset_part_id=None)` | forward query: a claim → its element indices |
| `annotations_for_elements(asset_part_id, element_kind, selected_indices, assessment=None)` | **reverse query**: a viewport selection → the claims covering it, one row per (annotation, assessment) |
| `elements_for_city_object(object_uid, include_descendants=True, ...)` | every element an object (and optionally its parts) covers |
| `elements_for_city_objects([object_uids], ...)` | the same over a set, de-duplicated — for walking your own hierarchy |
| `elements_for_semantic_class(semantic_class_id, include_subclasses=True)` | every element annotated under a concept and its subclasses |

## Value fields — one scalar per element

| Call | Does |
|---|---|
| `replace_value_field(annotation_id, asset_part_id, element_kind, values, value_dtype=None, assessment=None)` | write the whole field (v1 requires full coverage; NaN = no value) |
| `values_for_annotation(annotation_id, asset_part_id=None, assessment=None)` | the dense array back |
| `elements_where(annotation_id, predicate, asset_part_id=None, assessment=None)` | element indices matching `(">", 0.5)` or a callable |
| `value_field_stats(annotation_id, asset_part_id=None, assessment=None)` | min / max / count from stored block bounds, without decoding |

## Bulk import

| Call | Does |
|---|---|
| `build_project_package_from_file(config_path, overwrite=True, update=False)` | build a whole package from one JSON config (assets + vocabularies + batches) |
| `build_project_package(config, base_dir=".", update=False)` | the same, from a config already in memory |
| `apply_annotation_batch_file(pkg, path, replace_existing=False)` | apply a JSON annotation batch to an existing package |
| `apply_annotation_batch(pkg, data, replace_existing=False)` | the same, from a dict |
| `create_synthetic_package(db_path, config=...)` | generate a synthetic package for benchmarks and demos |

## Validation and GeoPackage

| Call | Does |
|---|---|
| `pkg.validate_report(level="deep")` | integrity check — `basic` (SQL only), `deep` (+ payloads), `external` (+ files on disk) |
| `validate_connection(conn, level="deep")` | the same against a raw connection |
| `verify_assets(conn)` | per asset: `ok` / `missing` / `changed` / `unhashed` |
| `read_geopackage_header(conn)` | application id and user version, to confirm the file is a GeoPackage |
| `set_package_srs(conn, srs_id, definition_wkt=None)` | declare the package's CRS (re-stamps; never transforms coordinates) |
| `epsg_from_wkt(wkt)` | best-effort EPSG code from a CRS WKT (needs `usap[crs]`) |

---

## Return types

Most calls return plain `dict`s. These are the exceptions — the dataclasses you
will actually hold:

| Type | Fields |
|---|---|
| `ValidationReport` | `issues`; plus `.is_ok`, `.errors`, `.warnings`, `.print()` |
| `ValidationIssue` | `severity`, `code`, `message`, `table`, `row_id`, `details` |
| `MeshRegistrationResult` | `asset_id`, `path`, `representation_name`, `representation_kind`, `lod`, `total_face_count`, `parts`; plus `.primary_asset_part_id` |
| `MeshPartRegistration` | `asset_part_id`, `part_path`, `geometry_name`, `face_count`, bounds |
| `LASRegistrationResult` | `asset_id`, `asset_part_id`, `path`, `point_count`, bounds, `crs_wkt` |
| `CityGMLImportResult` | `asset_id`, `path`, `object_count`, `relationship_count`, `imported_objects`, `imported_relationships`, `unresolved_targets`, `skipped_references`, `warnings` |
| `ImportedCityObject` | `city_object_id`, `object_uid`, `gml_id`, `local_name`, `semantic_class_id` |
| `ImportedRelationship` | `relationship_id`, `from_uid`, `to_uid`, `to_external_uri`, `relationship_type`, `code_space`, `role`, `graph_name` |
| `UnresolvedTarget` | `from_uid`, `relationship_type`, `href` — an xlink leaving the document |
| `VocabularyResult` | `by_name`, `by_uri` — concept name/URI → id |
| `OntologyResult` | `relationship_types`, `concepts`, `categorised`, `imports` |
| `BatchImportResult` | `annotation_count`, `membership_count`, `value_field_count`, `created_city_object_count`, `created_city_object_uids`, `annotations` |
| `BatchAnnotationResult` | `annotation_id`, `annotation_uid`, `concept`, `membership_count`, `value_field_count` |
| `ProjectBuildResult` | `db_path`, `manifest_path`, `citygml`, `las_assets`, `mesh_assets`, `accepted_concept_count`, `batches` |
| `SyntheticConfig` / `SyntheticResult` | generator inputs / what it produced |

## Constants

`ELEMENT_KIND_POINT`, `ELEMENT_KIND_FACE`, `ELEMENT_KIND_VERTEX`,
`ELEMENT_KIND_FEATURE` — element kinds (methods also accept `"point"`/`"face"`).
`VALUE_DTYPES`, `DEFAULT_VALUE_DTYPE` — value-field dtypes.
`DEFAULT_BLOCK_SIZE`, `DEFAULT_ENCODING`, `DEFAULT_GRAPH_NAME`,
`DEFAULT_SCHEMA_PATH`, `VALIDATION_LEVELS`,
`GPKG_APPLICATION_ID`, `GPKG_USER_VERSION`, `USAP_EXTENSION_NAME`.
