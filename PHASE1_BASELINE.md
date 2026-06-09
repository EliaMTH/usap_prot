# USAP Phase 1 Baseline

## Status

Phase 1 is considered functionally complete as a synthetic proof-of-concept baseline.

USAP now has a small but working Python SDK that can create, edit, query, validate, and benchmark synthetic `.usap.gpkg` packages.

The current implementation is still intentionally limited:

```text
no real CityGML parser
no real CityJSON importer
no real glTF/GLB parser
no point-cloud adapter
no viewer integration
```

That is deliberate. Phase 1 was about proving the core USAP data model and query logic before adding real asset complexity.

---

# 1. What Phase 1 proves

Phase 1 proves that the central USAP idea is technically workable:

```text
external asset
  -> stable asset part
    -> exact element indices
      -> compressed membership blocks
        -> annotation
          -> semantic class
            -> city object
              -> city-object graph
```

The most important query path is working:

```text
selected element indices
  -> block_start values
  -> usap_membership_block
  -> decoded exact offsets
  -> annotations
  -> semantic classes
  -> city objects
```

This supports the core interactive use case:

```text
I selected these faces/points/features in a viewer.
Tell me which semantic annotations they belong to.
```

---

# 2. Completed Phase 1 components

## 2.1 Database schema

Implemented core USAP tables:

```text
usap_profile
usap_asset
usap_asset_part
usap_semantic_class
usap_semantic_class_closure
usap_city_object
usap_city_object_relationship
usap_city_object_closure
usap_annotation
usap_annotation_object
usap_membership_block
usap_edit_log
```

Implemented minimal GeoPackage metadata tables:

```text
gpkg_spatial_ref_sys
gpkg_contents
gpkg_extensions
```

Set SQLite GeoPackage header metadata:

```text
application_id = 1196444487
user_version   = 10300
```

USAP tables are registered in `gpkg_extensions` using the extension name:

```text
usap_core
```

## 2.2 Python package structure

The prototype is now structured as an installable Python package:

```text
usap_from_scratch/
  pyproject.toml
  sql/
    schema.sql
  src/
    usap/
      __init__.py
      constants.py
      core.py
      errors.py
      geopackage.py
      sqlite_utils.py
      synthetic.py
      validation.py
  examples/
    demo_sdk.py
    build_synthetic.py
    validate_package.py
  scripts/
    benchmark_phase1.py
    profile_synthetic_build.py
  tests/
    test_core.py
    test_synthetic.py
    test_validation.py
    test_geopackage.py
```

## 2.3 SDK operations

Implemented core SDK operations:

```text
USAPPackage.create
USAPPackage.open
register_asset
register_asset_part
create_semantic_class
create_city_object
link_city_objects
rebuild_city_object_closure
create_annotation
link_annotation_to_object
replace_annotation_membership
annotations_for_elements
elements_for_annotation
elements_for_semantic_class
elements_for_city_object
validate_report
validate_basic
```

## 2.4 Membership block encoding

Implemented the first membership encoding:

```text
u32-zlib
```

Meaning:

```text
sorted unique unsigned 32-bit block-local offsets compressed with zlib
```

Example:

```text
absolute faces: 100, 101, 102, 6000, 6001
block size:     4096
```

Stored as:

```text
block_start = 0
payload     = offsets [100, 101, 102]
```

and:

```text
block_start = 4096
payload     = offsets [1904, 1905]
```

## 2.5 Synthetic package generator

Implemented synthetic package generation using fake mesh face IDs.

A synthetic building has:

```text
Building
  RoofSurface
  WallSurface
  GroundSurface
```

Each surface has:

```text
one city object
one annotation
one exact membership selection
```

This allows testing USAP behavior without parsing real geometry files.

## 2.6 Benchmark script

Implemented `scripts/benchmark_phase1.py`.

It measures:

```text
Q1: selected faces in one block -> annotations
Q2: selected faces across many blocks -> annotations
Q3: all RoofSurface membership blocks
Q4: building descendants -> membership blocks
Q5: replace one annotation with up to 5000 faces
```

It can also write benchmark reports as:

```text
JSON
Markdown
```

## 2.7 Validation report

Implemented structured validation:

```python
report = pkg.validate_report()
```

The validator detects problems such as:

```text
missing GeoPackage metadata
invalid GeoPackage application_id
missing USAP extension rows
corrupt membership payloads
unsupported encodings
membership count mismatch
membership min/max mismatch
membership outside asset-part range
membership element-kind mismatch
missing semantic-class closure rows
missing city-object closure rows
orphan annotation/object links
orphan relationships
```

---

# 3. Important fixes made during Phase 1

## 3.1 Transaction grouping fix

Initial synthetic generation was slow:

```text
~127 seconds for 1000 synthetic buildings
```

Profiling showed:

```text
13007 SQLite commits
~63 seconds spent committing
```

The cause was that only the first part of `create_synthetic_package()` was inside:

```python
with pkg.transaction():
```

After indenting the whole synthetic generation body inside one transaction, profiling changed to:

```text
~1.0 second for 1000 synthetic buildings
1 SQLite commit
```

This confirmed that the bottleneck was transaction overhead, not the USAP data model.

## 3.2 Semantic-class query optimization

The first `elements_for_semantic_class()` implementation was correct but inefficient.

It did:

```text
find all annotations for class
for each annotation:
    query membership blocks
```

For 1000 roof annotations, that meant roughly 1000 small SQL queries.

It was replaced with one SQL join:

```text
semantic class closure
  -> annotations
    -> membership blocks
```

Result:

```text
Q3 all RoofSurface blocks:
~787 ms -> ~11 ms
```

## 3.3 Selected-elements multi-block query optimization

The first `annotations_for_elements()` implementation ran one SQL query per touched block.

For Q2, that meant:

```text
20 block_start values -> 20 SQL queries
```

It was replaced with one SQL query using:

```sql
block_start IN (?, ?, ...)
```

Result:

```text
Q2 selected faces across 20 blocks:
~29.6 ms -> ~8.7 ms
```

## 3.4 GeoPackage schema creation bug

When minimal GeoPackage metadata was added, tests initially failed with:

```text
sqlite3.OperationalError: no such table: gpkg_spatial_ref_sys
```

The cause was that `USAPPackage.create()` had lost the call to:

```python
pkg.conn.executescript(schema_sql)
```

The corrected creation order is:

```text
1. create SQLite connection
2. read sql/schema.sql
3. execute schema_sql
4. initialize GeoPackage metadata
5. insert usap_profile
```

---

# 4. Current Phase 1 benchmark baseline

Benchmark command:

```bash
python scripts/benchmark_phase1.py --buildings 1000 --repeat 5
```

Synthetic package:

```text
Buildings:         1000
Faces per building: 500
Total faces:       500000
Annotations:       3000
City objects:      4000
Relationships:     3000
Closure rows:      7000
Membership blocks: 3120
Database size:     3.68 MiB
Build time:        0.929 s
Validation:        OK
```

Timings:

| Query | Repeat | Mean ms | Min ms | Max ms | Result |
|---|---:|---:|---:|---:|---|
| Q1 selected faces in one block -> annotations | 5 | 2.392 | 1.816 | 3.373 | 1 rows/items |
| Q2 selected faces across many blocks -> annotations | 5 | 8.671 | 6.574 | 12.954 | 25 rows/items |
| Q3 all RoofSurface blocks | 5 | 11.206 | 4.138 | 38.441 | 1029 rows/items |
| Q4 building_000000 descendants -> membership blocks | 5 | 6.697 | 5.761 | 7.839 | 3 rows/items |
| Q5 replace one annotation with up to 5000 faces | 5 | 16.396 | 14.591 | 18.059 | None |

Interpretation:

```text
Q1 is good for interactive single-block selection.
Q2 is now reasonable for multi-block selection.
Q3 is now set-based and fast enough for Phase 1.
Q4 shows the graph closure table works.
Q5 shows membership replacement is practical for moderate edits.
```

---

# 5. Phase 1 baseline commands

Install locally:

```bash
python -m pip install -e .
```

Run all tests:

```bash
python -m pytest
```

Run the benchmark and save reports:

```bash
python scripts/benchmark_phase1.py \
  --buildings 1000 \
  --repeat 5 \
  --json benchmark_results/phase1_1000.json \
  --md benchmark_results/phase1_1000.md
```

Validate a generated package:

```bash
python examples/validate_package.py benchmark_phase1.usap.gpkg
```

Inspect GeoPackage metadata:

```bash
sqlite3 benchmark_phase1.usap.gpkg "PRAGMA application_id;"
sqlite3 benchmark_phase1.usap.gpkg "PRAGMA user_version;"
sqlite3 benchmark_phase1.usap.gpkg "SELECT table_name, extension_name, scope FROM gpkg_extensions WHERE extension_name = 'usap_core';"
```

Expected:

```text
application_id = 1196444487
user_version   = 10300
USAP tables registered in gpkg_extensions
```

---

# 6. Known Phase 1 limitations

Phase 1 is not production-ready.

Known limitations:

```text
synthetic assets only
no real geometry parsing
no real CityJSON import
no real CityGML import
no ADE import
no point-cloud chunking
no glTF feature-ID integration
no viewer integration
no migration system
no formal conformance suite
no robust package version upgrade logic
```

The validator checks internal consistency only. It does not yet verify that external assets exist or that their content hashes still match.

The current GeoPackage metadata is minimal. It makes the file more GeoPackage-like, but USAP is not yet a formally documented OGC extension.

---

# 7. Definition of Phase 1 complete

Phase 1 can be considered complete when:

```text
all tests pass
benchmark script runs successfully
benchmark JSON/Markdown reports can be generated
validation reports OK for generated packages
minimal GeoPackage metadata is present
README/dev diary are updated
```

Current status:

```text
Phase 1 is functionally complete.
Documentation/baseline recording is in progress.
```

---

# 8. Next phases recap

## Phase 2 - Minimal real geometry adapters

Goal:

```text
Connect USAP to real external model assets while keeping geometry outside USAP.
```

Recommended order:

```text
1. OBJ adapter for simple face-index experiments
2. glTF/GLB primitive locator
3. CityJSON semantic importer
4. later LAS/LAZ/COPC point-cloud adapter
```

Phase 2 should focus on stable element identity:

```text
Which exact file?
Which exact primitive/chunk?
Which exact face or point index order?
What hash proves that asset part has not changed?
```

## Phase 3 - Editing workflow and validation

Goal:

```text
Make USAP reliable as a working file.
```

Add:

```text
richer edit log
annotation status transitions
object status transitions
asset stale detection
membership patching
repair/rebuild derived indexes
human-readable validation reports
```

## Phase 4 - CityJSON and CityGML import

Recommended order:

```text
CityJSON first
CityGML second
```

Reason:

```text
CityJSON is simpler to parse.
CityGML has richer XML structure and more relationship ambiguity.
```

Goal:

```text
Use external semantic models as sources of identity and provenance.
```

## Phase 5 - ADE support

Goal:

```text
Support ADE-defined semantic classes without changing membership tables.
```

ADE concepts should enter through:

```text
usap_semantic_class
is_ade = 1
attributes_json
optional extension tables later
```

## Phase 6 - 3D Tiles / glTF feature metadata

Goal:

```text
Use external feature IDs when assets already provide them.
```

This is where `usap_feature_id_binding` becomes important.

## Phase 7 - Point-cloud scale support

Goal:

```text
Support exact point-level annotation on large point clouds.
```

Key rule:

```text
Use chunk-local point indices.
Do not expand millions of points unless explicitly requested.
```

## Phase 8 - Viewer/application integration

Goal:

```text
Connect USAP to actual face/point/feature selection workflows.
```

Possible integrations:

```text
Blender
QGIS
Cesium / 3D Tiles
custom web viewer
Python desktop viewer
```

## Phase 9 - Robust packaging and interoperability

Goal:

```text
Make USAP documented, migratable, and reusable by independent tools.
```

Add:

```text
schema migrations
profile documentation
JSON metadata schemas
conformance tests
version upgrades
```

## Phase 10 - Endgame

Goal:

```text
USAP becomes a practical bridge between semantic city models and high-performance 3D assets.
```

Endgame capabilities:

```text
standalone USAP packages
CityJSON-linked packages
CityGML-linked packages
ADE semantic classes
multi-graph city-object relationships
fast viewer picking
editable annotation workflow
validation and provenance
point-cloud support
3D Tiles / glTF feature-ID support
```

Endgame principle:

```text
CityGML remains the semantic authority when present.
USAP remains the fast query/edit index.
```
