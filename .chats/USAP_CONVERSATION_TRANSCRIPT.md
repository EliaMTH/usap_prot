
# USAP Conversation Transcript

Generated from the visible conversation context. Hidden reasoning and internal tool logs are excluded.

---

## User

I have to write a datastructure to encode semantic annotation on urban 3D models. I have this design idea that i call Urban Semantic Annotation Package (USAP).

USAP is a GeoPackage-based prototype/profile for storing editable, exact, element-level semantic annotations over external 3D urban assets.

The package file extension is:

```text
*.usap.gpkg
```

USAP does not replace CityGML, CityJSON, 3D Tiles, 3DCityDB, or GeoPackage. It uses GeoPackage as the writable SQLite container and adds USAP-specific tables for mesh-face and point-index annotation membership linked to city-object identities. Those identities may come from CityGML/CityJSON, but the core annotation/query engine does not require reparsing CityGML at runtime.

3D model, in this case, is everything: pointcloud, triangulated mesh, cityGML models. Multiple models can be associated to the USAP file, and their "connection" is on a semantic level (a roof is saved as a semantic annotation and refers to different geometries in the models, but i can refer to both with the only concept of "that roof id")

I don't want to define a new standard, but use standard tools and combine them to create a compact efficient way to quickly:

- query 3D models elements ("I'm selecting a portion of this model, tell me the annotations of the selected elements")
- query the annotation on a model ("tell me all the roofs in this 3D model (vertices or points)")
- query an object id on all associated models, and/or subparts ("find building_1 in the models referred in USAP" or "find the roof in building_1 in all the models referred in USAP").

Annotation in 3D models use the cityGML concepts. THey will probably require to have their own ID, but the semantic concepts attached to it will follow cityGML standard and eventual ADE. ADE addition should not influence the overall structure.

CityGML is optional but useful in USAP. It should be understood as a semantic source/reference asset, not as the database used by normal annotation queries.

USAP stores a query-optimized mirror of the city-object graph in usap_city_object and usap_city_object_relationship, and it stores semantic class references in usap_semantic_class. Therefore, queries such as "which annotations belong to this object?", "which faces are RoofSurface?", or "which points belong to this annotation?" can run entirely from the GeoPackage tables plus the external geometry assets.

CityGML remains useful for three separate reasons:

External semantic authority. usap_semantic_class stores references such as citygml-3.0:Building:RoofSurface; the CityGML standard/schema defines the formal meaning and validation context behind those references.
Source identity/provenance. usap_city_object.gml_id, source_asset_id, and source_object_id can point back to the original gml:id in a CityGML file.
Information completeness. A CityGML source may contain rich content that USAP intentionally does not mirror, such as LoD geometry, appearances, addresses, generic attributes, XLinks, ADE XML, metadata, and complex nested structures.
A package can therefore be used in two modes:

```text
CityGML-linked USAP:
  .usap.gpkg references a sidecar CityGML/CityJSON source asset.

Standalone USAP:
  .usap.gpkg stores city objects, relationships, and semantic classes directly,
  without requiring a CityGML file.
```

The synthetic generator creates the CityGML-linked mode by default because it is useful for viewer demos and provenance. The backend treats the CityGML tree/preview as optional: if no parseable CityGML source is loaded, annotation queries still operate on the USAP tables.

The most critical aspect is that this must be "as quick as possible" in doing the queries. I'm not sure an unique SQLite table can do the trick, so i'm thingking that different tables could serve different queries. Of course, redundancy is expected but should be kept at a minimum.

Since USAP is intended as a working file, not just representative, I need tools to edit the annotations, and to keep tables consistent.

The product i envision is composed by a SKD that allows to query USAP (and thus the models or its annotation) and make basic edit operations.

This have to work on large, complex citymodels (let's say 40M) reasonably fast, and significant number of different annotations.

I need help polishing this idea in three, i think consecutive phases.

1) Validating the approach: can this work? What are the suggested changes? IS there something already that proposes the same thing that i can use instead?
2) polishing the design: would this sqlite design work? what do i have to make clear before trying a prototype?
3) can you help me building a prototype? I'm used to python.

This is serious businnes: no sugarcode truth, be honest, keep the design and code as simple as you can. If this work, conceptually should not be too complex.

---

## Assistant

Trattero questo come una revisione tecnica, non come una promozione: prima verifichero sovrapposizioni con standard esistenti, poi schema, query critiche, consistenza in modifica e prototipo Python.

Primo esito: non sembra esistere gia uno standard identico; l'idea e plausibile come indice/overlay semantico editabile, ma il rischio centrale e l'identita stabile degli elementi.

### 1) Validation: yes, but only under strict conditions

Your idea is viable as a working-file index/annotation layer, not as a replacement 3D city format. The key architectural decision is good: keep heavy geometry in external assets, keep semantic identities and editable annotation memberships in a writable SQLite/GeoPackage container.

This can work if:

1. Element identity is immutable and precisely defined. A face index or point index is meaningless unless you define the exact asset version, tile, mesh primitive, point-cloud chunk, index origin, and ordering.
2. You do not store one row per point/face unless the dataset is small or sparse. SQLite can handle tens of millions of rows, but one-row-per-element-per-annotation will become bulky, slow to edit, and hard to keep interactive.
3. Query outputs are allowed to be compact references. "Give me all roof points" should normally return blocks/ranges/bitmaps of point IDs, not immediately expand millions of IDs into a Python list.
4. You accept that generic GeoPackage clients will not understand USAP. It will be an Extended GeoPackage with custom USAP tables.
5. You keep CityGML as semantic authority/provenance, not as the runtime query engine.

The biggest recommended change was:

> Do not make annotation membership a plain many-to-many table between annotation and element index. Make it a blocked compressed membership index.

That gives fast forward and reverse queries without exploding the database.

### 2) Existing technologies nearby

There is no single existing standard that does exactly what USAP proposes: editable element-level semantic annotation over multiple external 3D urban assets, with one semantic identity graph shared across meshes, point clouds, CityGML/CityJSON, and tiled visualization formats.

Nearby technologies:

- GeoPackage Related Tables and Semantic Annotations ideas: useful for discoverability and registration, but not enough for exact mesh-face/point-index membership.
- CityGML / CityJSON / FlatCityBuf: good as source/semantic formats, not as the editable cross-asset annotation layer.
- 3DCityDB: useful if the primary problem is CityGML/CityJSON storage and querying in a server database, but not a compact single working file.
- 3D Tiles / glTF metadata: very relevant for runtime feature picking, especially if feature IDs already exist.
- COPC / EPT / LAS: useful point-cloud assets, not the semantic city-object database.

USAP is justified if the requirement is cross-representation, editable, exact semantic membership.

### 3) Recommended USAP core model

The proposed split was:

```text
A. Package and asset registry
B. Semantic class registry
C. City object graph mirror
D. Annotation objects
E. Element membership index
```

#### A. Package and asset registry

Tables:

```text
usap_package
usap_asset
usap_asset_part
usap_asset_version
```

The important concept is asset part, not just asset. A glTF asset may contain nodes, meshes, primitives, and index buffers. A 3D Tiles asset has tiles and glTF contents. A point cloud has chunks. USAP needs stable references like:

```text
asset_id = 3
asset_part_id = "tiles/12/1034.glb#node=4/mesh=2/primitive=0"
element_kind = face
element_index = 15231
```

For point clouds:

```text
asset_id = 8
asset_part_id = "copc://level=12/x=103/y=421/z=8"
element_kind = point
element_index = 3912
```

#### B. Semantic class registry

Tables:

```text
usap_semantic_class
usap_semantic_class_closure
```

A semantic class should store scheme, version, URI/QName, local name, parent class, and ADE flag.

A closure table supports fast subclass queries, for example "RoofSurface and all subclasses".

#### C. City object graph mirror

Tables:

```text
usap_city_object
usap_city_object_relationship
usap_city_object_closure
```

`usap_city_object` stores stable USAP object identities and provenance:

```text
city_object_id       INTEGER PRIMARY KEY
object_uid           TEXT UNIQUE
semantic_class_id    INTEGER
gml_id               TEXT NULL
source_asset_id      INTEGER NULL
source_object_id     TEXT NULL
attributes_json      TEXT NULL
```

The relationship table stores object-to-object relationships. A closure table makes descendant queries fast.

#### D. Annotation objects

Tables:

```text
usap_annotation
usap_annotation_object
```

An annotation is the editable working claim. A city object is the semantic identity. Often they are one-to-one, but not always.

#### E. Element membership index

The critical table:

```text
usap_membership_block
```

Do not start with one row per annotated element. Use block-compressed membership.

Each row stores all selected elements of one annotation inside one fixed index block.

Example with block size 4096:

```text
annotation_id = roof_123
asset_part_id = mesh_17
element_kind = face
block_start = 8192
payload = compressed list/bitmap of selected offsets inside [8192, 12287]
```

Recommended columns:

```sql
CREATE TABLE usap_membership_block (
    membership_block_id INTEGER PRIMARY KEY,
    annotation_id       INTEGER NOT NULL,
    asset_part_id       INTEGER NOT NULL,
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

CREATE INDEX usap_mb_by_element_block
ON usap_membership_block(asset_part_id, element_kind, block_start);

CREATE INDEX usap_mb_by_annotation
ON usap_membership_block(annotation_id, asset_part_id, element_kind, block_start);
```

This supports:

- selected elements to annotations
- annotation/class to model elements
- city object to all assets/subparts

### 4) Optional acceleration tables

- annotation bounding boxes and SQLite RTree as broad-phase query acceleration
- external feature-ID bindings for glTF/3D Tiles features
- semantic class and city object closure tables

### 5) Design choices to avoid

- Avoid storing raw 3D geometry in USAP by default.
- Avoid relying on CityGML parsing at runtime.
- Avoid using triggers for complex consistency.
- Avoid assuming SQLite is multi-user server infrastructure.
- Avoid premature `WITHOUT ROWID` everywhere.

### 6) Minimal prototype schema

The assistant proposed a phase-1 SQL schema with:

```text
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
usap_annotation_extent_rtree
```

### 7) First Python prototype

The assistant proposed standard-library Python using `sqlite3`, `array`, and `zlib`, including functions for:

- connecting to a `.usap.gpkg`
- encoding offsets into compressed BLOBs
- replacing annotation membership
- querying annotations for selected elements
- expanding elements for an annotation

The main design principle was: simple first, benchmark before adding complexity.

### 8) Performance expectations

For a 40M element asset with block size 4096:

```text
40,000,000 / 4096 = about 9766 blocks
```

Interactive selection speed depends on how many blocks are touched, not directly on the full size of the model.

### 9) Editing and consistency

Canonical tables:

```text
usap_asset
usap_asset_part
usap_semantic_class
usap_city_object
usap_city_object_relationship
usap_annotation
usap_annotation_object
usap_membership_block
```

Derived/rebuildable tables:

```text
usap_annotation_extent_rtree
usap_city_object_closure
usap_semantic_class_closure
statistics/cache tables
```

The SDK should handle edits transactionally and provide validators/rebuilders.

### 10) Required definitions before a real prototype

Important questions:

1. What is an element?
2. What is the stable index coordinate system?
3. What happens when assets change?
4. Are overlapping annotations allowed?
5. Can one annotation refer to multiple assets?
6. Are annotations always CityGML classes?
7. Do you need exact geometry queries or only element queries?

### Bottom line

USAP is conceptually sound if defined as:

> A GeoPackage-based editable semantic index over immutable external 3D assets, using stable asset-part element references and compressed membership blocks.

---

## User

Prepare a repository with the following:
SDK for phase 1. Then prepare a readme with project description and all the development phases (inclyuding reaching the "endgame" with the cityGML/ADE inclusion. 
If possible, in python

---

## Assistant

Preparerò un piccolo repository Python eseguibile per la Fase 1, con schema SQLite/GeoPackage-style, SDK, generatori sintetici, benchmark, test e README. Il README distinguerà il prototipo immediato dalla roadmap completa fino all'integrazione CityGML/ADE.

Aggiungerò solo un bootstrap GeoPackage minimale, senza fingere che sia già un'estensione completa. La Fase 1 resterà conservativa: niente parser CityGML, loader geometrici o dipendenze binarie.

Lo scheletro del repository e l'SDK core sono ora pronti, inclusi codec membership, API, esempi, benchmark scaffold e test. Sto validando e rifinendo il pacchetto finale.

Done. I prepared the phase-1 Python repository and packaged it as a zip.

Download link provided:

```text
/mnt/data/usap_phase1_repo.zip
```

Key files:

```text
README.md
src/usap/sdk.py
sql/usap_phase1_schema.sql
tests/test_sdk.py
scripts/benchmark_phase1.py
examples/quickstart.py
```

Repository layout:

```text
usap_phase1_repo/
  pyproject.toml
  README.md
  LICENSE
  TEST_RESULTS.md
  sql/usap_phase1_schema.sql
  src/usap/
    __init__.py
    cli.py
    constants.py
    encoding.py
    models.py
    schema.py
    sdk.py
    synthetic.py
  examples/quickstart.py
  scripts/benchmark_phase1.py
  tests/test_sdk.py
```

The SDK supports:

```text
create/open .usap.gpkg
register external assets and asset parts
register semantic classes
bootstrap a small CityGML-inspired class tree
create city objects and parent/child relationships
create annotations
link annotations to city objects
store exact face/point membership in compressed blocks
query selected elements -> annotations
query annotation/class/city object -> compact membership blocks
expand blocks to raw element IDs when needed
generate synthetic packages
validate consistency
run a small benchmark
```

Validation commands performed:

```bash
cd /mnt/data/usap_phase1_repo
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m compileall -q src tests examples scripts
PYTHONPATH=src python3 scripts/benchmark_phase1.py --buildings 20 --mesh-faces 20000 --repeat 3
```

The unit tests passed. Real CityGML/CityJSON/glTF/COPC parsers were not implemented yet.

---

## User

I'm lost, i need to build it myself, help me. First, I need to understand the logic behind the tables u decided to build, why, and their structure. Let's discuss that and the tools I need to use.

---

## Assistant

Ripartiamo con calma dal modello mentale di USAP: cosa conserva, perche ogni tabella esiste e quali query abilita. Poi distingueremo tabelle essenziali, derivate e strumenti Python necessari.

La semplificazione chiave e che USAP non salva il modello 3D: salva riferimenti stabili agli elementi, identita semantiche e membership esatte; il resto serve a renderli veloci/editabili.

The explanation was structured as follows.

### Core mental model

USAP pipeline:

```text
external 3D file
    -> stable asset part
    -> element IDs: face 10, face 11, point 500, ...
    -> annotation membership
    -> semantic annotation
    -> city object identity
    -> CityGML / CityJSON / ADE meaning and provenance
```

Important separation:

```text
Semantic class  = what kind of thing it is, e.g. RoofSurface
City object     = which real/conceptual object it is, e.g. building_1_roof_1
Annotation      = the editable working claim
Membership      = the exact mesh faces / points / elements selected
```

This separation lets the same roof exist in multiple models: mesh, point cloud, CityGML, CityJSON, 3D Tiles, etc.

### Layers of tables

```text
Layer 0: Package / GeoPackage housekeeping
Layer 1: External assets
Layer 2: Semantic classes
Layer 3: City objects
Layer 4: Annotations
Layer 5: Exact element membership
```

### Layer 0 - package and GeoPackage housekeeping

Tables:

```text
gpkg_spatial_ref_sys
gpkg_contents
gpkg_extensions
usap_profile
```

These identify CRS, contents, extensions/profile information. They are not the core logic.

### Layer 1 - external assets

Tables:

```text
usap_asset
usap_asset_part
```

`usap_asset` stores external files such as GLB, COPC, CityJSON, GML, 3D Tiles. `content_hash` is important because face/point indices are meaningful only if the file version is stable.

`usap_asset_part` stores the precise part of an asset where element indices live. A whole asset is too vague.

Example:

```text
asset_part_id = 10
asset_id = 1
part_path = "node=0/mesh=0/primitive=0"
element_kind = face
element_count = 1000000
```

### Layer 2 - semantic classes

Tables:

```text
usap_semantic_class
usap_semantic_class_closure
```

A semantic class is a type, not an individual object.

Examples:

```text
Building
RoofSurface
WallSurface
GroundSurface
Window
Door
ThermalZone
SolarPanel
```

`usap_semantic_class_closure` supports fast subclass queries.

### Layer 3 - city objects

Tables:

```text
usap_city_object
usap_city_object_relationship
usap_city_object_closure
```

A city object is an actual semantic object, such as:

```text
building_1
building_1_roof_1
building_1_wall_north
```

`object_uid` is the stable USAP identifier; `gml_id` is optional provenance.

`usap_city_object_relationship` stores parent-child/object relationships. `usap_city_object_closure` stores precomputed descendant relationships for speed.

### Layer 4 - annotations

Tables:

```text
usap_annotation
usap_annotation_object
```

An annotation is an editable working object that points to model elements. It can be draft, accepted, automatically generated, manually corrected, etc.

### Layer 5 - exact element membership

Table:

```text
usap_membership_block
```

This stores exact membership in blocks rather than one row per element.

With block size 4096:

```text
block 0      = indices 0 - 4095
block 4096   = indices 4096 - 8191
block 8192   = indices 8192 - 12287
```

If a roof contains faces:

```text
100, 101, 102, 6000, 6001
```

USAP stores two membership block rows:

```text
block_start = 0, payload = compressed offsets [100,101,102]
block_start = 4096, payload = compressed offsets [1904,1905]
```

This is fast because selected element queries touch only relevant blocks.

### Helper tables

- `usap_feature_id_binding` for future glTF/3D Tiles feature IDs.
- `usap_annotation_extent_rtree` for spatial broad-phase filtering.
- `usap_edit_log` for edit provenance.

### Main queries

A. Selected model elements -> annotations.

B. Semantic class -> all model elements, e.g. all roofs.

C. City object -> all related geometry across assets.

### Canonical vs derived tables

Canonical:

```text
usap_asset
usap_asset_part
usap_semantic_class
usap_city_object
usap_city_object_relationship
usap_annotation
usap_annotation_object
usap_membership_block
```

Derived/rebuildable:

```text
usap_semantic_class_closure
usap_city_object_closure
usap_annotation_extent_rtree
```

### Tools recommended

Required:

```text
Python 3.11 or 3.12
sqlite3 standard library
SQLite command-line tool
DB Browser for SQLite
Git
VS Code / PyCharm / similar
```

Avoid at first:

```text
SQLAlchemy
GeoPandas
PostGIS
full CityGML parser
```

The first milestone should be one tiny example with one mesh asset, one primitive, one building, one roof, and five annotated faces.

---

## User

Question: to my understanding, this implies that i have to decide a city object hierarchy, somewhere. Is this correct? If not explain why, if it is, where hsould i derive it? or should i choose myself?

I assume I can invent it for 3D models without annotation/semantic and that  i can get it from cityGML/cityJSON if available, but in cityGML 3 i have multiple relationships, thus multiple tree and thus multiple hierarichies

---

## Assistant

Si, questa e una delle domande strutturali piu importanti. La risposta breve e: USAP non deve imporre "la" gerarchia unica; deve registrare relazioni tipizzate e, solo per alcune query, materializzare viste gerarchiche scelte.

Answer:

Yes, somewhere USAP needs object relationships. But it does not need one universal city-object hierarchy.

Better design:

```text
USAP stores a typed object graph.
Specific queries may choose one graph view as a hierarchy.
```

CityGML 3 is not just a single parent-child tree; it is a conceptual model with multiple types of relationships. USAP should not flatten it into one forced tree and pretend that is the only truth.

### Relationship table versus hierarchy

The table stores edges. A hierarchy is a chosen interpretation of some edges.

Examples of edges:

```text
building_1 -> roof_1       boundedBy
building_1 -> wall_1       boundedBy
wall_1     -> window_1     opening
roof_1     -> wall_1       adjacentTo
building_1 -> address_1    hasAddress
```

If all edges are traversed as descendants, the result is nonsense.

### Recommended change

`usap_city_object_relationship` should be able to include:

```text
relationship_id
graph_name
parent_city_object_id
child_city_object_id
relationship_type
role
source_asset_id
source_relation_id
is_default_navigation
metadata_json
```

And closure should include:

```text
graph_name
ancestor_city_object_id
descendant_city_object_id
depth
```

Possible graph names:

```text
usap_default
citygml_composition
citygml_boundary
source_file_containment
annotation_working_graph
topology
```

### Practical rule

For phase 1, use only:

```text
graph_name = "usap_default"
```

and store relationships for practical query/navigation, such as:

```text
Building -> RoofSurface
Building -> WallSurface
Building -> GroundSurface
WallSurface -> Window
WallSurface -> Door
```

Later, CityGML/CityJSON importers can preserve multiple graph systems.

### Default hierarchy

For viewer/editor usability, define a `usap_default` graph. Its purpose is practical:

```text
show object tree in UI
query building and subparts
query roof of building
group annotations under objects
```

For buildings, the default can usually be:

```text
Building
  BuildingPart
    BoundarySurface
      Opening
```

or, without building parts:

```text
Building
  RoofSurface
  WallSurface
    Window
    Door
  GroundSurface
```

### Main recommendation

Do not decide one universal hierarchy. Decide one default query hierarchy, and preserve imported relationships as typed edges with provenance.

---

## User

Ok on building a usap_default, but I'm lost: if I the relations in usap_city_object_relationship can be defined during the USAP creation and are just for query purposes (not holding truth in semantic meaning) and I can assign any relationship type I want, why does its design needs to change for cityGML 3? isn't this structure already sufficient?

---

## Assistant

You are basically right.

The current structure:

```text
usap_city_object_relationship
  parent_city_object_id
  child_city_object_id
  relationship_type
  role
```

is conceptually sufficient to store CityGML-derived relationships, manually invented relationships, and USAP query relationships.

The reason for suggesting a change is not because CityGML 3 requires a totally different table. It is because multiple relationship systems need enough metadata to avoid ambiguity.

The real issue is:

```text
Can I later tell which relationships belong to which query graph,
which source they came from,
and which ones should be used for default descendant queries?
```

That is where extra fields help.

### Relationship table vs hierarchy

`usap_city_object_relationship` stores edges. A hierarchy is a chosen interpretation of some edges.

If all edges are used in descendant queries, you may accidentally include topology, metadata, provenance, or unrelated relations.

### Why `graph_name`

Without `graph_name`, every query must remember which relationship types are safe to traverse. With `graph_name`, this is explicit:

```text
graph_name = usap_default
  building_1 -> roof_1
  building_1 -> wall_1
  wall_1     -> window_1

graph_name = topology
  roof_1 -> wall_1
  wall_1 -> ground_1
```

Then a query can say:

```python
elements_for_city_object(
    "building_1",
    include_descendants=True,
    graph_name="usap_default",
)
```

### Is `graph_name` mandatory for phase 1?

No. For phase 1, the original simpler table is enough if you decide:

```text
For now, this table contains only usap_default navigation relationships.
```

### Minimal future-proof version

Recommended:

```sql
CREATE TABLE usap_city_object_relationship (
    relationship_id        INTEGER PRIMARY KEY,
    graph_name             TEXT NOT NULL DEFAULT 'usap_default',
    parent_city_object_id  INTEGER NOT NULL,
    child_city_object_id   INTEGER NOT NULL,
    relationship_type      TEXT NOT NULL,
    role                   TEXT,
    source_asset_id        INTEGER,
    source_relation_id     TEXT,
    is_default_navigation  INTEGER NOT NULL DEFAULT 1,
    metadata_json          TEXT
);
```

The core idea does not change. It just records graph, source, and default-navigation state.

### Why relationship_id

A composite primary key may prevent storing several assertions of the same apparent relationship from different sources. A `relationship_id` allows provenance-heavy duplicates if needed.

### Final answer

The current structure is sufficient for phase 1. It only needs to change for CityGML 3 if you want to preserve multiple relationship systems without ambiguity.

Phase 1:

```text
usap_city_object_relationship = usap_default query/navigation edges
```

Later:

```text
usap_city_object_relationship = generic typed graph edges
usap_city_object_closure = graph-specific hierarchy/materialized traversal
```

---

## User

Can you present me again then the final form of usap_city_object_relationship and what other tables changes accordingly?

My last version is 

```text
usap_city_object_relationship
  parent_city_object_id
  child_city_object_id
  relationship_type
  role (infered from semantic classes if possible, debating if enforcing it (might simplify structure), currently “null”)
  metadata_json
```

---

## Assistant

Yes. I would slightly upgrade your table, but not overcomplicate it.

Recommended final form:

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

### Why this form

#### `relationship_id`

Use an integer primary key rather than a composite key. Later, several assertions of the same relationship may exist from different sources.

#### `graph_name`

This is the most important addition. For phase 1, everything can use:

```text
usap_default
```

Later:

```text
usap_default
citygml_composition
citygml_boundedBy
cityjson_parent_child
manual_review
topology
```

#### `relationship_type`

Keep mandatory. Examples:

```text
contains
partOf
boundedBy
consistsOf
opening
derivedFrom
associatedWith
```

For `usap_default`, use a small controlled vocabulary, but do not globally enforce it at database level yet.

#### `role`

Keep nullable. If null, infer from child semantic class when useful. If set, use it as explicit project/query role.

#### `source_asset_id` and `source_relation_id`

Optional provenance fields.

### Recommended indexes

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

### Closure table change

Old closure:

```text
usap_city_object_closure
  ancestor_city_object_id
  descendant_city_object_id
  depth
```

New graph-aware closure:

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

### SDK changes

Instead of:

```python
link_city_objects(parent, child, relationship_type, role=None)
```

use:

```python
link_city_objects(
    parent_object_id,
    child_object_id,
    relationship_type,
    role=None,
    graph_name="usap_default",
    source_asset_id=None,
    source_relation_id=None,
    metadata=None,
)
```

Instead of:

```python
elements_for_city_object("building_1", include_descendants=True)
```

use:

```python
elements_for_city_object(
    "building_1",
    include_descendants=True,
    graph_name="usap_default",
)
```

### Other tables

`usap_city_object` does not need mandatory change, though an optional `object_status` could be useful:

```text
accepted
draft
inferred
deprecated
conflict
```

`usap_annotation` does not need change.

`usap_annotation_object` does not need change.

Final interpretation:

```text
relationship_type = what kind of edge is this?
role = what role does the child play from the parent's point of view?
```

Examples:

```text
Building -> RoofSurface
relationship_type = boundedBy
role = roof

WallSurface -> Window
relationship_type = opening
role = window

Building -> BuildingPart
relationship_type = consistsOf
role = building_part
```

---

## User

Ok, I accept these chages. prepare a .md file with the final design and a development roadmap.

---

## Assistant

Preparo un file Markdown unico, leggibile come documento di progetto: prima il modello dati finale accettato, poi le query supportate, poi la roadmap di sviluppo per fasi.

Prepared the Markdown file:

```text
/mnt/data/USAP_FINAL_DESIGN_ROADMAP.md
```

It includes the final relationship design we agreed on, the affected table changes, the full table logic, core queries, SDK responsibilities, risks, and a phased roadmap through CityGML/ADE support.

---

## User

save also the whole conversation in a md file

---

## Assistant

This file was generated in response to that request.
