# USAP Development Process So Far

## A tutorial-style record of what we built, why we built it, and what each step teaches

**USAP** means **Urban Semantic Annotation Package**.

The goal of USAP is to create a lightweight, editable, SQLite/GeoPackage-based annotation package for external 3D urban assets.

USAP does **not** store the 3D model itself. Instead, it stores:

```text
external asset reference
  -> stable asset part
    -> exact element indices
      -> annotation
        -> semantic class
          -> city object identity
            -> city-object graph
```

The core idea is that a face, point, feature, or vertex index is meaningful only inside a specific external asset part.

For example:

```text
city_mesh.glb
  -> node=0/mesh=0/primitive=0
    -> face 6000
      -> ann_building_1_roof_mesh
        -> RoofSurface
          -> building_1_roof_1
            -> child of building_1
```

This document records the development process so far as a learning path.

---

# 1. Starting point: the mental model

Before writing code, we clarified the main concepts.

USAP separates concepts that are often mixed together:

| Concept | Meaning | Example |
|---|---|---|
| Asset | External file or dataset | `city_mesh.glb` |
| Asset part | Stable sub-location where indices are valid | `node=0/mesh=0/primitive=0` |
| Element | Indexed model component | face `6000`, point `42` |
| Semantic class | Type of thing | `Building`, `RoofSurface` |
| City object | Stable semantic object identity | `building_1_roof_1` |
| Annotation | Editable claim over exact elements | `ann_building_1_roof_mesh` |
| Membership block | Compressed element membership row | block `4096`, offsets `[1904, 1905]` |

The most important distinction is:

```text
Semantic class = what kind of thing it is
City object    = which object it is
Annotation     = editable claim linking meaning to elements
Membership     = exact face/point/element indices
```

Example:

```text
RoofSurface              = semantic class
building_1_roof_1        = city object
ann_building_1_roof_mesh = annotation
faces 100, 101, 102      = membership
```

Why this matters:

- The same roof can exist in several external representations.
- One city object can be represented by multiple annotations.
- One annotation can point to exact faces in a mesh, exact points in a point cloud, or external feature IDs later.
- CityGML or CityJSON can provide semantic identity, but normal USAP queries should not need to reparse them.

---

# 2. Important design decision: object graphs, not one universal hierarchy

A key conceptual issue was whether USAP requires one city-object hierarchy.

The answer we settled on was:

```text
USAP should not impose one universal hierarchy.
USAP stores typed graph edges.
A hierarchy is a selected graph view used for query/navigation.
```

This matters because CityGML 3 can contain multiple kinds of relationships, not just a single parent-child tree.

For example, these relationships may all be meaningful, but not all belong in the same traversal:

```text
building_1 -> roof_1       boundedBy
building_1 -> wall_1       boundedBy
wall_1     -> window_1     opening
roof_1     -> wall_1       adjacentTo
building_1 -> address_1    hasAddress
```

If every relationship is treated as a descendant relationship, queries become ambiguous.

So the accepted design uses `graph_name`.

For the current prototype, we only use:

```text
usap_default
```

Later, USAP may also store graphs such as:

```text
citygml_composition
citygml_boundedBy
cityjson_parent_child
topology
manual_review
```

The final accepted relationship table is:

```sql
CREATE TABLE usap_city_object_relationship (
    relationship_id        INTEGER PRIMARY KEY,

    graph_name             TEXT NOT NULL DEFAULT 'usap_default',

    parent_city_object_id  INTEGER NOT NULL
        REFERENCES usap_city_object(city_object_id)
        ON DELETE CASCADE,

    child_city_object_id   INTEGER NOT NULL
        REFERENCES usap_city_object(city_object_id)
        ON DELETE CASCADE,

    relationship_type      TEXT NOT NULL,

    role                   TEXT,

    source_asset_id        INTEGER
        REFERENCES usap_asset(asset_id)
        ON DELETE SET NULL,

    source_relation_id     TEXT,

    metadata_json          TEXT
);
```

And the graph-aware closure table is:

```sql
CREATE TABLE usap_city_object_closure (
    graph_name                  TEXT NOT NULL,

    ancestor_city_object_id     INTEGER NOT NULL
        REFERENCES usap_city_object(city_object_id)
        ON DELETE CASCADE,

    descendant_city_object_id   INTEGER NOT NULL
        REFERENCES usap_city_object(city_object_id)
        ON DELETE CASCADE,

    depth                       INTEGER NOT NULL,

    PRIMARY KEY (
        graph_name,
        ancestor_city_object_id,
        descendant_city_object_id
    )
) WITHOUT ROWID;
```

This allows queries such as:

```python
elements_for_city_object(
    object_uid="building_1",
    include_descendants=True,
    graph_name="usap_default",
)
```

Meaning:

```text
Give me the membership blocks linked to building_1 and its descendants,
but only according to the usap_default graph.
```

---

# 3. Phase 0: manual tiny database

## Goal

The first goal was not to build a full SDK.

The first goal was:

```text
Create a tiny USAP file by hand and understand every row.
```

We built a minimal example with:

```text
one external mesh asset
one mesh primitive / asset part
Building and RoofSurface semantic classes
building_1 and building_1_roof_1 city objects
building_1 -> building_1_roof_1 relationship
one roof annotation
five annotated face IDs
```

The annotated faces were:

```text
100, 101, 102, 6000, 6001
```

## Why this phase was necessary

This phase teaches the database logic before hiding it inside Python methods.

It proves that you understand:

- what each table stores;
- why an asset part is necessary;
- why annotation is separate from city object;
- how exact element membership is stored;
- why membership blocks are better than one row per element.

## Files created

At this stage, the project was simple:

```text
usap_from_scratch/
  schema.sql
  make_tiny_usap.py
  query_selected.py
```

## `schema.sql`

This file contains the database schema.

The important table groups are:

```text
Package/profile:
  usap_profile

External assets:
  usap_asset
  usap_asset_part

Semantic classes:
  usap_semantic_class
  usap_semantic_class_closure

City objects:
  usap_city_object
  usap_city_object_relationship
  usap_city_object_closure

Annotations:
  usap_annotation
  usap_annotation_object

Exact membership:
  usap_membership_block

Provenance/debugging:
  usap_edit_log
```

## `make_tiny_usap.py`

This script created the first package:

```text
demo.usap.gpkg
```

It inserted one row or a few rows into the core tables.

The most important function in this script was the membership encoding logic.

With block size:

```text
4096
```

The face IDs:

```text
100, 101, 102, 6000, 6001
```

became two membership blocks:

```text
block_start = 0
payload = offsets [100, 101, 102]
```

and:

```text
block_start = 4096
payload = offsets [1904, 1905]
```

because:

```text
6000 - 4096 = 1904
6001 - 4096 = 1905
```

This is the first major USAP performance idea:

```text
Do not store one row per face or point.
Store compressed blocks of selected element offsets.
```

## `query_selected.py`

This script implemented the first core query:

```text
selected faces -> annotations
```

The algorithm was:

```text
1. Take selected face indices.
2. Convert each selected index to a block_start.
3. Query usap_membership_block only for those block_start values.
4. Decode the compressed payload.
5. Check exact offset intersection.
6. Return matching annotations.
```

For selected faces:

```text
100, 101, 6000
```

The expected answer was:

```text
ann_building_1_roof_mesh
RoofSurface
building_1_roof_1
matched faces: 100, 101, 6000
```

## What Phase 0 proved

It proved this trace:

```text
face 6000
  -> block_start 4096
  -> offset 1904
  -> membership block
  -> ann_building_1_roof_mesh
  -> RoofSurface
  -> building_1_roof_1
  -> building_1
```

This is the smallest meaningful USAP workflow.

---

# 4. Phase 1A: first SDK class

## Goal

After proving the logic manually, the next step was to stop writing one-off SQL scripts.

The goal became:

```text
Wrap the core operations in a small Python SDK class.
```

The class was called:

```python
USAPPackage
```

## Why this was necessary

USAP is intended to be a working file, not just a passive data dump.

That means edits should go through controlled operations, not random SQL statements.

For example, replacing annotation membership should:

```text
check that the asset part exists
check that the element kind matches
check that element indices are in range
delete previous membership for that annotation/asset part
write new compressed membership blocks
log the edit
```

This should be one SDK operation, not something repeated manually.

## Main file

At first, the SDK lived in:

```text
usap_core.py
```

Later, it was moved into the package structure as:

```text
src/usap/core.py
```

## Main class

```python
class USAPPackage:
    ...
```

The first useful methods were:

```python
USAPPackage.create(...)
USAPPackage.open(...)
register_asset(...)
register_asset_part(...)
create_semantic_class(...)
create_city_object(...)
link_city_objects(...)
rebuild_city_object_closure(...)
create_annotation(...)
link_annotation_to_object(...)
replace_annotation_membership(...)
annotations_for_elements(...)
elements_for_annotation(...)
elements_for_semantic_class(...)
elements_for_city_object(...)
validate_basic(...)
```

## Important SDK method: `replace_annotation_membership`

This became the first serious edit operation.

Conceptually:

```python
pkg.replace_annotation_membership(
    annotation_id=annotation_id,
    asset_part_id=asset_part_id,
    element_kind=ELEMENT_KIND_FACE,
    element_indices=[100, 101, 102, 6000, 6001],
)
```

It replaces old membership blocks with new ones.

This matters because annotation editing is central to USAP.

A user may later correct a roof selection, so the SDK must support replacing exact membership safely.

## Important SDK method: `annotations_for_elements`

This implements the key reverse query:

```text
selected elements -> annotations
```

It does not scan the entire model.

It only touches membership blocks whose `block_start` matches the selected indices.

This is the query that matters most for viewer integration later.

If a user selects faces in a 3D viewer, the viewer can send:

```text
asset_part_id
 element_kind
 selected face indices
```

and USAP can return matching annotations.

## Important SDK method: `elements_for_city_object`

This supports queries like:

```text
Find building_1 in the model.
```

With descendants enabled, it uses:

```text
usap_city_object_closure
```

for a selected graph:

```text
usap_default
```

So:

```python
pkg.elements_for_city_object(
    object_uid="building_1",
    include_descendants=True,
    graph_name="usap_default",
)
```

returns membership blocks for the building and its roof/wall/ground/etc. descendants.

---

# 5. Small code-quality improvement: `require_lastrowid`

During development, Pylance warned about `cur.lastrowid` because SQLite types it as possibly `None`.

The helper added was:

```python
def require_lastrowid(cur: sqlite3.Cursor) -> int:
    """
    Return cursor.lastrowid as int, or fail loudly if SQLite did not produce one.
    """
    if cur.lastrowid is None:
        raise USAPError("Expected SQLite lastrowid, but got None.")

    return cur.lastrowid
```

This is better than repeatedly writing:

```python
int(cur.lastrowid)
```

because it makes the assumption explicit:

```text
This INSERT must produce a row ID.
If it does not, something is wrong and the SDK should fail loudly.
```

The helper belongs in:

```text
src/usap/sqlite_utils.py
```

---

# 6. Phase 1B: turn loose scripts into a real Python package

## Goal

After the SDK class worked, the project needed structure.

The goal became:

```text
Turn loose files into a small installable Python package.
```

## New project structure

The structure became:

```text
usap_from_scratch/
  pyproject.toml
  sql/
    schema.sql
  src/
    usap/
      __init__.py
      constants.py
      errors.py
      sqlite_utils.py
      core.py
  examples/
    demo_sdk.py
  tests/
    test_core.py
```

## Why this matters

This matters because USAP is becoming an SDK, not just a script.

A package structure gives you:

```text
clean imports
separated modules
tests
examples
editable local installation
room to grow
```

Instead of:

```python
from usap_core import USAPPackage
```

you can now write:

```python
from usap import USAPPackage, ELEMENT_KIND_FACE
```

## `pyproject.toml`

This file defines the project as an installable package:

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "usap"
version = "0.1.0"
description = "Urban Semantic Annotation Package phase-1 prototype"
requires-python = ">=3.11"
dependencies = []

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
```

It allows installation with:

```bash
python -m pip install -e .
```

The `-e` means editable mode.

So if you modify code under `src/usap`, Python immediately sees the changes.

## `constants.py`

This file contains shared constants:

```python
ELEMENT_KIND_FACE = 1
ELEMENT_KIND_POINT = 2
ELEMENT_KIND_VERTEX = 3
ELEMENT_KIND_FEATURE = 4

DEFAULT_BLOCK_SIZE = 4096
DEFAULT_ENCODING = "u32-zlib"
DEFAULT_GRAPH_NAME = "usap_default"
```

This avoids scattering magic numbers and strings through the code.

## `errors.py`

This file contains:

```python
class USAPError(Exception):
    """Base exception for USAP SDK errors."""
```

Using a project-specific exception makes errors easier to distinguish later.

## `sqlite_utils.py`

This file contains SQLite helper functions such as:

```python
require_lastrowid(...)
```

## `core.py`

This is where the main SDK class lives:

```python
USAPPackage
```

This remains the core of the project.

## `__init__.py`

This exports the public API:

```python
from usap import USAPPackage, ELEMENT_KIND_FACE
```

That is the interface you want users to see.

## `examples/demo_sdk.py`

This script demonstrates normal SDK usage.

It creates:

```text
demo_sdk.usap.gpkg
```

and performs:

```text
asset registration
asset-part registration
semantic class creation
city-object creation
city-object linking
annotation creation
membership replacement
selected-face query
annotation-to-elements query
semantic-class query
city-object query
validation
```

## `tests/test_core.py`

This file introduced the first real tests.

The tests prove:

```text
selected face 6000 returns the roof annotation
five faces are stored as two membership blocks
building_1 finds roof faces through usap_default descendants
the tiny package validates cleanly
```

This is important because USAP will become complex.

Tests protect the core query path:

```text
selected element
  -> block_start
  -> usap_membership_block
  -> annotation
  -> semantic class
  -> city object
```

---

# 7. Phase 1C: synthetic package generator

## Goal

After the tiny test case worked, the next step was to generate many fake buildings automatically.

The goal became:

```text
Create synthetic USAP packages large enough to test query behavior.
```

This is still not real geometry.

We intentionally avoid real CityGML, glTF, point clouds, and viewers for now.

Why?

Because real importers introduce many unrelated problems:

```text
file parsing
format conventions
geometry indexing
asset hashes
coordinate systems
viewer selection APIs
```

Before dealing with those, we want to test the core USAP data model.

## New file

```text
src/usap/synthetic.py
```

## Main configuration class

```python
@dataclass(frozen=True)
class SyntheticConfig:
    building_count: int = 100
    roof_faces_per_building: int = 120
    wall_faces_per_building: int = 300
    ground_faces_per_building: int = 80
    mesh_uri: str = "synthetic_city_mesh.glb"
    mesh_part_path: str = "node=0/mesh=0/primitive=0"
```

This lets you configure how many synthetic buildings and faces to generate.

## Main result class

```python
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
```

This returns useful IDs and counts after generation.

## Main function

```python
create_synthetic_package(...)
```

It creates a package with this structure:

```text
building_000000
  building_000000_roof
  building_000000_wall
  building_000000_ground

building_000001
  building_000001_roof
  building_000001_wall
  building_000001_ground

...
```

Each surface gets one annotation:

```text
ann_building_000000_roof_mesh
ann_building_000000_wall_mesh
ann_building_000000_ground_mesh
```

Each annotation gets deterministic face ranges.

For example, if each building has:

```text
20 roof faces
30 wall faces
10 ground faces
```

then building 0 receives faces:

```text
roof:   0 - 19
wall:   20 - 49
ground: 50 - 59
```

Building 1 receives:

```text
roof:   60 - 79
wall:   80 - 109
ground: 110 - 119
```

And so on.

## Important performance choice

Inside the generator, we used:

```python
rebuild_closure=False
```

when linking city objects.

Then we rebuild closure once at the end:

```python
pkg.rebuild_city_object_closure(graph_name="usap_default")
```

Why?

Because rebuilding the closure table after every single edge would be slow.

For synthetic data, many relationships are inserted:

```text
building -> roof
building -> wall
building -> ground
```

For 100 buildings, that is 300 relationships.

For 10,000 buildings, that is 30,000 relationships.

Rebuilding once at the end is the correct workflow.

## Example script

A new example was planned:

```text
examples/build_synthetic.py
```

It creates:

```text
synthetic_100.usap.gpkg
```

with:

```text
100 buildings
300 annotations
50,000 synthetic faces
```

Then it tests queries such as:

```text
selected faces -> annotation
building_000000 -> compact membership blocks
RoofSurface -> compact membership blocks
```

## Synthetic tests

A new test file was planned:

```text
tests/test_synthetic.py
```

It proves:

```text
a synthetic package can be created
selected roof face 0 returns ann_building_000000_roof_mesh
building_000000 returns all its roof/wall/ground faces through usap_default
```

---

# 8. What has been completed so far

So far, the development process has covered:

```text
Phase 0  - manual tiny database
Phase 1A - tiny SDK class
Phase 1B - package structure and tests
Phase 1C - synthetic generator design and implementation plan
```

The project now has the conceptual and code structure for:

```text
creating USAP packages
registering assets
registering asset parts
creating semantic classes
creating city objects
linking city objects in usap_default
creating annotations
replacing annotation membership
encoding membership as compressed blocks
querying selected elements to annotations
querying annotation to elements
querying semantic class to blocks
querying city object to blocks
basic validation
synthetic package generation
```

---

# 9. Current recommended project structure

At this point, the project should look like this:

```text
usap_from_scratch/
  pyproject.toml
  sql/
    schema.sql
  src/
    usap/
      __init__.py
      constants.py
      errors.py
      sqlite_utils.py
      core.py
      synthetic.py
  examples/
    demo_sdk.py
    build_synthetic.py
  tests/
    test_core.py
    test_synthetic.py
```

---

# 10. Current development commands

Install the package locally:

```bash
python -m pip install -e .
```

Run the SDK demo:

```bash
python examples/demo_sdk.py
```

Run the synthetic generator demo:

```bash
python examples/build_synthetic.py
```

Run tests:

```bash
python -m pytest
```

---

# 11. What the code is teaching

## Lesson 1: indices are local, not global

A face index is not meaningful by itself.

This is ambiguous:

```text
face 6000
```

This is meaningful:

```text
asset_id = 1
asset_part_id = 1
part_path = node=0/mesh=0/primitive=0
element_kind = face
face index = 6000
```

## Lesson 2: annotations are editable claims

A city object is a stable semantic identity.

An annotation is an editable statement about exact elements.

This lets you later support:

```text
draft annotations
manual corrections
automatic candidate annotations
conflicting annotations
superseded annotations
```

without changing the city object identity.

## Lesson 3: membership blocks are the core performance design

The naive model would be:

```text
annotation_id | asset_part_id | element_index
```

That would create one database row per element.

USAP instead stores:

```text
annotation_id | asset_part_id | block_start | compressed offsets
```

This makes selected-element queries depend on touched blocks, not total model size.

## Lesson 4: compact results are important

A query like:

```text
Give me all RoofSurface faces
```

should usually return compact membership blocks, not immediately expand millions of face IDs into Python lists.

That is why methods such as:

```python
elements_for_annotation(..., expand=False)
elements_for_semantic_class(..., expand=False)
elements_for_city_object(..., expand=False)
```

are important.

## Lesson 5: closure tables are derived indexes

The city-object relationship table is canonical.

The closure table is derived.

That means:

```text
usap_city_object_relationship = source of truth
usap_city_object_closure      = rebuildable acceleration table
```

If closure becomes inconsistent, the SDK should rebuild it.

---

# 12. What comes next

The next major phase is:

```text
Phase 1D - benchmark script
```

Now that synthetic packages can be generated, the next task is to measure query behavior.

The benchmark should test:

```text
Q1: 100 selected faces in one block -> annotations
Q2: 1000 selected faces across many blocks -> annotations
Q3: all RoofSurface blocks in one asset
Q4: building_1 descendants -> annotations -> membership blocks
Q5: replace one annotation with 5000 faces
```

The benchmark should measure:

```text
elapsed time
number of results
number of membership blocks touched
package size
```

This is the first serious proof that USAP is worth continuing.

The key question is:

```text
Can USAP answer selected element queries quickly without scanning the whole model?
```

Only after this synthetic benchmark works should the project move toward real geometry adapters.

---

# 13. Longer roadmap reminder

The full roadmap remains:

```text
Phase 0  - manual schema understanding
Phase 1  - core SDK and synthetic benchmark
Phase 2  - minimal real geometry adapters
Phase 3  - editing workflow and validation
Phase 4  - CityJSON and CityGML import
Phase 5  - ADE support
Phase 6  - 3D Tiles / glTF feature metadata
Phase 7  - point-cloud scale support
Phase 8  - viewer/application integration
Phase 9  - robust packaging and interoperability
Phase 10 - endgame CityGML/ADE-aware semantic annotation ecosystem
```

The current work is still inside Phase 1.

That is correct.

Do not jump to CityGML, ADE, or viewers yet.

The current priority is:

```text
prove the core table design and membership-block query logic on synthetic data
```


---

# 14. Phase 1D to Phase 1I: benchmark, validation, optimization, and minimal GeoPackage metadata

This section updates the development diary after completing the later Phase 1 work.

The project moved from a correct small SDK to a more serious Phase 1 baseline with profiling, benchmark evidence, validation, query optimization, and minimal GeoPackage metadata.

---

## 14.1 Phase 1D: benchmark script

### Goal

The goal was to measure whether the synthetic SDK behaves well under larger generated data.

The benchmark script was added:

```text
scripts/benchmark_phase1.py
```

It measures five core operations:

```text
Q1: selected faces in one block -> annotations
Q2: selected faces across many blocks -> annotations
Q3: all RoofSurface membership blocks
Q4: one building and descendants -> membership blocks
Q5: replace one annotation with up to 5000 faces
```

This benchmark is important because it tests the actual query families USAP is designed for:

```text
selected elements -> annotations
semantic class -> elements/blocks
city object -> elements/blocks
annotation editing
```

---

## 14.2 First benchmark result and the build-time problem

The first 1000-building benchmark produced acceptable query timings, but the build time was bad:

```text
Build time: ~126-127 seconds
```

The query timings were already promising:

```text
Q1: a few milliseconds
Q2: tens of milliseconds
Q4: a few milliseconds
Q5: tens of milliseconds
```

But a 127-second synthetic build for 1000 buildings was too slow.

This raised an implementation question:

```text
Is the USAP data model slow, or is the generator/SDK writing inefficiently?
```

---

## 14.3 Profiling synthetic generation

A profiling script was added:

```text
scripts/profile_synthetic_build.py
```

The profile showed the real bottleneck:

```text
~124 seconds total
13007 SQLite commits
~63 seconds spent committing
```

So the issue was not zlib compression, closure rebuild, or the membership-block design.

The issue was transaction overhead:

```text
many tiny SQLite commits
```

Instead of writing all synthetic data in one transaction, the code was committing thousands of times.

---

## 14.4 Transaction grouping fix

The intended fix was to wrap the entire synthetic generation body inside:

```python
with pkg.transaction():
    ...
```

At first, only the first asset registration section was indented inside the transaction. That meant most of the generator still committed operation by operation.

After correcting the indentation so that sections 2, 3, 4, closure rebuild, and validation were all inside the transaction, the profile changed dramatically:

```text
Before:
  ~124 seconds
  13007 commits

After:
  ~1.0 second
  1 commit
```

This taught an important SDK lesson:

```text
Individual edit operations should be safe when called alone.
Bulk operations should be able to group many edits in one transaction.
```

The USAP format did not change. Only the write behavior improved.

---

## 14.5 Phase 1E: stronger validation report

The initial validation function was too shallow.

The project added:

```text
src/usap/validation.py
examples/validate_package.py
tests/test_validation.py
```

The new validation API is:

```python
report = pkg.validate_report()
```

It returns a structured report containing issues with:

```text
severity
code
message
table
row_id
details
```

The validator now checks problems such as:

```text
corrupt membership payloads
unsupported membership encodings
membership count mismatch
membership min/max mismatch
membership outside asset-part range
membership element-kind mismatch
missing semantic-class closure rows
missing city-object closure rows
orphan annotation-object links
orphan relationships
missing profile row
```

A useful manual test was to corrupt one membership payload in SQLite and then run:

```bash
python examples/validate_package.py benchmark_phase1.usap.gpkg
```

The validator correctly reported the corrupt payload after the UPDATE was applied to an existing row.

---

## 14.6 Phase 1F: semantic-class query optimization

The benchmark showed that Q3 was slow:

```text
Q3 all RoofSurface blocks: ~787 ms
```

The reason was implementation structure.

The old implementation did:

```text
find all RoofSurface annotations
for each annotation:
    query membership blocks
```

For 1000 roofs, this produced many small SQL queries.

The method `elements_for_semantic_class()` was rewritten as one set-based SQL join:

```text
semantic class closure
  -> annotations
    -> membership blocks
```

After this change:

```text
Q3 all RoofSurface blocks: ~787 ms -> ~11 ms
```

This taught another key SDK lesson:

```text
Correct query logic is not enough.
The SDK should use set-based SQL when querying many rows.
```

---

## 14.7 Phase 1G: selected-elements multi-block query optimization

After Q3 was fixed, Q2 became the slowest read query.

The old `annotations_for_elements()` implementation ran one SQL query per touched block:

```text
20 touched blocks -> 20 SQL queries
```

It was optimized to issue one query using:

```sql
block_start IN (?, ?, ...)
```

The algorithm stayed the same:

```text
selected indices
  -> block_start values
  -> candidate membership blocks
  -> decode payloads
  -> exact intersection
  -> annotations
```

But the SQL was improved.

Result:

```text
Q2 selected faces across 20 blocks: ~29.6 ms -> ~8.7 ms
```

---

## 14.8 Phase 1H: benchmark JSON and Markdown reports

The benchmark script was extended so results can be saved as files:

```text
benchmark_results/phase1_1000.json
benchmark_results/phase1_1000.md
```

This matters because benchmark results should be reproducible evidence, not just terminal scrollback.

The benchmark report records:

```text
creation time
Python/platform information
database size
synthetic configuration
package counts
build time
query timings
validation result
```

This allows future comparisons:

```text
before optimization vs after optimization
Phase 1 vs Phase 2
small synthetic data vs larger synthetic data
```

---

## 14.9 Phase 1I: minimal GeoPackage metadata

The package was upgraded from:

```text
SQLite file with .usap.gpkg extension
```

to a more GeoPackage-like SQLite container.

Added tables:

```text
gpkg_spatial_ref_sys
gpkg_contents
gpkg_extensions
```

Added SQLite header metadata:

```text
application_id = 1196444487
user_version   = 10300
```

USAP tables are registered in:

```text
gpkg_extensions
```

using extension name:

```text
usap_core
```

Important limitation:

```text
This is minimal GeoPackage metadata.
It does not yet make USAP a formal OGC extension.
Generic GeoPackage tools may recognize the container but will not understand USAP semantics unless they know the USAP extension.
```

A bug appeared during this phase:

```text
sqlite3.OperationalError: no such table: gpkg_spatial_ref_sys
```

The cause was that `USAPPackage.create()` no longer executed:

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

After this fix, all tests passed.

---

# 15. Current Phase 1 baseline result

The current reported 1000-building benchmark is:

```text
Build time:        0.929 s
Database size:     3.68 MiB
Total faces:       500000
Annotations:       3000
City objects:      4000
Relationships:     3000
Closure rows:      7000
Membership blocks: 3120
Validation:        OK
```

Timings:

| Query | Mean ms | Result |
|---|---:|---|
| Q1 selected faces in one block -> annotations | 2.392 | 1 rows/items |
| Q2 selected faces across many blocks -> annotations | 8.671 | 25 rows/items |
| Q3 all RoofSurface blocks | 11.206 | 1029 rows/items |
| Q4 building_000000 descendants -> membership blocks | 6.697 | 3 rows/items |
| Q5 replace one annotation with up to 5000 faces | 16.396 | None |

This is a good Phase 1 baseline.

The result supports the narrow claim that:

```text
USAP can create, query, edit, validate, and benchmark synthetic annotation packages efficiently enough to justify moving to real asset adapters.
```

It does not yet prove real-world interoperability.

---

# 16. Phase 1 completion checklist

Current Phase 1 status:

```text
manual tiny package: complete
SDK package structure: complete
membership block encoding: complete
synthetic generator: complete
benchmark script: complete
transaction grouping: complete
validation report: complete
Q3 semantic-class optimization: complete
Q2 selected-elements optimization: complete
benchmark JSON/Markdown output: complete
minimal GeoPackage metadata: complete
```

Before starting Phase 2, the remaining documentation/baseline tasks are:

```text
write PHASE1_BASELINE.md
save final benchmark reports
update README with Phase 1 commands
commit or tag the Phase 1 baseline in Git
```

---

# 17. Roadmap from here

## Phase 2 - Minimal real geometry adapters

Goal:

```text
Connect USAP to real external assets without storing geometry inside USAP.
```

Recommended order:

```text
1. Simple OBJ adapter for face-index experiments
2. glTF/GLB primitive locator
3. CityJSON semantic importer
4. LAS/LAZ/COPC point-cloud adapter later
```

Main risk:

```text
stable element identity
```

The adapter must answer:

```text
Which exact file?
Which exact part?
Which exact face/point/feature index order?
How do we know the external asset has not changed?
```

## Phase 3 - Editing workflow and validation

Goal:

```text
Make USAP reliable as a working file.
```

Add:

```text
richer edit logs
status transition helpers
asset stale detection
membership patching
repair/rebuild functions
stronger validation reports
```

## Phase 4 - CityJSON and CityGML import

Recommended order:

```text
CityJSON first
CityGML second
```

CityJSON is simpler to parse. CityGML has richer structure and relationship ambiguity.

## Phase 5 - ADE support

Goal:

```text
Allow ADE-defined semantic classes without changing the core membership design.
```

ADE support should mostly use:

```text
usap_semantic_class
is_ade = 1
attributes_json
optional extension tables
```

## Phase 6 - 3D Tiles / glTF feature metadata

Goal:

```text
Use feature IDs from external assets when they already exist.
```

This will use:

```text
usap_feature_id_binding
```

## Phase 7 - Point-cloud scale support

Goal:

```text
Support point-level annotations on large point clouds.
```

Key principle:

```text
Use chunk-local point indices.
Do not expand millions of point IDs unless explicitly requested.
```

## Phase 8 - Viewer/application integration

Goal:

```text
Use USAP in an actual selection/editing workflow.
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

Endgame principle:

```text
CityGML remains the semantic authority when present.
USAP remains the fast query/edit index.
```
