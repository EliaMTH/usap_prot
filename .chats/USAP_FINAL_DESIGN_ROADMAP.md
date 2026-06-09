# USAP Final Design and Development Roadmap

**USAP** means **Urban Semantic Annotation Package**.

USAP is a GeoPackage/SQLite-based working-file profile for storing editable, exact, element-level semantic annotations over external 3D urban assets.

The intended file extension is:

```text
*.usap.gpkg
```

USAP does **not** replace CityGML, CityJSON, 3D Tiles, 3DCityDB, COPC, LAS/LAZ, glTF, or GeoPackage.

USAP is an annotation/index layer that connects:

```text
semantic city objects
    -> editable annotations
        -> exact mesh faces / point indices / external feature IDs
            -> external 3D assets
```

The central goal is fast querying and editing of semantic annotations on large 3D urban models.

---

## 1. Core design principle

USAP should not store the whole 3D model.

It should store:

1. References to external 3D assets.
2. Stable asset-part identifiers.
3. Semantic classes.
4. City-object identities.
5. Typed object relationships for query/navigation.
6. Editable annotation records.
7. Exact membership of annotations over model elements.
8. Optional acceleration/derived tables.

The most important row in the whole design is conceptually:

```text
annotation_id
asset_part_id
element_kind
block_start
payload
```

This means:

> For this annotation, inside this exact part of this exact external model, for this kind of element, here are the exact selected elements in this index block.

Everything else gives meaning, provenance, queryability, and consistency to that membership row.

---

## 2. What USAP is for

USAP is designed to support these query families.

### 2.1 Selected elements -> annotations

Example:

```text
I selected faces 100, 101, and 6000 in this mesh primitive.
Tell me which annotations they belong to.
```

USAP should answer this without scanning the whole model.

### 2.2 Semantic class -> elements

Example:

```text
Tell me all RoofSurface faces in this mesh.
```

The result should usually be returned as compact membership blocks, not as millions of expanded element IDs unless requested.

### 2.3 City object -> all model representations

Example:

```text
Find building_1 in all models referenced by this USAP package.
```

or:

```text
Find the roof of building_1 in the mesh, point cloud, and 3D Tiles representation.
```

### 2.4 Editable annotation workflow

USAP is a working file, not only a representation format.

It must support:

```text
create annotation
replace annotation membership
modify annotation status
link/unlink city objects
validate package
rebuild derived indexes
track edits
```

---

## 3. What USAP is not

USAP is not:

```text
a new 3D city model standard
a replacement for CityGML
a replacement for CityJSON
a replacement for 3D Tiles metadata
a replacement for 3DCityDB
a geometry storage format
a multi-user server database
```

USAP is best understood as:

```text
an editable semantic index over immutable or versioned external 3D assets
```

---

## 4. Main conceptual entities

USAP separates five concepts that are often confused.

### 4.1 Asset

An external file or dataset.

Examples:

```text
city_mesh.glb
city_pointcloud.copc.laz
city_model.city.json
city_model.gml
tileset.json
```

### 4.2 Asset part

A stable sub-location inside an asset where element indices are meaningful.

Examples:

```text
node=0/mesh=0/primitive=0
copc/level=12/x=101/y=88/z=5
tiles/12/1034.glb#node=2/mesh=0/primitive=1
```

A face index or point index is only meaningful inside an asset part.

### 4.3 Semantic class

The type of thing.

Examples:

```text
Building
RoofSurface
WallSurface
GroundSurface
Window
Door
ThermalZone
```

Semantic classes may come from:

```text
CityGML 2.0
CityGML 3.0
CityJSON
CityGML ADEs
USAP/custom vocabularies
project-specific schemas
```

### 4.4 City object

The stable identity of a semantic thing.

Examples:

```text
building_1
building_1_roof_1
building_1_wall_north
building_1_door_3
```

A city object can exist even if no external CityGML file is available.

### 4.5 Annotation

An editable claim that connects semantic meaning and/or city-object identity to exact elements in one or more external assets.

Examples:

```text
ann_building_1_roof_mesh
ann_building_1_roof_pointcloud
ann_wall_candidate_auto_42
ann_manual_correction_7
```

A city object is the semantic identity.

An annotation is the editable working object.

---

## 5. Table groups

The USAP schema is organized into these groups:

```text
0. GeoPackage/package metadata
1. External assets
2. Semantic classes
3. City objects and object graphs
4. Annotations
5. Element membership
6. Optional acceleration and provenance
```

---

# 6. Package and GeoPackage metadata

These tables make the package identifiable and GeoPackage-compatible.

## 6.1 `usap_profile`

Purpose:

```text
Identify the file as a USAP package and store profile-level defaults.
```

Recommended structure:

```sql
CREATE TABLE usap_profile (
    profile_id          INTEGER PRIMARY KEY CHECK (profile_id = 1),
    profile_name        TEXT NOT NULL DEFAULT 'USAP',
    profile_version     TEXT NOT NULL,
    default_block_size  INTEGER NOT NULL DEFAULT 4096,
    default_encoding    TEXT NOT NULL DEFAULT 'u32-zlib',
    metadata_json       TEXT
);
```

Notes:

- There should normally be exactly one row.
- `default_block_size` defines the default element-block size used by membership encoding.
- `default_encoding` defines the default payload encoding.

---

# 7. External assets

## 7.1 `usap_asset`

Purpose:

```text
Register external 3D or semantic source assets referenced by the USAP package.
```

Recommended structure:

```sql
CREATE TABLE usap_asset (
    asset_id       INTEGER PRIMARY KEY,
    uri            TEXT NOT NULL,
    asset_kind     TEXT NOT NULL,
    media_type     TEXT,
    content_hash   TEXT,
    srs_id         INTEGER,
    metadata_json  TEXT,

    UNIQUE(uri, content_hash)
);
```

Typical `asset_kind` values:

```text
mesh
pointcloud
citygml
cityjson
3dtiles
gltf
other
```

Notes:

- `uri` may be relative to the USAP file.
- `content_hash` is important for detecting whether an external asset has changed.
- If the asset changes and element indices are not stable anymore, related annotations must be considered stale.

---

## 7.2 `usap_asset_part`

Purpose:

```text
Identify the exact sub-location inside an asset where element indices are valid.
```

Recommended structure:

```sql
CREATE TABLE usap_asset_part (
    asset_part_id   INTEGER PRIMARY KEY,

    asset_id        INTEGER NOT NULL
        REFERENCES usap_asset(asset_id)
        ON DELETE CASCADE,

    part_path       TEXT NOT NULL,
    element_kind    INTEGER NOT NULL,
    element_count   INTEGER NOT NULL,
    index_origin    TEXT NOT NULL DEFAULT 'zero_based',

    minx            REAL,
    miny            REAL,
    minz            REAL,
    maxx            REAL,
    maxy            REAL,
    maxz            REAL,

    metadata_json   TEXT,

    UNIQUE(asset_id, part_path, element_kind)
);
```

Recommended `element_kind` constants:

```text
1 = face
2 = point
3 = vertex
4 = feature
```

Examples:

```text
asset: city_mesh.glb
part_path: node=0/mesh=0/primitive=0
element_kind: face
```

```text
asset: city_pointcloud.copc.laz
part_path: level=12/x=101/y=88/z=5
element_kind: point
```

Why this table is critical:

```text
face 1000 in the whole file is ambiguous.
face 1000 in asset_part_id 7 is precise.
```

---

# 8. Semantic classes

## 8.1 `usap_semantic_class`

Purpose:

```text
Store semantic class references used by city objects and annotations.
```

Recommended structure:

```sql
CREATE TABLE usap_semantic_class (
    semantic_class_id  INTEGER PRIMARY KEY,
    scheme             TEXT NOT NULL,
    scheme_version     TEXT,
    class_uri          TEXT NOT NULL,
    local_name         TEXT NOT NULL,

    parent_class_id    INTEGER
        REFERENCES usap_semantic_class(semantic_class_id),

    is_ade             INTEGER NOT NULL DEFAULT 0,
    metadata_json      TEXT,

    UNIQUE(class_uri)
);
```

Examples:

```text
scheme = citygml
scheme_version = 3.0
class_uri = citygml-3.0:bldg:RoofSurface
local_name = RoofSurface
is_ade = 0
```

```text
scheme = citygml-ade
scheme_version = EnergyADE-2.0
class_uri = energy-ade-2.0:ThermalZone
local_name = ThermalZone
is_ade = 1
```

Notes:

- Do not store only `RoofSurface`.
- Store enough information to know which semantic vocabulary/version defines the class.
- ADE classes should not require structural schema changes.

---

## 8.2 `usap_semantic_class_closure`

Purpose:

```text
Speed up class hierarchy queries, including subclass queries.
```

Recommended structure:

```sql
CREATE TABLE usap_semantic_class_closure (
    ancestor_class_id    INTEGER NOT NULL
        REFERENCES usap_semantic_class(semantic_class_id)
        ON DELETE CASCADE,

    descendant_class_id  INTEGER NOT NULL
        REFERENCES usap_semantic_class(semantic_class_id)
        ON DELETE CASCADE,

    depth                INTEGER NOT NULL,

    PRIMARY KEY (ancestor_class_id, descendant_class_id)
) WITHOUT ROWID;
```

Meaning:

```text
depth = 0 means a class is its own descendant.
depth = 1 means direct child.
depth > 1 means indirect descendant.
```

This table is derived/rebuildable.

---

# 9. City objects and object graphs

This is the part that changed during discussion.

USAP should not impose one universal city-object hierarchy.

Instead:

```text
USAP stores typed object graph edges.
A hierarchy is a chosen graph view used for query/navigation.
```

For phase 1, the only graph can be:

```text
usap_default
```

Later, additional graphs can be added:

```text
citygml_composition
citygml_boundedBy
cityjson_parent_child
topology
manual_review
```

---

## 9.1 `usap_city_object`

Purpose:

```text
Store stable identities of semantic city objects.
```

Recommended structure:

```sql
CREATE TABLE usap_city_object (
    city_object_id     INTEGER PRIMARY KEY,
    object_uid         TEXT NOT NULL UNIQUE,

    semantic_class_id  INTEGER
        REFERENCES usap_semantic_class(semantic_class_id),

    gml_id             TEXT,

    source_asset_id    INTEGER
        REFERENCES usap_asset(asset_id)
        ON DELETE SET NULL,

    source_object_id   TEXT,

    object_status      TEXT NOT NULL DEFAULT 'accepted',

    attributes_json    TEXT
);
```

Recommended `object_status` values:

```text
accepted
draft
inferred
deprecated
conflict
```

Notes:

- `object_uid` is the stable USAP identity.
- `gml_id` is optional CityGML provenance.
- Standalone USAP does not require CityGML.
- City objects should represent semantic objects, not raw faces or points.

---

## 9.2 `usap_city_object_relationship`

Purpose:

```text
Store typed edges between city objects.
```

Final recommended structure:

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

Recommended indexes:

```sql
CREATE INDEX usap_rel_by_parent_graph
ON usap_city_object_relationship(
    graph_name,
    parent_city_object_id,
    relationship_type
);

CREATE INDEX usap_rel_by_child_graph
ON usap_city_object_relationship(
    graph_name,
    child_city_object_id,
    relationship_type
);

CREATE INDEX usap_rel_by_source
ON usap_city_object_relationship(
    source_asset_id,
    source_relation_id
);
```

### Field meanings

#### `relationship_id`

Surrogate primary key.

This allows multiple assertions of the same apparent relationship from different sources.

Example:

```text
building_1 -> roof_1, boundedBy, source = CityGML
building_1 -> roof_1, boundedBy, source = manual edit
```

#### `graph_name`

Identifies which graph/view the relationship belongs to.

For phase 1:

```text
usap_default
```

Later examples:

```text
citygml_composition
citygml_boundedBy
cityjson_parent_child
topology
manual_review
```

This avoids mixing unrelated relationship systems during descendant queries.

#### `relationship_type`

The type of edge.

Examples:

```text
contains
partOf
boundedBy
consistsOf
opening
derivedFrom
associatedWith
```

For `usap_default`, use a small controlled vocabulary in the SDK.

Do not enforce a global database-level vocabulary too early.

#### `role`

Optional role of the child from the parent's point of view.

Examples:

```text
roof
wall
window
door
building_part
```

Recommended rule:

```text
If role is NULL, infer it from the child semantic class where possible.
If role is set, treat it as an explicit project/query role.
```

Do not make `role` mandatory.

#### `source_asset_id`

Optional source/provenance asset.

Example:

```text
The relationship was imported from a CityGML file registered in usap_asset.
```

#### `source_relation_id`

Optional source-specific relationship identifier.

Examples:

```text
gml:bldg_1/boundedBy[2]
cityjson:CityObjects/building_1/children/0
manual:edit_123
```

---

## 9.3 `usap_city_object_closure`

Purpose:

```text
Speed up descendant/ancestor queries for a selected graph.
```

Final recommended structure:

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

Meaning:

```text
depth = 0 means object is descendant of itself in that graph.
depth = 1 means direct child.
depth > 1 means indirect descendant.
```

This table is graph-aware because descendants are meaningful only inside a selected graph.

Examples:

```text
building_1 descendants in usap_default
```

may differ from:

```text
building_1 descendants in citygml_boundedBy
```

This table is derived/rebuildable from `usap_city_object_relationship`.

---

# 10. Annotations

## 10.1 `usap_annotation`

Purpose:

```text
Store editable annotation records.
```

Recommended structure:

```sql
CREATE TABLE usap_annotation (
    annotation_id          INTEGER PRIMARY KEY,
    annotation_uid         TEXT NOT NULL UNIQUE,

    semantic_class_id      INTEGER NOT NULL
        REFERENCES usap_semantic_class(semantic_class_id),

    primary_city_object_id INTEGER
        REFERENCES usap_city_object(city_object_id)
        ON DELETE SET NULL,

    label                  TEXT,
    status                 TEXT NOT NULL DEFAULT 'accepted',
    confidence             REAL,
    attributes_json        TEXT,

    created_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

Recommended `status` values:

```text
accepted
draft
inferred
rejected
superseded
conflict
```

Notes:

- An annotation is editable.
- A city object is a stable semantic identity.
- One city object may have multiple annotations.
- One annotation may refer to elements in multiple assets.

---

## 10.2 `usap_annotation_object`

Purpose:

```text
Link annotations to one or more city objects.
```

Recommended structure:

```sql
CREATE TABLE usap_annotation_object (
    annotation_id   INTEGER NOT NULL
        REFERENCES usap_annotation(annotation_id)
        ON DELETE CASCADE,

    city_object_id  INTEGER NOT NULL
        REFERENCES usap_city_object(city_object_id)
        ON DELETE CASCADE,

    relation_type   TEXT NOT NULL DEFAULT 'represents',

    PRIMARY KEY(annotation_id, city_object_id, relation_type)
) WITHOUT ROWID;
```

Examples of `relation_type`:

```text
represents
candidate_for
corrects
conflicts_with
observes
```

This table is separate from `usap_city_object_relationship`.

`usap_city_object_relationship` answers:

```text
How are city objects related to each other?
```

`usap_annotation_object` answers:

```text
Which city object does this annotation represent or affect?
```

---

# 11. Exact element membership

## 11.1 Why blocked membership is necessary

The naive design would be:

```text
annotation_id | asset_part_id | element_kind | element_index
```

This is simple but dangerous for very large models.

For 40 million faces/points and many annotations, one-row-per-element membership can become too large and too slow for interactive use.

Instead, USAP stores compressed membership blocks.

---

## 11.2 `usap_membership_block`

Purpose:

```text
Store exact element membership using compressed blocks.
```

Recommended structure:

```sql
CREATE TABLE usap_membership_block (
    membership_block_id INTEGER PRIMARY KEY,

    annotation_id       INTEGER NOT NULL
        REFERENCES usap_annotation(annotation_id)
        ON DELETE CASCADE,

    asset_part_id       INTEGER NOT NULL
        REFERENCES usap_asset_part(asset_part_id)
        ON DELETE CASCADE,

    element_kind        INTEGER NOT NULL,

    block_start         INTEGER NOT NULL,
    block_size          INTEGER NOT NULL,
    encoding            TEXT NOT NULL,

    element_count       INTEGER NOT NULL,
    min_element_index   INTEGER NOT NULL,
    max_element_index   INTEGER NOT NULL,

    payload             BLOB NOT NULL,

    UNIQUE(annotation_id, asset_part_id, element_kind, block_start)
);
```

Recommended indexes:

```sql
CREATE INDEX usap_mb_by_element_block
ON usap_membership_block(
    asset_part_id,
    element_kind,
    block_start
);

CREATE INDEX usap_mb_by_annotation
ON usap_membership_block(
    annotation_id,
    asset_part_id,
    element_kind,
    block_start
);
```

### Example

Block size:

```text
4096
```

Annotated faces:

```text
100, 101, 102, 6000, 6001
```

Stored as:

```text
block_start = 0
payload = compressed offsets [100, 101, 102]
```

and:

```text
block_start = 4096
payload = compressed offsets [1904, 1905]
```

because:

```text
6000 - 4096 = 1904
6001 - 4096 = 1905
```

### Recommended phase-1 encoding

```text
u32-zlib
```

Meaning:

```text
sorted unique unsigned 32-bit offsets compressed with zlib
```

This is not necessarily the best final encoding, but it is simple and dependency-free.

Later alternatives:

```text
roaring bitmap
bitset + zstd
varint delta encoding
hybrid dense/sparse encoding
```

---

# 12. Optional acceleration and provenance tables

## 12.1 `usap_annotation_extent_rtree`

Purpose:

```text
Spatial broad-phase filtering for annotations.
```

Recommended structure:

```sql
CREATE VIRTUAL TABLE usap_annotation_extent_rtree USING rtree(
    annotation_id,
    minx, maxx,
    miny, maxy,
    minz, maxz
);
```

This is approximate acceleration only.

Exact membership still comes from:

```text
usap_membership_block
```

---

## 12.2 `usap_feature_id_binding`

Purpose:

```text
Bridge USAP annotations to external feature IDs already present in assets such as 3D Tiles/glTF.
```

Recommended structure:

```sql
CREATE TABLE usap_feature_id_binding (
    binding_id           INTEGER PRIMARY KEY,

    annotation_id        INTEGER NOT NULL
        REFERENCES usap_annotation(annotation_id)
        ON DELETE CASCADE,

    asset_part_id        INTEGER NOT NULL
        REFERENCES usap_asset_part(asset_part_id)
        ON DELETE CASCADE,

    feature_id_set       TEXT NOT NULL,
    external_feature_id  TEXT NOT NULL,

    metadata_json        TEXT,

    UNIQUE(asset_part_id, feature_id_set, external_feature_id, annotation_id)
);
```

This table is not essential for phase 1.

It becomes important when assets already contain feature IDs.

---

## 12.3 `usap_edit_log`

Purpose:

```text
Record edit operations for debugging, provenance, and future undo/redo.
```

Recommended structure:

```sql
CREATE TABLE usap_edit_log (
    edit_id       INTEGER PRIMARY KEY,
    operation     TEXT NOT NULL,
    target_table  TEXT,
    target_id     INTEGER,
    details_json  TEXT,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

This is optional but recommended for a working file.

---

# 13. Canonical versus derived tables

Canonical tables contain the source of truth:

```text
usap_asset
usap_asset_part
usap_semantic_class
usap_city_object
usap_city_object_relationship
usap_annotation
usap_annotation_object
usap_membership_block
usap_feature_id_binding
```

Derived/rebuildable tables:

```text
usap_semantic_class_closure
usap_city_object_closure
usap_annotation_extent_rtree
statistics/cache tables
```

Design rule:

```text
If a derived table becomes inconsistent, the SDK must be able to rebuild it from canonical tables.
```

---

# 14. Core query logic

## 14.1 Selected elements -> annotations

Input:

```text
asset_part_id = 10
element_kind = face
selected elements = [100, 101, 6000]
```

Process:

```text
1. Convert selected element indices to block starts.
2. Query usap_membership_block by asset_part_id, element_kind, block_start.
3. Decode each payload.
4. Check exact offsets.
5. Return matching annotations.
```

Critical index:

```sql
CREATE INDEX usap_mb_by_element_block
ON usap_membership_block(asset_part_id, element_kind, block_start);
```

---

## 14.2 Semantic class -> elements

Input:

```text
semantic class = RoofSurface
```

Process:

```text
1. Find class ID.
2. Use usap_semantic_class_closure to include subclasses if requested.
3. Find annotations with those class IDs.
4. Fetch membership blocks for those annotations.
5. Return compact blocks or expanded elements.
```

---

## 14.3 City object -> elements across models

Input:

```text
object_uid = building_1
include_descendants = true
graph_name = usap_default
```

Process:

```text
1. Find city_object_id for building_1.
2. Use usap_city_object_closure for graph_name = usap_default.
3. Find annotations linked to those city objects.
4. Fetch membership blocks.
5. Group by asset and asset part.
```

Important:

```text
Descendant traversal must always specify graph_name.
```

---

# 15. SDK responsibilities

The SDK should be the normal way to edit USAP.

Direct SQL editing should be discouraged for non-trivial operations.

Minimum SDK operations:

```text
create_package
open_package
validate_package
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
rebuild_derived_indexes
```

Important SDK rules:

```text
Use transactions for edits.
Maintain derived tables after edits or mark them stale.
Validate asset parts before writing membership.
Do not silently accept out-of-range element indices.
Do not assume CityGML exists.
Do not expand huge membership results unless explicitly requested.
```

---

# 16. Phase-1 Python toolchain

Keep the first implementation simple.

Required tools:

```text
Python 3.11+
Python standard library sqlite3
SQLite command-line tool
DB Browser for SQLite, optional
Git
```

Avoid at first:

```text
SQLAlchemy
GeoPandas
PostGIS
full CityGML parser
full glTF parser
full point-cloud parser
```

The first prototype should prove the core data model, not solve every import/export case.

---

# 17. Development roadmap

## Phase 0 — Manual schema understanding

Goal:

```text
Understand the database model with a tiny manual dataset.
```

Tasks:

```text
Create plain SQLite/GeoPackage-style file.
Create core tables.
Insert one mesh asset.
Insert one asset part.
Insert Building and RoofSurface classes.
Insert one building and one roof city object.
Link building -> roof in usap_default.
Create one roof annotation.
Store five face IDs as membership blocks.
Inspect everything in DB Browser for SQLite.
```

Success criteria:

```text
You can explain every row in the file.
You can manually trace face 100 -> annotation -> roof -> building.
```

---

## Phase 1 — Core SDK and synthetic benchmark

Goal:

```text
Build the basic USAP Python SDK and prove the compressed block membership design.
```

Scope:

```text
No real CityGML parser.
No real mesh parser.
No real point-cloud parser.
No viewer integration.
Synthetic assets only.
```

Features:

```text
create/open package
register assets and asset parts
register semantic classes
create city objects
create graph relationships with graph_name = usap_default
create annotations
replace annotation membership
query selected elements -> annotations
query annotation -> elements/blocks
query semantic class -> blocks
query city object -> blocks using usap_default closure
validate database consistency
run synthetic benchmarks
```

Benchmarks:

```text
Q1: 100 selected faces in 1 block -> annotations
Q2: 1000 selected faces across 20 blocks -> annotations
Q3: all RoofSurface blocks in one asset
Q4: building_1 descendants -> annotations -> membership blocks
Q5: replace one annotation with 5000 faces
```

Success criteria:

```text
The block model is measurably faster and smaller than naive row-per-element storage for large synthetic data.
Queries do not scan the whole model.
The SDK can rebuild closure tables.
The SDK can validate obvious inconsistencies.
```

---

## Phase 2 — Minimal real geometry adapters

Goal:

```text
Connect USAP to real external model assets without trying to own geometry storage.
```

Adapters to build:

```text
glTF/GLB primitive locator
OBJ/PLY mesh test adapter, optional
LAS/LAZ point-index adapter, optional
CityJSON object/semantic importer, preferred before CityGML
```

Key design work:

```text
Define stable part_path conventions.
Define element_kind precisely per format.
Define asset-part content hashing.
Detect when external assets changed.
Mark affected membership as stale.
```

Success criteria:

```text
A real mesh primitive can be registered as an asset part.
Face IDs from a viewer or script can be mapped to USAP membership.
A CityJSON file can provide initial city objects and semantic classes.
```

---

## Phase 3 — Editing workflow and validation

Goal:

```text
Make USAP reliable as a working file.
```

Features:

```text
transactional edit operations
edit log
annotation status workflow
object status workflow
stale asset detection
membership replacement
membership patching
validation reports
rebuild derived tables
optional annotation extents
```

Validation checks:

```text
orphan annotations
orphan membership blocks
invalid asset parts
out-of-range element indices
missing semantic classes
missing closure rows
cycles in hierarchy-like graphs, if disallowed for a graph
changed asset hashes
unsupported payload encodings
```

Success criteria:

```text
A user can edit annotations repeatedly without corrupting the package.
Derived tables can be deleted and rebuilt.
Validation produces useful human-readable reports.
```

---

## Phase 4 — CityJSON and CityGML semantic import

Goal:

```text
Use external semantic models as sources of object identity and semantic structure.
```

Recommended order:

```text
CityJSON first
CityGML second
```

Reason:

```text
CityJSON is usually simpler to parse and map into tables.
CityGML has richer structure and more complex relationships.
```

CityJSON tasks:

```text
import CityObjects as usap_city_object
import parent/child relationships into graph_name = cityjson_parent_child
create or map semantic classes
record source_asset_id and source_object_id
optionally create usap_default from CityJSON hierarchy
```

CityGML tasks:

```text
import gml:id as provenance
map CityGML classes to usap_semantic_class
import selected relationships as typed graph edges
preserve source relationship provenance
create graph_name values such as citygml_composition and citygml_boundedBy
optionally derive usap_default from selected CityGML edges
```

Success criteria:

```text
USAP can be CityGML-linked but still query annotations without reparsing CityGML at runtime.
USAP can be standalone when no CityGML/CityJSON source exists.
Multiple relationship graphs can coexist without ambiguity.
```

---

## Phase 5 — ADE support

Goal:

```text
Allow ADE-defined semantic concepts without changing the core USAP structure.
```

Design rule:

```text
ADEs add semantic classes, attributes, and relationships.
They should not require new core membership tables.
```

Tasks:

```text
register ADE semantic classes in usap_semantic_class
set is_ade = 1
store ADE namespace/version in scheme and scheme_version
store ADE-specific attributes in attributes_json or extension tables
extend semantic class closure for ADE subclass relationships
preserve source provenance to CityGML/ADE source asset
```

Possible extension mechanism:

```text
usap_extension_registry
usap_attribute_schema
project/ADE-specific attribute tables
```

Success criteria:

```text
EnergyADE or another ADE can define classes such as ThermalZone without changing usap_membership_block.
Queries by superclass can include ADE subclasses through usap_semantic_class_closure.
```

---

## Phase 6 — 3D Tiles / glTF feature metadata integration

Goal:

```text
Avoid duplicating exact element membership when external assets already contain feature IDs.
```

Tasks:

```text
support asset parts for 3D Tiles tile contents
support glTF primitive feature ID sets
store mappings in usap_feature_id_binding
allow viewer selection by feature ID
resolve feature ID -> annotation -> city object
```

Success criteria:

```text
A 3D Tiles/glTF viewer can pick a feature and resolve it to USAP annotations.
USAP can use feature IDs when available and membership blocks when exact face/point membership is needed.
```

---

## Phase 7 — Point-cloud scale support

Goal:

```text
Make point annotations practical on large point clouds.
```

Tasks:

```text
define stable point indexing per chunk
support COPC/EPT/LAS asset-part conventions
test block membership on sparse and dense point selections
avoid huge expanded point-ID lists
return compact blocks to clients
add chunk-level spatial acceleration
```

Success criteria:

```text
USAP can store and query point-level annotations on large point clouds without exploding file size or query time.
```

---

## Phase 8 — Application/viewer integration

Goal:

```text
Use USAP in an annotation workflow with actual 3D selection.
```

Possible integrations:

```text
QGIS plugin, if useful for 2D/3D GIS workflows
Blender plugin, useful for mesh annotation experiments
Cesium/3D Tiles viewer integration
custom web viewer
Python desktop viewer
```

Required viewer capabilities:

```text
select face/point/feature
send asset_part_id + element_kind + element indices to SDK
receive annotations
display semantic object tree from usap_default
create/modify annotation membership
save edits transactionally
```

Success criteria:

```text
A user can select part of a model and see/edit semantic annotations stored in USAP.
```

---

## Phase 9 — Robust packaging and interoperability

Goal:

```text
Make USAP a clean, documented, reusable profile/extension.
```

Tasks:

```text
formalize .usap.gpkg package rules
register USAP tables in GeoPackage metadata tables
write profile documentation
write migration system
write conformance/validation tests
define JSON metadata schemas
support package export/import
support version upgrades
```

Success criteria:

```text
A USAP file can be created, validated, migrated, and used by independent tools that follow the profile.
```

---

## Phase 10 — Endgame: CityGML/ADE-aware semantic annotation ecosystem

Goal:

```text
USAP becomes a practical bridge between semantic city models and high-performance 3D assets.
```

Endgame capabilities:

```text
CityGML-linked USAP packages
Standalone USAP packages
CityJSON-linked packages
3D Tiles/glTF feature metadata integration
point-cloud chunk annotation
ADE semantic class support
multi-graph city-object relationships
fast viewer selection queries
editable annotation workflow
validation and provenance
export back to semantic sources where possible
```

Important endgame principle:

```text
CityGML remains the semantic authority/reference when present.
USAP remains the query/edit index.
```

USAP should not attempt to mirror all CityGML content.

It should intentionally mirror only what is needed for:

```text
identity
semantic class
object graph/query navigation
annotation membership
provenance
```

Content that may remain in external CityGML/CityJSON:

```text
full LoD geometries
appearances
addresses
generic attributes
XLinks
complex ADE XML structures
metadata
source-specific validation context
```

---

# 18. Recommended implementation order

Build in this exact order:

```text
1. Plain SQLite schema
2. Tiny manual example
3. Membership block encoder/decoder
4. annotations_for_elements query
5. elements_for_annotation query
6. semantic class query
7. usap_default city-object graph
8. graph-aware closure table
9. synthetic generator
10. benchmark script
11. validation script
12. minimal GeoPackage metadata
13. real asset adapters
14. CityJSON import
15. CityGML import
16. ADE support
```

Do not start with CityGML parsing.

Do not start with a viewer.

Do not start with optimization beyond the membership block model.

The first serious proof is:

```text
Can USAP answer selected elements -> annotations quickly on synthetic large data?
```

---

# 19. Main technical risks

## 19.1 Stable element identity

This is the biggest risk.

If a mesh is re-exported, simplified, reordered, or triangulated differently, face indices may change.

USAP must treat this seriously.

Required safeguards:

```text
asset content hash
asset-part path
asset-part element count
optional asset-part/accessor hash
stale membership detection
validation warnings
```

---

## 19.2 Large point clouds

Point clouds can be much larger than meshes.

Rules:

```text
Use chunk-local point indices.
Avoid global point-index assumptions unless guaranteed.
Return compact blocks.
Do not expand millions of point IDs unnecessarily.
```

---

## 19.3 Relationship ambiguity

CityGML, CityJSON, manual edits, topology, and workflow links may all define different relationships.

Rule:

```text
Store typed graph edges.
Always query descendants with an explicit graph_name.
```

---

## 19.4 SQLite write concurrency

SQLite is good for a local working file.

It is not a multi-user annotation server.

Rule:

```text
Use USAP for local/snapshot editing.
Use server infrastructure for concurrent multi-user editing.
```

---

## 19.5 Over-expansion of results

Returning millions of face IDs to Python can be slower than the database query itself.

Rule:

```text
Return compact membership blocks by default.
Expand only when explicitly requested.
```

---

# 20. Final accepted relationship design summary

The accepted final design for object relationships is:

```text
USAP stores object relationships as typed graph edges.
The default query/navigation graph is named usap_default.
The relationship table is generic enough to later store CityGML, CityJSON, topology, and manual graphs.
The closure table is graph-aware.
Role is optional and may be inferred from child semantic class.
```

Final table:

```sql
CREATE TABLE usap_city_object_relationship (
    relationship_id        INTEGER PRIMARY KEY,
    graph_name             TEXT NOT NULL DEFAULT 'usap_default',
    parent_city_object_id  INTEGER NOT NULL REFERENCES usap_city_object(city_object_id) ON DELETE CASCADE,
    child_city_object_id   INTEGER NOT NULL REFERENCES usap_city_object(city_object_id) ON DELETE CASCADE,
    relationship_type      TEXT NOT NULL,
    role                   TEXT,
    source_asset_id        INTEGER REFERENCES usap_asset(asset_id) ON DELETE SET NULL,
    source_relation_id     TEXT,
    metadata_json          TEXT
);
```

Final closure table:

```sql
CREATE TABLE usap_city_object_closure (
    graph_name                  TEXT NOT NULL,
    ancestor_city_object_id     INTEGER NOT NULL REFERENCES usap_city_object(city_object_id) ON DELETE CASCADE,
    descendant_city_object_id   INTEGER NOT NULL REFERENCES usap_city_object(city_object_id) ON DELETE CASCADE,
    depth                       INTEGER NOT NULL,

    PRIMARY KEY (
        graph_name,
        ancestor_city_object_id,
        descendant_city_object_id
    )
) WITHOUT ROWID;
```

This is the recommended basis for the next implementation step.
