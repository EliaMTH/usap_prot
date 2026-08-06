# USAP test suite

What each test protects and why. Run everything with:

```bash
python -m pytest
```

(pytest is configured in `pyproject.toml`: `pythonpath=["src"]`,
`testpaths=["tests"]` — run it from the repo root.)

## Shared helpers and fixtures (`tests/conftest.py`)

- `SCHEMA_PATH` — the packaged `src/usap/data/schema.sql` (re-exported from
  `usap.DEFAULT_SCHEMA_PATH`, so the suite passes against an installed wheel).
- `make_pkg(tmp_path, name=...)` — create a fresh empty package.
- `make_mesh_part(pkg, element_count=...)` — register a bare mesh asset with
  one face part; returns the `asset_part_id`.
- `pkg` / `mesh_part` — pytest fixtures wrapping the two helpers.
- `assert_package_valid(pkg)` — assert `validate_report()` is clean, printing
  the issues on failure.
- `write_tiny_las(path, point_count)` / `write_tiny_mesh(path)` — minimal real
  LAS / 2-triangle mesh files for adapter and end-to-end tests.

## Core data model — `test_core.py`

- `test_selected_face_returns_roof_annotation` — the central promise: selecting
  elements returns the annotations that cover them, with matched indices.
- `test_annotation_membership_is_split_into_two_blocks` — memberships must be
  stored in blocks (bounded decode cost), not one giant blob.
- `test_city_object_query_uses_usap_default_descendants` — object queries
  traverse the relationship graph (building → parts), not just direct links.
- `test_city_object_query_finds_annotation_without_object_link` — annotations
  attached to a descendant's elements are found even without an explicit
  annotation↔object link row; validation still reports the missing link, since
  the package breaks the primary-object invariant.
- `test_city_object_query_follows_only_representing_links` — object queries
  follow `represents` links only, so a `derivedFrom` link does not make the
  annotation's elements part of the object it was derived from.
- `test_standalone_object_answers_for_itself` — an object with no edges at all
  is still its own only part, so its annotations survive the default
  `include_descendants=True` (they used to disappear, silently and validly).
- `test_descendants_follow_containment_edges_only` — the object graph is
  typed: an `adjacentTo` edge must not report the neighbour's elements as
  parts of this object, while `containment_types=` can opt into it.
- `test_link_city_objects_is_idempotent` — re-linking an identical edge
  returns the existing `relationship_id` instead of inserting a duplicate,
  while a variant edge (different role) still inserts; CityGML re-imports in
  update mode rely on this.
- `test_log_edit_writes_row` — the edit log is the package's provenance trail;
  a custom operation recorded via the public API must land in `usap_edit_log`.
- `test_create_failure_leaves_no_artifacts` — a failed `create()` must not
  leave a half-initialized file or open connection behind (a retry would
  otherwise hit "Database already exists").
- `test_default_paths_work_from_any_cwd` — default schema/vocabulary paths
  are package-anchored, not CWD-dependent (and ship in the wheel).
- `test_annotations_for_elements_survives_huge_selection` — id lists larger
  than SQLite's variable limit are chunked (and chunked results merged).
- `test_elements_for_city_object_survives_many_descendants` — a thousand
  descendants stay one query (the CTE binds no per-id variables), and an
  annotation reachable by two paths is still returned once.
- `test_raw_write_then_sdk_write_both_persist` — an implicit transaction
  opened by a raw `pkg.conn` write is adopted and committed by the next SDK
  write.
- `test_normalize_element_kind_is_strict` — unknown element kinds raise.
- `test_reregistering_an_asset_with_different_values_raises` /
  `test_reregistering_a_part_with_a_different_count_raises` — idempotent
  registration must mean "already registered **as the same thing**"; a
  conflicting kind or element count raises instead of returning a row that
  describes something the caller did not register.
- `test_annotation_domain_values_are_refused` — an unknown status, a
  confidence of 7.5, and non-JSON attributes are refused on create *and* on
  update; each of them breaks a reader rather than merely looking odd.
- `test_open_refuses_a_foreign_sqlite_file` /
  `test_open_refuses_an_unsupported_profile_version` — `open()` gates on the
  package being USAP and readable by this build, instead of failing later
  with "no such table: usap_asset".

## Annotation CRUD — `test_annotation_crud.py`

- `test_get_and_update_annotation` — read/update round-trip of the editable
  claim fields (label, status, confidence, attributes).
- `test_list_annotations_with_filters` — listing filters (status, concept,
  city object) return exactly the matching annotations.
- `test_delete_annotation_cascades_membership` — deleting an annotation must
  not orphan its membership blocks.
- `test_create_annotation_rejects_conflicting_concept` — re-using an
  `annotation_uid` with a different concept must raise, not silently replace
  the existing claim.
- `test_integrity_violations_raise_usap_error` — constraint violations
  surface as `USAPError`, not raw `sqlite3` exceptions.
- `test_update_annotation_moves_primary_object_link` /
  `test_update_annotation_clearing_primary_object_removes_link` — changing (or
  clearing) `primary_city_object_id` moves the `represents` link with it, so
  the annotation stops answering queries for the object it left.
- `test_update_annotation_keeps_other_object_links` — a move rewrites only the
  old primary link; other link types (e.g. `derivedFrom`) record separate facts
  and survive.
- `test_update_annotation_link_move_rolls_back_with_its_transaction` — column
  and link row move together or not at all.
- `test_update_annotation_repairs_missing_primary_object_link` — re-stating the
  current primary object restores a missing link row.

## Concept layer

`test_concept_registry.py`:

- `test_vocabulary_seeding_is_idempotent` — re-seeding the same vocabulary
  must not duplicate concepts.
- `test_list_accepted_concepts` — the registry listing (scheme, uri, ADE flag)
  is queryable.
- `test_get_semantic_class_and_concept_exists` — point lookups of registered
  concepts.
- `test_unknown_concept_fails_loudly` — annotating with an unregistered
  concept is an error, not a silent auto-create.
- `test_ambiguous_local_name_requires_scheme_or_uri` — the same local name in
  two schemes must raise an ambiguity error, not pick one silently.

`test_concept_layer.py` (minimal local vocabularies + hierarchy):

- `test_minimal_vocabulary_seeds_and_annotates` — a names-only vocabulary
  (derived `class_uri`, parent by name) is enough to annotate.
- `test_minimal_vocabulary_reingest_is_additive` — re-loading an updated
  vocabulary file adds concepts without duplicating existing ones.
- `test_changing_parent_on_reingest_raises` — silently re-parenting a concept
  would corrupt the hierarchy closure; it must raise.
- `test_list_accepted_concepts_in_use_flag` — `in_use`/`annotation_count`
  scouting reflects actual annotations.
- `test_subclass_block_query_uses_indexes` — hierarchy-accelerated queries
  must be served by the closure/index tables, not table scans
  (asserts on `EXPLAIN QUERY PLAN`; may need updating on SQLite upgrades).
- `test_ambiguous_vocabulary_parent_reports_ambiguity` — a vocabulary parent
  name matching concepts in several schemes reports ambiguity, not
  "not registered".

`test_external_vocabulary.py`:

- `test_seed_default_citygml_vocabulary` / `test_seed_default_ade_vocabulary`
  — the bundled example registries load and expose their expected concepts.

## Concept-first annotation API — `test_concept_annotation_api.py`

- `test_annotate_elements_with_citygml_concept` — `annotate_elements` accepts
  a concept by name and creates the annotation + membership in one call.
- `test_annotate_elements_with_ade_concept_and_city_object` — an ADE/custom
  concept can make a *different* claim on a CityGML object (e.g. `EnergyRoof`
  on a `RoofSurface`), linked to that object.
- `test_attach_annotation_elements_adds_second_representation` — attaching a
  second asset part's elements to an existing annotation spans representations
  without duplicating the claim.

## Value fields — `test_value_fields.py`

The v1 contract: dense full-coverage scalar fields bound to an asset part,
typed by a registered concept, queryable by value, edited by whole-field
rewrite.

- `test_round_trip_with_nan_holes` — NaN means "no value" and survives the
  round-trip.
- `test_elements_where_query_intent` — value queries return sorted element
  indices, same shape as membership queries.
- `test_concept_must_be_registered_and_field_is_asset_bound` — fields obey the
  same concept rule as annotations and can never reference a city object.
- `test_minimal_local_vocabulary_concept_works` — value fields work with
  names-only vocabularies too.
- `test_chunking_and_block_pruning` — large fields are stored in chunks and
  queries prune blocks via per-block min/max instead of decoding everything.
- `test_replace_overwrites_old_field` — whole-field rewrite is the only edit
  path; stale blocks must not survive.
- `test_dtype_fidelity` — each supported dtype round-trips bit-exactly.
- `test_narrowing_cast_allows_rounding_but_not_overflow` — strict casting must
  not block legitimate f8→f4 precision rounding, but a value that would become
  `inf` must raise.
- `test_error_paths` — partial coverage, NaN in integer fields, unsupported
  dtypes, and unknown annotations all fail loudly.
- `test_validation_flags_corrupt_value_blocks` — `validate_report()` catches
  hand-corrupted payloads, not just the readers.
- `test_batch_value_fields` — the batch format carries value fields (JSON
  `null` = "no value").
- `test_value_field_stats` — decode-free stats (min/max/count) agree with the
  decoded values.
- `test_integer_dtype_rejects_out_of_range_and_truncation` — integer value
  dtypes never wrap or truncate silently.
- `test_mixed_dtype_field_fails_validation` — a field mixing dtypes across
  blocks fails `validate_report()`, not only the readers.
- `test_all_value_readers_reject_partial_fields` — all three readers enforce
  the full-coverage contract.

## Asset adapters

`test_asset_adapters.py`:

- `test_register_asset_and_annotate_elements[las|mesh]` — one parametrized
  round-trip per adapter: registering an external asset yields an asset part
  whose elements can be annotated and queried back; per-adapter blocks pin
  what registration must capture (LAS point count from the header; mesh parts,
  face counts, and representation metadata).
- `test_gltf_mesh_registration_is_refused` — a glTF scene carries node
  transforms and instancing that this adapter cannot see, so registering one
  would record wrong bounds and part identity rather than none; `.glb`/`.gltf`
  must be refused, leaving nothing half-registered.

`test_citygml_adapter.py`:

- `test_import_tiny_citygml_semantics` — the semantic-only import: objects by
  `gml:id`, classes, nesting relationships, provenance — never geometry.
- `test_malformed_citygml_fails_loud` — a truncated/invalid file must refuse
  to import (it used to be silently half-parsed with `recover=True`).
- `test_non_citygml_namespace_is_refused` — matching on element local names
  alone imported any XML using the word "Building" as a CityGML building;
  the namespace must gate it, and a non-CityGML document is refused outright
  rather than importing zero objects.
- `test_foreign_id_attribute_is_not_a_gml_id` — only a real `gml:id` becomes
  object identity; an `id` attribute from another vocabulary must not be
  adopted as the uid annotations bind to.

## Ingestion procedures — `test_ingestion_procedures.py`

End-to-end tests of the three INGESTION.md procedures:

- `test_procedure_1_citygml_init_is_one_call` — CityGML + assets + linking
  JSON in one build call; concept and uid derived from the CityGML class.
- `test_procedure_2_minimal_init_is_fully_queryable` — no CityGML: carrier
  city objects (`object_status='temporary'`) answer the same queries; re-running
  in update mode edits in place.
- `test_unknown_city_object_fails_without_the_flag` — strict by default:
  unknown object names fail loudly unless `create_missing_city_objects` opts in.
- `test_carriers_are_queryable_in_every_graph` — a carrier created after a
  CityGML import has no edges in any graph; an edgeless object must still
  answer for itself in every graph, named or not.
- `test_new_carrier_requires_a_concept` — a carrier without "what it is" is
  meaningless and must be rejected.
- `test_part_reference_strictness` — ambiguous or contradictory asset/part
  references in batches fail loudly; `part_path` disambiguates.
- `test_procedure_3_update_adds_assets_and_edits` — update mode adds assets
  idempotently and edits annotations in place via the stable derived uid.
- `test_update_rerun_does_not_duplicate_relationships` — re-running a CityGML
  config in update mode must not duplicate relationship edges (regression for
  the `link_city_objects` idempotency guard).
- `test_update_mode_requires_an_existing_package` — update on a missing file
  is an error, not a silent create.

## Batches — `test_batch_annotations.py`

- `test_apply_annotation_batch_with_las_and_mesh` — one batch entry spanning
  LAS points and mesh faces of the same object.
- `test_batch_rejects_unknown_concept` / `test_batch_rejects_out_of_range_indices`
  — batches obey the same registration and index-range rules as the API.
- `test_batch_replace_existing` — `replace_existing` replaces memberships for
  the same `annotation_uid` instead of duplicating.
- `test_batch_replace_preserves_omitted_fields` — a partial edit entry updates
  only the fields it carries; omitted ones are preserved, not nulled.
- `test_batch_replace_moves_primary_object_link` — re-applying an entry against
  a different city object moves the annotation instead of leaving it linked to
  both.
- `test_apply_annotation_batch_file` — the file entry point behaves exactly
  like the in-memory batch and fails loudly on a missing path.

## Project builder — `test_project_builder.py`

- `test_build_project_package_from_config` — the full config build: CityGML +
  LAS + mesh + vocabularies, manifest contents included.
- `test_build_project_package_from_dict` — the dict entry point resolves
  relative paths against `base_dir`, not the CWD.
- `test_failed_build_leaves_no_package` — a build that dies part-way used to
  leave a package on disk with the concepts seeded and no assets; it looks
  real to everything downstream, so a failed build must leave nothing.
- `test_failed_update_leaves_the_previous_package_intact` — an `update=True`
  failure must not half-modify a package someone already has.

## Membership encoding — `test_encoding.py`

Element indices are the one place USAP handles data at asset scale (a
selection over a 10 GB point cloud is hundreds of millions of them).

- `test_index_normalization_sorts_and_deduplicates` /
  `test_index_normalization_accepts_arrays_and_lists_alike` — an ndarray from
  a selection tool and a list from a JSON batch must normalize identically;
  neither spelling is privileged.
- `test_non_integer_indices_are_refused` — truncating 3.5 to 3 would annotate
  a different element than the caller named; an exactly-integral float is
  still accepted, since JSON has no integer type.
- `test_negative_and_oversized_indices_are_refused` — the u32 index space is
  the storage format, so its edges are errors.
- `test_blocks_split_on_block_boundaries` / `test_roundtrip_preserves_offsets`
  — the block split and the payload round-trip.
- `test_decoding_refuses_an_oversized_payload` — a payload is decompressed
  before anything knows how big it becomes; without a ceiling a few hundred KB
  of crafted input expands to gigabytes. USAP packages are meant to be
  exchanged, so this cannot rest on trusting the file.
- `test_membership_write_accepts_a_numpy_selection` — end to end: an ndarray
  selection stores identically to the same selection as a list, and stored
  bounds are SQLite integers rather than numpy scalars smuggled in as BLOBs.

## Streaming mesh registration — `test_mesh_streaming.py`

- `test_streamed_registration_matches_loaded_registration[ply-binary|ply-ascii|obj]`
  — the property that lets streaming switch on by file size: both paths must
  record the same face count and bounds, or what a face index *means* would
  depend on how large the file happened to be.
- `test_large_files_stream_by_default` — the threshold decision is recorded on
  the asset rather than being invisible.
- `test_grouped_obj_is_refused_when_streaming` — a normal load splits a
  grouped OBJ into one part per group and annotations bind to part names, so a
  file that would decompose differently is refused, not guessed at.
- `test_streaming_an_unsupported_format_is_refused` — `.stl` has no streaming
  reader; say so instead of returning a wrong answer.

## GIS interoperability — `test_gpkg_interop.py`

Conformance additions:

- `test_view_keys_and_aggregate_types` — GDAL recognises `OGC_FID` as a view's
  feature id; a column merely named `fid` is an ordinary attribute, so layers
  opened but their feature ids were GDAL row numbers. Aggregates need explicit
  `CAST`s or they come back as strings.
- `test_srs_row_needs_a_definition` / `test_incomplete_srs_row_is_repaired` —
  "undefined" definitions are reserved for srs_id −1/0, and an already-written
  incomplete row must be repairable (INSERT OR IGNORE left it broken forever).
- `test_mixed_asset_crs_is_reported` — one CRS per package is an assumption
  nothing enforced; mixed CRSs are warned about, not silently misplaced.
- `test_extension_definition_is_a_uri` — the standard asks for a reference to
  the defining document, not a prose description.

- `test_attribute_layers_are_registered` — the three read-only attribute
  layers exist in `gpkg_contents` so QGIS/GDAL can browse the package.
- `test_annotations_view_is_readable` / `test_city_objects_view_shows_temporary_carriers`
  — the views expose annotations/concepts/carriers as documented.
- `test_asset_extent_is_union_of_part_bounds` / `test_part_without_bounds_gets_no_extent`
  / `test_adapters_produce_extents` / `test_asset_delete_cascades_extent` —
  the derived per-asset extent boxes: written from part bounds at
  registration, absent when bounds are unknown, removed by cascade.
- `test_tampered_extent_fails_validation` — extent blobs are checked against
  part bounds, so GIS-side tampering is detected.
- `test_epsg_from_wkt` / `test_set_package_srs_updates_layer_and_blobs` /
  `test_builder_config_srs_and_las_sniffing` — the SRS plumbing: WKT1/WKT2
  EPSG extraction, late SRS declaration re-encodes existing boxes, config
  `srs_id` and unambiguous LAS EPSG promotion.

`test_geopackage.py`:

- `test_created_package_is_a_geopackage` — the container contract: GeoPackage
  application id/user version, SRS rows, and extension registration.
- `test_no_explicit_index_duplicates_a_unique_autoindex` — schema hygiene: no
  explicit index may duplicate a UNIQUE constraint's auto-index (asserts on
  `EXPLAIN QUERY PLAN`; may need updating on SQLite upgrades).

## Validation — `test_validation.py`

Each test corrupts one invariant and asserts `validate_report()` names it:

- `test_validation_report_is_ok_for_valid_synthetic_package` — a freshly built
  package validates clean (the baseline the others corrupt).
- `test_validation_catches_corrupt_membership_payload` /
  `test_validation_catches_membership_count_mismatch` /
  `test_validation_catches_unsupported_encoding` — membership block integrity.
- `test_validation_catches_missing_semantic_class_closure` — class-closure
  integrity (subclass queries would silently miss rows without it).
- `test_validation_catches_containment_cycle` /
  `test_non_containment_cycle_is_not_an_error` — containment must be a DAG
  (a cycle makes an object its own part), while a cycle of non-containment
  edges such as `adjacentTo` is legitimate and must not be flagged.
- `test_basic_level_skips_payload_decoding` — `basic` must really not read
  payloads (a corrupt one that `deep` reports goes unnoticed), which is the
  whole point of having the level for large packages.
- `test_unknown_validation_level_is_refused` — a mistyped level must raise,
  not silently fall back to a weaker check.
- `test_external_level_detects_changed_asset` — a changed or deleted asset
  file is reported at `external` only; the in-database levels cannot see it
  and must not claim to.
- `test_verify_assets_reports_unhashed_assets` — registering without a hash
  trades away change detection, and that trade has to be visible.
- `test_validation_catches_primary_object_without_represents_link` — an
  annotation whose `primary_city_object_id` has no matching `represents` link
  is reported as an **error** (`ANNOTATION_PRIMARY_OBJECT_LINK_MISSING`):
  city-object queries would disagree with the annotation row.
- `test_validation_warns_on_duplicate_relationship_edges` — duplicate
  relationship edges (legacy packages, raw SQL) are reported as a **warning**
  with code `DUPLICATE_RELATIONSHIP_EDGE`, and a warning does not make the
  package invalid.
- `test_validate_connection_accepts_plain_connection` — validation works on a
  bare `sqlite3.Connection` and restores the caller's row factory.

## Synthetic generator — `test_synthetic.py`

- `test_synthetic_selected_roof_face_returns_roof_annotation` /
  `test_synthetic_city_object_query_returns_building_parts` /
  `test_synthetic_semantic_class_query_returns_roof_blocks` — the benchmark
  generator produces a package whose three query families all answer correctly.
- `test_selected_faces_across_multiple_blocks_return_annotations` — selections
  spanning block boundaries merge matches correctly.

## End-to-end — `test_integrated_prototype.py`

- `test_integrated_citygml_las_mesh_ade_annotation` — the full MVP story in
  one test: CityGML import + LAS + mesh + ADE concept + one annotation
  spanning all three representations, queried back from each.
