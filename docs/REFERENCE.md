# USAP — Urban Semantic Annotation Package

USAP is a prototype Python package and SQLite/GeoPackage-style data model for storing **semantic annotations over urban 3D assets**.

The current prototype bundles adapters for:

- **LAS/LAZ point clouds** using stable point indices.
- **Mesh files** (`.obj`/`.ply`/`.stl`) using stable face indices.
- **CityGML semantic objects** using imported `gml:id` / object identifiers.

However, any asset with stable integer-indexed elements of one of the four kinds — point, face, vertex, or feature (`feature` is declared but not yet exercised) — can be declared directly via `register_asset` + `register_asset_part`, without an adapter.

USAP is designed for concepts from:

- **CityGML standard concepts** read from the OGC CityGML 3.0 XSDs you supply
  (`load_citygml_schema`), hierarchy included.
- **ADE-like/custom concepts** loaded from an external vocabulary registry.
- **minimal local schemes** (names only, optional parents) for exploratory
  work without an ontology.

USAP does **not** copy geometry into the package. Instead, it stores references to external assets and compact membership blocks that identify which points, faces, or elements are annotated.

The motivation and mental model are in [README.md](../README.md); this file is the reference manual.

---

## Project status

This repository contains a working MVP. The file format, schema, and API may still change, and packages created with this version should be treated as experimental.

The current schema is profile version **0.4.0**. There is no migration path from
either older profile: 0.3.0 renamed the relationship endpoints and made the link
type a foreign key into a new table, and 0.4.0 moved membership and value blocks
under `usap_assessment` and dropped `usap_annotation.label`. `USAPPackage.open`
refuses an older package with an explicit "unsupported profile version" error
rather than misreading it. Rebuild rather than migrate.

What the prototype can do, end to end:

```text
Given:
  a CityGML file (or a minimal vocabulary — INGESTION.md procedure 2),
  the OGC CityGML 3.0 schemas, for the concepts that file uses,
  at least one 3D asset representing (at least one of) the city objects listed in the CityGML file,

USAP can:
  create a package,
  register all assets,
  load accepted concepts from the schemas,
  import CityGML semantic objects and their typed relationships,
  apply JSON annotation batches,
  attach annotations to 3D assets,
  link annotations to city objects,
  query annotations from selected elements,
  validate package integrity.
```

---

## Repository structure

```text
usap_prot/
  pyproject.toml
  README.md            motivation and mental model
  US.md                the user stories this prototype is built against
  docs/                API.md, REFERENCE.md, HANDOFF.md, INGESTION.md,
                       TESTS.md, SCHEMA_WIRING.md, and the design records

  src/usap/             the Python SDK: core, validation, geopackage,
                        domain_vocab, batch, project_builder, synthetic
    adapters/           LAS / mesh / CityGML adapters
    data/schema.sql     the package schema (USAP tables + GeoPackage
                        metadata tables + GIS views)
    data/vocabularies/  example concept registries (ADE prototype, minimal
                        local scheme). No CityGML registry ships: concepts
                        come from the OGC XSDs you supply.
  examples/             runnable CLI scripts: build, validate, smoke test,
                        batch apply, demos
  project_configs/      example_project.json — template project config
  scripts/              synthetic benchmark + profiling
  tests/                pytest suite — every test described in TESTS.md
```

---

## Installation

We suggest creating a virtual environment. Install the package in editable
mode:

```bash
python -m pip install -e .
```

LAS is supported by the base install; LAZ and CRS parsing each need a backend
of their own, so they are optional extras:

```bash
python -m pip install -e ".[laz,crs]"
```

Without an extra, a `.laz` file fails to open and reading a CRS raises a
capability error — neither degrades silently into "this file has no CRS".

Ontologies are **RDF/XML only** and need no extra: they are parsed with the
lxml USAP already depends on. A `.ttl` (or `.n3` / `.nt` / `.trig` /
`.jsonld`) is refused with a "convert to RDF/XML" message — including inside a
configuration folder, where being passed over would show up as "no concepts".

Run the test suite (described in [TESTS.md](TESTS.md)):

```bash
python -m pytest
```

---

## Key concepts

### Package identity

Every package carries a stable identifier in `usap_profile.package_iri`, minted
at creation as a UUID URN (`urn:uuid:...`) and readable with
`pkg.get_package_iri()`. A UUID is globally unique by construction, so this
needs no domain, registry, or namespace — nothing external can make it fail,
and the caller never has to supply one:

```python
with USAPPackage.create("area.usap.gpkg", overwrite=True) as pkg:
    print(pkg.get_package_iri())      # urn:uuid:1f25293f-...

# only to adopt an identity that already exists elsewhere
USAPPackage.create("area.usap.gpkg", package_iri="https://example.org/pkg/area")
```

Identity has to be born with the package: one invented at read time would
differ between two readers of the same file, which is the opposite of what it
is for.

### 3D Asset

An external file registered in USAP, like a LAS/LAZ point cloud or an OBJ/PLY/STL mesh. USAP stores the path, kind, media type, optional content hash, and metadata.

The content hash is stored canonically as `algorithm:digest` — e.g.
`sha256:a48f...`. The algorithm is part of the stored value because
`usap_asset` is unique on `(uri, content_hash)`: a bare digest could not be
told apart from the same file hashed differently, and changing the spelling
later would register one file as two assets. A bare 64-character hex digest is
still *read* as SHA-256, so older records stay comparable; `deep` validation
reports anything that is not a recognizable digest as
`NON_CANONICAL_CONTENT_HASH` (a warning — such a value can never be verified
against the file).

### 3D Asset part

A stable indexable part of an asset. Each part stores its `element_kind` (point or face) and its `element_count` (number of points or faces). Element indices into a part are the coordinate system annotations live in.

A part also records an optional **`indexing_profile`**: which convention
assigned those indices, e.g. `usap:ply-face-record-order-v1`. A content hash
proves the source bytes are unchanged but says nothing about how a reader turns
them into face 0, 1, 2 — two readers of one PLY can disagree on face order and
each be self-consistent, which would repoint every membership without changing
a stored index. The adapters write a token per format; re-registering a part
under a different one raises. What each token means normatively (parsing,
triangulation, duplicate handling) is not yet specified, so the field is
advisory.

### Semantic class / concept

A registered concept accepted by USAP, e.g. `RoofSurface`, `Window`, `EnergyRoof`. Concepts come from a CityGML registry, an ADE/custom registry, or a minimal local scheme (see "Concept registries").

### City object

A semantic object, usually imported from CityGML, e.g. `building_1`, `building_1_roof_1`, `building_1_window_1`.

City objects are wired to each other by **typed, directed** edges in
`usap_city_object_relationship` (`link_city_objects`), grouped into named
graphs (`usap_default` is the default everywhere).

Directed is not hierarchical. `from`/`to` record which way the source asserted
an edge; whether that makes the target a *part* of the source is a property of
the link **type**, held in `usap_relationship_type`:

| Column | |
|---|---|
| `local_name` + `code_space` | the QName the source wrote — `boundary` in `http://www.opengis.net/citygml/3.0`. Together they are the type's identity, so the same name from two namespaces stays two types. |
| `category` | `containment`, `peer`, `generalization`, `grouping`, or NULL. |

"This object **and its parts**" — what `elements_for_city_object` and
`list_city_objects(descendants_of=...)` expand — follows the `containment`
category. A `peer` edge (`adjacentTo`, `predecessor`) relates two objects
without either being part of the other, so it is recorded and simply not
followed. Every query can override:

```python
pkg.list_city_objects(descendants_of="building_1")                       # containment
pkg.list_city_objects(descendants_of="b1",
                      relationship_categories=("containment", "peer"))
pkg.list_city_objects(descendants_of="b1",
                      relationship_types=[("boundary", CITYGML_3_0_CORE_NS)])
pkg.list_city_objects(related_to="b1", direction="both")                 # one hop
```

A type name is resolved within its code space, and a `(name, code_space)` pair
is accepted anywhere a name is — one query routinely spans modules. An
unregistered name **raises**: a typo must not answer "this object has no
parts".

**`category` is the one thing no CityGML artifact states.** Not the XSD, not
the conceptual model's data dictionary, not an OWL rendering of it — all three
carry the properties but nothing marking which are aggregations. So USAP does
not ship it either: it is asserted by whoever builds the package
(`register_relationship_type`, or a project-config `relationship_types`
block). Until something does, an imported edge is stored, queryable by name,
and reported as `UNCLASSIFIED_RELATIONSHIP_TYPE` — never silently treated as
containment.

Containment must be acyclic — a cycle would make an object its own part, and
`validate_report()` flags it as `CITY_OBJECT_GRAPH_CYCLE`. A cycle of peer
edges is legitimate and is not flagged.

An edge may point **outside the package**: `to_external_uri` holds an
`xlink:href` the import could not resolve locally, with `to_city_object_id`
NULL. Such a target has no object row, so `list_city_objects` cannot show it —
`related_city_objects()` returns edges rather than objects and is the only way
to see one.

Descendants are walked from the edges on every query; USAP stores no
precomputed object closure, so an object never has to be "rebuilt into" the
graph and one created with no edges at all still answers for itself.

### Annotation

A semantic claim linked to one concept and optionally to one city object.

The primary city object is stored both as `usap_annotation.primary_city_object_id` and
as a `represents` row in `usap_annotation_object`; the SDK keeps the two in step, and
`validate_report()` reports any disagreement. Other rows in `usap_annotation_object`
carry secondary links (`concerns`, `derivedFrom`, …); city-object queries follow
`represents` links only unless asked otherwise
(`elements_for_city_object(..., link_types=(...))`).

**What belongs in USAP vs the semantic source.** USAP is authoritative for the *claim layer*: which elements, under which concept, with what status, confidence, and provenance. The CityGML/ADE (or other semantic source) is authoritative for the *meaning layer*: which concepts and objects exist, their
properties, and their hierarchy. Accordingly, an annotation's `attributes` must hold **claim-level metadata only** — how/when/by what the claim was produced (`method`, `source`, `assessed_at`, and for value fields `unit`, `validAt`).
Object properties (e.g., roof slope) stay in the semantic source, reachable through the linked city object, so there is exactly one authority for them and nothing to keep synchronized.

Example:

```text
annotation_uid: ann_energy_roof_001
concept: EnergyRoof
primary city object: building_1_roof_1
attributes: claim metadata (method, source)
```

### Assessment

One dated evaluation of an annotation against one 3D asset. The annotation is
the *logical* claim ("this is a RoofSurface, and it is building_1_roof_1"); an
assessment is one concrete evaluation of it — when it was made, which asset it
was made against, and which elements of that asset it covers. Re-surveying the
same roof next year is a second assessment of the same annotation, **not** a
second annotation: that is what keeps the concept and the city-object link from
being duplicated across evaluations and drifting apart.

Every membership and value block belongs to an assessment, so an annotation
that has any geometry has at least one. **You do not have to think about them
until you want two.** A write that names none uses the annotation's single
assessment on that part's asset, creating an undated one if there is none — so
`annotate_elements(...)` and `attach_annotation_elements(...)` behave exactly as
they did before assessments existed. Once a second assessment exists on the same
asset, an unqualified write raises `USAPAmbiguityError` rather than guessing,
because picking the newest would silently rewrite a historical evaluation.

An assessment is bound to an **asset**, not an asset part: a mesh registers one
part per geometry, so an evaluation of "that mesh" routinely spans several
parts. The rule that every block of an assessment lives in a part of *its* asset
is enforced on write and re-checked by `validate_report()`
(`MEMBERSHIP_OUTSIDE_ASSESSMENT_ASSET`).

```text
annotation ann_energy_roof_001            (concept + city object, once)
├── assessment asm_… assessed_at 2026-03-01, asset area_lod2 → faces [100..140]
└── assessment asm_… assessed_at 2027-03-01, asset area_lod2 → faces [120..160]
```

`assessed_at` is free-form text, stored as given: the format is the caller's,
and refusing an unfamiliar spelling would be worse than storing it. At most one
*undated* assessment may exist per (annotation, asset) — enforced by a partial
unique index, because SQLite treats NULLs as distinct and a plain `UNIQUE` would
not have constrained them.

| Call | Does |
|---|---|
| `pkg.create_assessment(annotation_id, asset, assessed_at=…)` | create or reuse one evaluation (idempotent on annotation + asset + date) |
| `pkg.list_assessments(annotation_id=…)` | every evaluation, undated first then by date |
| `pkg.get_assessment` / `update_assessment` / `delete_assessment` | read, edit metadata, remove one evaluation and its blocks |
| `pkg.elements_for_annotation(id, assessment=…)` | the elements of one evaluation |
| `pkg.annotations_for_elements(..., assessment=…)` | restrict a reverse lookup to one evaluation |

`annotations_for_elements` returns **one entry per (annotation, assessment)**,
each tagged with `assessment_id` and `assessed_at`. With one assessment per
annotation that is one entry per annotation, as before; two entries appear only
once the same asset really has been evaluated twice, which is exactly when they
must be distinguishable. The two extents are never merged — a union would report
a coverage no single evaluation ever claimed.

Each entry carries both `primary_city_object_uid` and
`primary_city_object_gml_id`, the same pair `get_annotation` and
`list_annotations` return, so a selection list and a detail panel name the same
object identically. (Added in 0.4.1; before that the reverse query returned the
uid alone.) Both are `None` for an annotation with no city object — the normal
case for a free lasso claim, and for every value field.

The asset of an assessment cannot be changed after the fact: its membership is
indexed against that asset's parts, so re-pointing it would silently make every
stored index mean different geometry. Record a new assessment instead.

### Membership block

A compressed set of selected element indices for one **assessment**, one asset part, and one element kind (the owning annotation is carried alongside, so forward queries stay one indexed lookup). Stored as a **roaring bitmap** in CRoaring's portable serialization (`encoding = 'roaring'`), so the payload is readable by any roaring implementation, not only this SDK. Indices are partitioned into blocks of `usap_profile.default_block_size` (16384) and held as within-block offsets; `block_start` is what the reverse element query prunes on.

Example:

```text
annotation ann_energy_roof_001
assessment assessed_at 2026-03-01
asset part area.las points/all
selected point indices [100, 101, 102]
```

### Value block (annotation on a whole 3D asset)

A compressed dense array of per-element scalar values for one assessment and one asset part: element *i*'s value is `decoded[i - block_start]`. Membership stores *which* elements are a concept; value blocks store the *value* of a property at each element (e.g. shadow fraction per face). Rule of thumb: booleans and categories are **sets** (native membership, like "shadowed at 14:00", is just a concept plus the shadowed faces); reach for a value field only for genuinely **continuous** values. Value fields are bound to the geometry asset only, never to a city object, and must cover every element of the part (v1; NaN = "no value" in float fields). Stored little-endian, dtype per block (`f4` default; see `VALUE_DTYPES`), with per-block min/max for decode-free stats and query pruning.

Example:

```text
annotation ann_shadow_1400 (concept ShadowFraction, no city object)
asset part area_lod2.obj geometry/0
values float32 [0.0, 0.73, 0.5, ...]   one per face
```

---

## Integrating USAP into an application

USAP is a Python library, not a service or a viewer. An application drives the
`USAPPackage` object for interactive edits, or `build_project_package_from_file`
for bulk import. What follows is the set of things that are cheap to get right at
the start and expensive to discover later.

**USAP stores no source geometry.** A viewer or processing pipeline stays
responsible for meshes and point clouds. USAP identifies annotated elements by
their stable integer index within an asset part, so the application maps a
viewer pick to `(asset_part_id, element_index)` and keeps the registered source
version immutable.

**Register assets from the code that loads them for display.** This is the one
integration mistake nothing can detect afterwards. The application owns the
loader, so it produces the indices USAP stores; if that loader triangulates
quads, dedupes vertices, or orders faces differently from whatever counted the
elements at registration, every membership silently points at the wrong geometry
and no validation level will notice. Registering through the generic
`register_asset` + `register_asset_part` path *from the same code that builds the
render buffers* makes count and order agree by construction — and sidesteps the
mesh adapter's `.glb`/`.gltf` refusal, since the adapter is then not involved.
Record the convention in `indexing_profile`; `validate_report()` warns
`ASSET_PART_NO_INDEXING_PROFILE` when an annotated part declares none.

**City objects need not come from CityGML.** When the semantic source belongs to
another system, create carriers on demand —
`create_city_object(object_uid=gml_id, object_status="temporary")` — and skip
`import_citygml_semantics` entirely. Consequence: with no imported link graph
there is nothing for `elements_for_city_object(include_descendants=True)` to
walk, so an application that wants "this Building and all its surfaces" walks its
own hierarchy and passes the set to `elements_for_city_objects([...])`.

**Create carriers classless.** A carrier is an identity anchor; its class lives
in the semantic source, by the same division of authority that keeps attributes
there. This matters because an annotation's concept (`EnergyRoof`) is usually
*not* the object's class (`RoofSurface`), so passing the former here is an easy
mistake — one that used to be swallowed, and since 0.4.1 raises. Idempotency on
`object_uid` compares only the fields a call actually supplies, so the bare
`create_city_object(uid)` lookup keeps working against a populated row.

**Register the semantic source with `compute_hash=False`, or not at all.** If
another system edits the CityGML, a hash recorded here reports
`ASSET_FILE_CHANGED` at the `external` validation level for a file USAP does not
own.

**The vocabulary is seeded into the package, not read at annotation time.**
`load_vocabulary_folder` (or the individual loaders) copies concepts into
`usap_semantic_class`; CRUD then validates against that copy, which is why a
package is portable and why a concept cannot vanish from under existing
annotations when a config file is edited. Seed on create and **re-seed on open**:
seeding is idempotent and enriching, filling in what is missing and raising only
on a genuine contradiction — which the application should surface rather than
swallow. Note the asymmetry: concepts are gated (an unregistered one raises),
link types are not (an unregistered name auto-registers).

**One thread per connection.** `sqlite3` connections are thread-bound; using a
`USAPPackage` from a worker thread raises `ProgrammingError` at runtime, not at
review. Confine USAP to one thread, or open a connection per thread.

**A city-object link and an asset membership are different things.** The
city-object link identifies the authoritative semantic instance a claim
represents or concerns; membership blocks identify selected points or faces in
operational assets. `attach_annotation_elements` adds another geometric
membership to the same claim — it does not create another city object.

**Large source files stay outside the edit path.** After registration, changing
an annotation updates the USAP package rather than rewriting the mesh, point
cloud, or semantic authority. Query cost is governed by the stored blocks and the
result size, not by rereading the source asset.

**Errors.** Catch `usap.USAPError` (bad references, constraint violations,
out-of-range indices, unsupported dtypes) and its subclass
`usap.USAPAmbiguityError` (a reference matching more than one record).

---

## Concept registries

USAP ships **no** built-in taxonomy and does not enforce one. A new package starts with **zero** concepts; it holds only whatever vocabulary you seed into it.

**Register a concept before you annotate with it.** Every annotation references exactly one concept so the concept must already exist in the package, carrying at least its minimal identity:

- `scheme` — the vocabulary/namespace it belongs to
- `local_name` — its label
- `class_uri` — its globally-unique identifier (optional: derived as
  `scheme:local_name` when omitted)
- *(optional)* `parent_uri` (parent concept, for hierarchy), `scheme_version`, `is_ade`
- *(optional, provenance)* `source_namespace`, `concept_iri` — see below

Annotations then **reference** the registered concept; they do not re-describe it.

**The vocabulary format is the contract.** Concepts are loaded with `seed_vocabulary_file()`, which expects a JSON registry of this minimal shape (one file per scheme; list parents before children):

```json
{
  "scheme": "citygml",
  "scheme_version": "3.0",
  "is_ade": false,
  "concepts": [
    { "local_name": "AbstractThematicSurface",
      "class_uri": "http://www.opengis.net/citygml/3.0#AbstractThematicSurface" },
    { "local_name": "RoofSurface",
      "class_uri": "http://www.opengis.net/citygml/construction/3.0#RoofSurface",
      "parent_uri": "http://www.opengis.net/citygml/3.0#AbstractThematicSurface" }
  ]
}
```

Required: top-level `scheme`; `concepts[]` each with `local_name`. Optional: `class_uri` (derived as `scheme:local_name` when omitted — recommended explicit for ontology-backed schemes), `parent_uri` (accepts a `class_uri` **or** the local name of an already-registered concept, resolved within the same scheme first), `scheme_version`, `is_ade`. **Any other keys are ignored.** This minimal shape is intentionally a thin JSON form of a **SKOS** concept scheme (`class_uri` → concept IRI, `local_name` →
`skos:prefLabel`, `parent_uri` → `skos:broader`, `scheme` → `skos:ConceptScheme`). Richer per-concept metadata (definitions, units, properties) is deliberately out of scope — that is the *application schema* (e.g. a CityGML ADE XSD / SHACL), not the concept scheme.

**CityGML concepts do not use this format.** They are read directly from the
OGC XSDs with `load_citygml_schema(pkg, path)`, which takes identity from each
element's `targetNamespace` and the hierarchy from its `substitutionGroup`.
That is the normative source, and it is the only one that carries the class
hierarchy at all — a CityGML OWL rendering names every class but asserts
almost no `rdfs:subClassOf`.

The JSON format above remains the way to register an **ADE or a local scheme**
that has no schema to read, and a taxonomy in some other format can still be
converted into it.

#### Initialising on an ontology

`load_ontology(pkg, path)` reads an **RDF/XML** ontology and registers what it
declares:

| Read | Becomes |
|---|---|
| `owl:ObjectProperty` | a link type, identified by the IRI's namespace + local name |
| `usap:category` | that type's category — the one fact no CityGML artifact carries |
| `owl:Class` (+ `rdfs:subClassOf`) | a concept and its parent, for ADE classes |

```xml
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xmlns:usap="urn:usap:">
  <owl:ObjectProperty rdf:about="http://www.opengis.net/citygml/3.0#boundary">
    <usap:category>containment</usap:category>
  </owl:ObjectProperty>
</rdf:RDF>
```

`urn:usap:category` is a URN because the predicate has to be globally unique
without USAP owning a domain — the same reasoning as `package_iri`.

This is what makes a package *depend* on its ontology: supply a different one
and the link vocabulary changes with it, over the very same document. Loading
is idempotent and order-independent — classify before or after the import, the
result is the same, and a category that contradicts one already recorded raises
rather than overwriting it.

**RDF/XML is the only syntax read**, picked by suffix: `.owl` / `.rdf` /
`.rdfs` / `.xml` go through a narrow built-in reader that uses the `lxml` USAP
already depends on, so ontology loading adds no install and the package carries
no second RDF stack.

`.ttl` / `.n3` / `.nt` / `.trig` / `.jsonld` **raise**, with the remedy in the
message (convert to RDF/XML — Protégé's "Save as" does it). They raise inside
`load_vocabulary_folder` too, which is the point: a folder walk that quietly
ignored them would seed fewer concepts than the configuration names, and
nothing downstream could tell that apart from a folder that held less.

One deliberate limit remains: **`owl:imports` is not followed** — the imported
IRIs are returned so you can load them yourself, but fetching over the network
during a package build is not something the SDK does for you. To seed a whole
configuration directory in one call — schemas, ontologies, and JSON
vocabularies — use `load_vocabulary_folder(pkg, path)`.

A category only bites if the property's IRI namespace matches the namespace the
source document writes; that pair *is* the type's identity. Mismatch it and the
category lands on a type nothing uses, while the one the import registered
stays unclassified — which `validate_report()` reports.

#### Concept provenance

Two further optional fields record where a concept came from in its authority:

- **`source_namespace`** — the authority's namespace. For a CityGML-derived
  concept this is the XML namespace URI, which together with `local_name` is
  the QName the `.gml` actually uses, and so the only exact join key back to
  the source. May be given once at the top level as the default for the whole
  file and overridden per concept (a CityGML registry spans several module
  namespaces).
- **`concept_iri`** — the authority's own IRI for the concept, when it
  publishes one.

```json
{
  "scheme": "citygml",
  "scheme_version": "3.0",
  "source_namespace": "http://www.opengis.net/citygml/construction/3.0",
  "concepts": [
    { "local_name": "RoofSurface",
      "class_uri": "http://www.opengis.net/citygml/construction/3.0#RoofSurface" }
  ]
}
```

Both are nullable and neither is populated by the shipped registries yet.
`class_uri` remains the *internal* key — unique, and what seeding is idempotent
on; these two are the *external* facts. Recording them is what keeps a stable
identifier **derivable** later without re-deciding anything, which is why the
format carries them before any decision about IRIs has been made (see
[FW_FEATURE_W3C_WEB_ANNOTATION_PROFILE.md](../FW_FEATURE_W3C_WEB_ANNOTATION_PROFILE.md)).

**Re-seeding is enriching, not just additive.** A field still `NULL` on an
existing concept is filled in, so provenance added to a registry later reaches
packages that already exist by re-seeding rather than rebuilding. A field that
already holds a *different* value raises instead of being silently rewritten —
seeding must never change what a package already asserts. Concept identity
(`scheme`, `class_uri`, `local_name`) is never backfilled: a change there is a
different concept, not an enrichment. A parent arriving late is propagated into
the class closure, so subclass queries see the new edge.

### Example registries

The files under `src/usap/data/vocabularies/` ship with the package but are
**examples only**, not a built-in taxonomy — a new package starts with zero
concepts and seeds only what you ask for:

```text
src/usap/data/vocabularies/usap_ade_prototype.json
src/usap/data/vocabularies/local_minimal_example.json
```

The ADE one is reachable in code as `DEFAULT_ADE_VOCABULARY_PATH`
(`usap.domain_vocab`), so config files need not name it by path.

**No CityGML registry ships.** There used to be a hand-written
`citygml_3_0_mvp.json`, and every error it accumulated came from being
hand-written: thematic surfaces filed under `building` instead of
`construction`, `Window` parented to `AbstractFillingSurface` rather than
`AbstractFillingElement`, a `WaterClosureSurface` that exists only in CityGML
2.0, and twenty concepts short-circuited past the space/space-boundary layer.
Deriving them from the schema removes the whole class of error, and means USAP
asserts nothing of its own about what CityGML contains.

The ADE registry is an ADE-ready custom registry for project-specific concepts such as:

```text
EnergyBuilding
EnergyFacade
EnergyRoof
PermeabilityExternalSurface
PermeabilityUrbanZone
AcousticBuilding
AcousticUrbanArea
VisualBuilding
VisualFacade
```

To consult the list of accepted concepts in a `.usap.gpkg` (without `--db`
the script builds a throwaway demo package instead):

```bash
python examples/list_concepts_demo.py --db my_area.usap.gpkg
python examples/list_concepts_demo.py --db my_area.usap.gpkg --search Roof
python examples/list_concepts_demo.py --db my_area.usap.gpkg --used
```

In Python:

```python
from usap import USAPPackage, load_citygml_schema, seed_default_ade_vocabulary

with USAPPackage.create("concepts.usap.gpkg", overwrite=True) as pkg:
    load_citygml_schema(pkg, "citygml-3.0-schemas/")   # the OGC XSDs
    seed_default_ade_vocabulary(pkg)

    for concept in pkg.list_accepted_concepts(search="Roof"):
        print(concept["local_name"], concept["class_uri"], concept["in_use"])
```

### Minimal vocabulary without an ontology

When no CityGML registry or ontology is provided, a vocabulary file only needs a `scheme` and concept `local_name`s — `class_uri` is derived as `scheme:local_name` when omitted, and `parent_uri` accepts either a `class_uri` or the local name of an already-registered concept (same-scheme resolution first). Declare just the names you need under a temporary scheme and annotate right away:

```json
{
  "scheme": "local",
  "concepts": [
    { "local_name": "TempRoof" },
    { "local_name": "TempChimney", "parent_uri": "TempRoof" }
  ]
}
```

See [`src/usap/data/vocabularies/local_minimal_example.json`](../src/usap/data/vocabularies/local_minimal_example.json).
Re-loading an updated copy is additive, idempotent, and enriching — new concepts are added and fields still `NULL` on existing ones are filled in; changing an existing concept's parent (or any other field that already has a value) raises. Concepts declared this way stay identifiable by their scheme (`list_accepted_concepts(scheme="local")`), so annotations made this way can later be aligned to a full ontology-based package (the latter is WIP).

---

## Build a real project package

> The three supported ingestion/editing procedures (CityGML init,
> minimal-vocabulary init, editing) are documented end to end in
> [INGESTION.md](INGESTION.md). This section describes the config keys.

One example config is provided —
[`project_configs/example_project.json`](../project_configs/example_project.json),
a generic template: edit its `../data/area.*` paths to point at your own data
files (paths are resolved relative to the config file; the data files
themselves are not committed to git). Abridged here to one of its three
meshes:

```json
{
  "db_path": "../outputs/example_project.usap.gpkg",
  "manifest_path": "../outputs/example_project_manifest.json",

  "citygml_schema": "../citygml-3.0-schemas",

  "relationship_types": [
    { "local_name": "boundary",
      "code_space": "http://www.opengis.net/citygml/3.0",
      "category": "containment" },
    { "local_name": "fillingSurface",
      "code_space": "http://www.opengis.net/citygml/construction/3.0",
      "category": "containment" }
  ],

  "citygml": {
    "path": "../data/area.gml",
    "compute_hash": true
  },

  "las": [
    {
      "path": "../data/area.las",
      "part_path": "points/all",
      "compute_hash": true
    }
  ],

  "meshes": [
    {
      "path": "../data/area_lod2.obj",
      "uri": "area_lod2",
      "representation_name": "buildings_lod2",
      "representation_kind": "building_mesh",
      "lod": "LoD2",
      "compute_hash": true
    }
  ],

  "annotation_batches": [
    "../examples/batches/example_annotation_batch.json"
  ]
}
```

Key notes:

- `"annotation_batches"` — batch files applied right after the assets are
  registered, so one build call ingests the annotations too.
- an asset's optional `"uri"` (a stable logical name, `area_lod2` above) lets
  batch memberships reference parts as `"asset_uri": "<that name>"` instead of
  the numeric `asset_part_id` from the manifest.
- `"srs_id": 25833` (+ optional `"srs_wkt"`) — declares the package CRS for the
  GIS extent layer (one CRS per package). Without it, an EPSG code found in the
  LAS files' CRS WKT is promoted automatically when they all agree; otherwise
  the layer stays in the undefined SRS (−1), which is honest for
  local-coordinate meshes.
- `"schema_path"` / `"vocabularies"` are optional; both default to the files
  shipped inside the package.
- `"validation_level"` — the level the build validates at before committing
  (`deep` by default; see Validation).
- the whole build is **one transaction**: on failure a fresh build leaves no
  package behind at all, and an `update=True` run leaves the previous package
  exactly as it was.

Run:

```bash
python examples/build_project_package.py project_configs/example_project.json
```

Expected outputs:

```text
outputs/example_project.usap.gpkg
outputs/example_project_manifest.json
```

The manifest lists each part's `asset_part_id` (usable in batches as an
alternative to `asset_uri`).

To apply a config to an **existing** package — add assets, apply editing
batches — pass `--update` (Python: `build_project_package_from_file(path,
update=True)`). Registration is idempotent, and in update mode batches run
with `replace_existing=True`.

To see which concepts a built package accepts — optionally filtered — point
the concept lister at it (each line shows local name, CityGML vs ADE/custom,
`used:N`/`unused`, scheme, and full `class_uri`):

```bash
python examples/list_concepts_demo.py --db outputs/my_area.usap.gpkg
python examples/list_concepts_demo.py --db outputs/my_area.usap.gpkg --search Roof
python examples/list_concepts_demo.py --db outputs/my_area.usap.gpkg --used
```

---

## Opening a package in QGIS

A `.usap.gpkg` is a valid GeoPackage. Adding it to QGIS (or listing it with `ogrinfo`) shows four read-only layers:

```text
usap_annotations_view    attributes   annotations with concept, city object,
                                      status, and element counts
usap_concepts_view       attributes   accepted concept registry + usage
usap_city_objects_view   attributes   city objects (carriers show
                                      object_status = 'temporary')
usap_asset_extents       features     one derived 2D bounding box per
                                      registered asset
```

Notes:

- The extent boxes are **derived summaries** (the union of each asset's part
  bounds, captured at registration) — never actual geometry. They are written
  automatically, kept by `ON DELETE CASCADE`, and checked by `validate_report()`
  against the part bounds.
- The layer CRS defaults to the undefined SRS (−1) until declared — see the
  `"srs_id"` config key above or `set_package_srs(pkg.conn, epsg)` from Python
  (safe before or after registration; existing boxes are re-encoded).
- Declaring a CRS **requires its definition**: pass `"srs_wkt"` alongside
  `"srs_id"`, or install `usap[crs]` so the WKT can be looked up from the EPSG
  code. GeoPackage reserves "undefined" definitions for the built-in SRS ids
  −1 and 0 and requires a record defining every SRS a package actually uses.
- `set_package_srs` re-stamps the CRS id on the stored extent boxes but does
  **not** transform coordinates: USAP assumes one CRS per package. Assets
  registered in different CRSs are reported as a `MIXED_ASSET_CRS` warning
  rather than silently misplaced.
- Each view exposes its key as `OGC_FID`, the alias GDAL recognises as a
  view's feature id (a column merely called `fid` is carried as an ordinary
  attribute, so features got GDAL's own row numbers instead of USAP ids), and
  aggregate columns are `CAST` to INTEGER so they are not inferred as strings.
- Fine-grained content — element memberships, value fields — is not exposed as
  layers; use the Python API for those.
- USAP registers itself in `gpkg_extensions` as an Extended GeoPackage. The
  `definition` column currently holds a **placeholder** URI
  (`https://usap.invalid/...`) until the extension document has a public home
  — the file is readable everywhere, but the registration is not yet a
  resolvable reference.

---

## Run an end-to-end smoke test

After building a project package:

```bash
python examples/smoke_test_project_package.py \
  outputs/example_project.usap.gpkg \
  outputs/example_project_manifest.json \
  --batch-out outputs/smoke_batch.json
```

This creates one annotation, attaches it to:

```text
one CityGML city object
one LAS point asset part
one mesh face asset part
```

and then queries it back from both a selected LAS point and a selected mesh face.

Useful flags:

- `--replace-existing` — rerun and replace the same smoke annotation.
- `--city-object-uid YOUR_GML_ID` — use a specific CityGML object.
- `--mesh-representation-name buildings_lod2` — use a specific mesh
  representation.

---

## Reproduce the benchmark

The synthetic benchmark measures build and query performance on a generated
city — the evidence behind the project's performance claim (no external data
needed; run from the repo root):

```bash
python scripts/benchmark_phase1.py --buildings 1000 --repeat 5 --md bench.md
```

It generates a synthetic city (`create_synthetic_package` / `SyntheticConfig`,
also runnable standalone via `examples/build_synthetic.py`), times the build
and the core queries, validates the package, and writes the report to the
`--md`/`--json` paths. `--schema` defaults to the packaged schema; sizes are
tunable with `--buildings`, `--roof-faces`, `--wall-faces`, `--ground-faces`.

---

## Batch annotation format

Batch annotations (the "linking JSON" of [INGESTION.md](INGESTION.md)) are
JSON files. Several fields are derivable, so the minimal entry is just
*object + elements* (procedure 1) or *object + concept + elements*
(procedure 2):

- `concept` — optional when the linked city object already has a class
  (inherited from it); required otherwise, and when creating carriers.
- `annotation_uid` — optional when a city object is linked; derived as
  `ann_{object_uid}_{concept_local_name}` (stable across re-runs, so
  re-applying with `--replace-existing` edits in place). **The derivation is
  scoped to one object/concept pair**, which is what makes a re-run idempotent
  rather than duplicating — so two assertions that differ only in *when* or
  *how* they were made (the same concept on the same object at two timesteps,
  or from two methods) collide on it. Give those an explicit `annotation_uid`.
  Value-field annotations link no city object, so they already require one.
- `element_kind` — optional; defaults to the asset part's stored kind.
- parts are referenced by `asset_part_id` (int) **or** `asset_uri`
  (+ `part_path` when the asset has several parts) — exactly one of the two.
- top-level `"create_missing_city_objects": true` (minimal-vocabulary
  procedure only) lets unknown `city_object_uid`s create carrier city
  objects: classed by the entry's concept, `object_status='temporary'`
  (the marker for later CityGML alignment), nothing else. Without the flag,
  unknown names fail loudly.

Full-form example:

```json
{
  "annotations": [
    {
      "annotation_uid": "ann_energy_roof_001",
      "concept": "EnergyRoof",
      "city_object_uid": "building_1_roof_1",
      "status": "draft",
      "assessed_at": "2026-06-30T14:00:00Z",
      "confidence": 0.8,
      "attributes": {
        "domain": "energy_emissions",
        "method": "roof_detector_v2",
        "source": "survey_2026_06"
      },
      "memberships": [
        {
          "asset_part_id": 2,
          "element_kind": "point",
          "element_indices": [100, 101, 102]
        },
        {
          "asset_uri": "area_lod2",
          "element_indices": [40, 41, 42]
        }
      ]
    }
  ]
}
```

Apply a batch:

```bash
python examples/apply_annotation_batch.py \
  outputs/example_project.usap.gpkg \
  path/to/batch.json
```

Replace an existing annotation with the same `annotation_uid`:

```bash
python examples/apply_annotation_batch.py \
  outputs/example_project.usap.gpkg \
  path/to/batch.json \
  --replace-existing
```

An annotation may carry `"value_fields"` instead of (or alongside) `"memberships"` —
at least one of the two is required. Values are listed inline, one per element of the
asset part, with JSON `null` meaning "no value" (stored as NaN; float dtypes only):

```json
{
  "annotations": [
    {
      "annotation_uid": "ann_shadow_1400",
      "concept": "ShadowFraction",
      "attributes": { "validAt": "2026-06-21T14:00:00Z", "unit": "fraction" },
      "value_fields": [
        {
          "asset_part_id": 3,
          "element_kind": "face",
          "values": [0.0, 0.73, null, 0.5],
          "value_dtype": "f4"
        }
      ]
    }
  ]
}
```

The batch importer validates:

```text
concept is registered (or inheritable from the linked object's class)
city object exists (unless create_missing_city_objects is set)
asset part exists and the asset_uri reference is unambiguous
element kind matches asset part (when given; defaulted otherwise)
element indices are in range
value fields cover the whole asset part; dtype is supported
annotation UID is not duplicated unless replacement is requested
```

---

## Python API examples

### Create a package and load concepts

```python
from usap import USAPPackage, load_citygml_schema, seed_default_ade_vocabulary

with USAPPackage.create("demo.usap.gpkg", overwrite=True) as pkg:
    # Concepts and their hierarchy, read from the OGC CityGML 3.0 XSDs.
    # USAP ships none of its own; download them from
    # schemas.opengis.net/citygml/citygml-3_0_0.zip.
    load_citygml_schema(pkg, "citygml-3.0-schemas/")

    seed_default_ade_vocabulary(pkg)

    # Which link types mean "part of". No CityGML artifact states this, so it
    # is asserted here (or by an ontology). Without it every edge is still
    # recorded and queryable by name, but nothing is reported as a part.
    pkg.register_relationship_type(
        "boundary",
        code_space="http://www.opengis.net/citygml/3.0",
        category="containment",
    )
```

(`schema_path` defaults to the schema shipped inside the package, so this works
from a plain wheel install; pass it explicitly only to use a modified schema.)

### Register LAS and mesh assets

```python
from usap import register_las_asset, register_mesh_asset

las = register_las_asset(pkg, "data/area.las")

mesh = register_mesh_asset(
    pkg,
    "data/area_lod2.obj",
    representation_name="buildings_lod2",
    representation_kind="building_mesh",
    lod="LoD2",
)
```

### Import CityGML semantics

```python
from usap import import_citygml_semantics

result = import_citygml_semantics(pkg, "data/area.gml")

print(result.object_count)
print(result.relationship_count)
```

### Annotate elements with a CityGML concept

```python
from usap import ELEMENT_KIND_FACE

annotation = pkg.annotate_elements(
    concept="RoofSurface",
    annotation_uid="ann_roof_faces_001",
    asset_part_id=mesh.primary_asset_part_id,
    element_kind=ELEMENT_KIND_FACE,
    element_indices=[10, 11, 12],
    status="accepted",
)
```

### Annotate elements with an ADE/custom concept

```python
from usap import ELEMENT_KIND_POINT

annotation = pkg.annotate_elements(
    concept="EnergyRoof",
    annotation_uid="ann_energy_roof_points_001",
    city_object_uid="building_1_roof_1",
    asset_part_id=las.asset_part_id,
    element_kind=ELEMENT_KIND_POINT,
    element_indices=[100, 101, 102],
    attributes={
        "domain": "energy_emissions",
        "method": "manual_selection",
        "assessed_at": "2026-06-30T14:00:00Z",
    },
)
```

### Attach a second representation to the same annotation

```python
annotation = pkg.attach_annotation_elements(
    annotation_id=annotation["annotation_id"],
    asset_part_id=mesh.primary_asset_part_id,
    element_kind="face",
    element_indices=[40, 41, 42],
)
```

### Query annotations from selected elements

```python
matches = pkg.annotations_for_elements(
    asset_part_id=las.asset_part_id,
    element_kind="point",
    selected_indices=[101],
)

for match in matches:
    print(match["annotation_uid"], match["semantic_class"])
```

### Read, list, update, and delete annotations

```python
annotation = pkg.get_annotation(annotation_uid="ann_energy_roof_points_001")

items = pkg.list_annotations(
    semantic_class_local_name="EnergyRoof",
    status="draft",
)

updated = pkg.update_annotation(
    annotation["annotation_id"],
    status="accepted",
    confidence=0.9,
)

# Moving an annotation to another city object rewrites its 'represents' link
# in the same transaction, so it stops answering queries for the old object.
pkg.update_annotation(
    annotation["annotation_id"],
    primary_city_object_id=pkg.resolve_city_object("building_1_roof_2"),
)

pkg.delete_annotation(annotation["annotation_id"])
```

### Per-element value fields

```python
import numpy as np

# one value per face of the asset part; NaN = "no value"
shadow = np.clip(np.random.default_rng(0).normal(0.4, 0.2, mesh_face_count), 0, 1)

ann = pkg.annotate_value_field(
    concept="ShadowFraction",          # any registered concept: CityGML, ADE, or local
    asset_part_id=mesh_part_id,
    element_kind="face",
    values=shadow,                     # dtype: explicit > whitelisted ndarray dtype > 'f4'
    attributes={"validAt": "2026-06-21T14:00:00Z", "unit": "fraction"},
)
annotation_id = ann["annotation_id"]   # primary_city_object_id is always NULL

values = pkg.values_for_annotation(annotation_id)          # full dense array back
faces = pkg.elements_where(annotation_id, (">", 0.5))      # sorted face-index set
dim = pkg.elements_where(annotation_id, lambda v: (v > 0.2) & (v < 0.8))
stats = pkg.value_field_stats(annotation_id)               # min/max/count, no decode

# editing is whole-field rewrite (the exception path)
pkg.replace_value_field(annotation_id, mesh_part_id, "face", corrected_values)
```

---

## Element kinds

The user-facing API accepts readable element-kind names — `point`/`points`,
`face`/`faces`, `triangle`/`triangles` — and normalizes them to USAP
constants internally, so `element_kind="point"` and
`element_kind=ELEMENT_KIND_POINT` are equivalent.

---

## CityGML support

The CityGML importer is semantic-only: identities and relationships, never
geometry.

It imports:

```text
gml:id / object identity
semantic class, matched on the exact QName the document writes
typed relationships, in every serialization CityGML allows
source provenance
```

It does not import:

```text
full CityGML geometry
full schema validation
complete ADE XML interpretation
```

**Concepts are a precondition, not an output.** The import classifies elements
against concepts already registered, and raises if none are — it will not
invent a vocabulary, which is how a CityGML 2.0 `Building` used to end up filed
under a 3.0 class URI. Load `load_citygml_schema()` (or an ADE registry) first.

### Relationships: nesting is a serialization, not the relationship

The relationship is the **named property element**; whether its target sits
inside that element or behind an `xlink:href` only says where the target is
*defined*. Most CityGML properties accept either form — a surface shared
between two features *must* be written by reference — and six accept nothing
else (`generalizesTo`, `relatedTo`, `predecessor`, `successor`, `groupMember`,
`parent`).

The importer therefore runs two passes: one creating every city object and
indexing it by element and `gml:id`, then one resolving each relationship
property against that index. Three shapes are handled behind one resolver:

| Shape | |
|---|---|
| inline | the target is written inside the property element |
| xlink | the property carries `xlink:href`; the target is elsewhere in the document, or outside it |
| objectified | the property holds a `CityObjectRelation` or a `Role`, which carries the qualifier and then points at the target |

For an objectified `CityObjectRelation` the stored link type is the
`relationType` **code value** with its `codeSpace` — `adjacentTo`, not the
generic `relatedTo` carrier — so an open relation stays queryable in SQL. A
`Role` supplies the edge's `role`.

An `xlink:href` that does not resolve in this document is kept as an edge with
`to_external_uri` set, warned about at import, and reported by
`validate_report()` as `UNRESOLVED_RELATIONSHIP_TARGET`. A reference that is
not a city-object link at all — an appearance href, say — is skipped and
listed in `result.skipped_references` rather than minting a bogus edge.

The import writes **one graph** (`usap_default` unless `graph_name` says
otherwise). It used to write every edge twice, mirroring into `usap_default`,
which made half the relationship table a duplicate.

**What counts as CityGML.** Elements become city objects only when their
namespace is a CityGML one (`*opengis.net/citygml*`, any module, versions
1.0/2.0/3.0) — an element merely *named* `Building` in some other vocabulary
is skipped, and a document declaring no CityGML namespace at all is refused
rather than imported as zero objects. Likewise, only a real `gml:id`
(`*opengis.net/gml*`) is adopted as object identity; an `id` attribute from
another namespace is ignored and a generated uid is used instead.

**Version provenance.** The detected CityGML version is recorded in the asset
metadata (`citygml_version_hint`), and both concepts and link types carry the
namespace the document actually used — so a 2.0 `Building` and a 3.0 one are
different rows, and neither is silently filed under the other. This used to be
a known limitation: concepts came from a shipped 3.0 registry regardless of
what the file said.

---

## Mesh support

The mesh adapter reads **`.obj`, `.ply` and `.stl`** and is not limited to
LoD1/LoD2: building meshes, city triangulations, TIN terrain, photogrammetry
or simulation meshes all register the same way. Two conditions:

- **Face indices must remain stable for the registered file version.** If a
  mesh is remeshed, simplified, reordered, or re-exported in a way that
  changes face order, register it as a new asset.
- **Vertices must already be in their final coordinates.** The adapter reads
  geometries, not scenes.

`.glb`/`.gltf` are therefore **refused** rather than partially supported: a
glTF scene positions its geometries with node transforms and may instance one
geometry at several nodes, so reading the geometries alone yields bounds in
the wrong place and one asset part where the scene has several instances —
wrong data rather than missing data. Export with transforms baked in, or wait
for scene-graph support (see the future-work notes).

### Large meshes

Registration is the **only** step that opens a mesh file. Everything after it
— annotating, editing, querying — works from the element count stored on the
asset part, so a 10 GB mesh is no more expensive to annotate than a 10 MB one.

Registration itself needs just two facts (face count, bounding box), and
loading a whole mesh to get them does not survive a large file. Files over
**256 MB** are therefore read in a streaming pass instead; `stream=True` /
`stream=False` overrides the threshold:

```python
register_mesh_asset(pkg, "city.ply", representation_name="city")               # auto
register_mesh_asset(pkg, "city.ply", representation_name="city", stream=True)  # forced
```

Measured on a 174 MB binary PLY (6 M faces), both paths recording identical
face counts and bounds:

| | time | peak RSS |
|---|---:|---:|
| `stream=False` (trimesh load) | 2.40 s | 758 MB |
| `stream=True` | 0.68 s | 121 MB |

Streaming readers exist for **`.ply`** (ASCII and binary) and **`.obj`** only,
and treat one file as one part. Two cases raise instead of guessing:

- an OBJ carrying several `o`/`g` groups — a normal load would split it into
  one part per group, and annotations bind to part names, so the same file
  must not register differently depending on its size;
- any other format (`.stl`) — no streaming reader, so load it in full.

`compute_hash=True` (the default) re-reads the whole file to SHA-256 it, which
is minutes on a 10 GB asset. It is what makes a later change to that file
detectable (`validate_report(level="external")`), so skip it deliberately, not
by accident.

---

## Validation

Validate a package in Python:

```python
report = pkg.validate_report()          # level="deep" by default
report.print()
```

Or run an example validator:

```bash
python examples/validate_package.py outputs/example_project.usap.gpkg
python examples/validate_package.py outputs/example_project.usap.gpkg --level external
```

### Levels

Validation costs scale with what it is allowed to read, so it comes in three
levels. `report.is_ok` always means "no errors **at the level you asked
for**" — a clean `basic` report is not a claim about payloads, and a clean
`deep` report is not a claim about the files on disk.

| Level | Reads | Use it for |
|---|---|---|
| `basic` | SQL columns only — never a block payload | packages with millions of membership blocks, or a fast pre-flight |
| `deep` *(default)* | + every membership/value payload | the normal correctness check |
| `external` | + every registered asset file (SHA-256) | before trusting a package whose sources may have moved or changed |

```text
basic     GeoPackage metadata and registered layers
          USAP profile presence and package_iri (INVALID_PACKAGE_IRI)
          orphan references
          membership/value block structure (counts, bounds, element kinds)
          assessment integrity:
            block annotation matches its assessment's
                                        (ASSESSMENT_ANNOTATION_MISMATCH)
            block stays inside its assessment's asset
                                        (MEMBERSHIP_OUTSIDE_ASSESSMENT_ASSET,
                                         VALUE_BLOCK_OUTSIDE_ASSESSMENT_ASSET)
            assessment status domain     (ASSESSMENT_UNKNOWN_STATUS)
            assessment covering nothing  (ASSESSMENT_WITHOUT_MEMBERSHIP, warning)
          semantic class closure
          concept registry duplicates
          annotation primary object / 'represents' link agreement
          duplicate relationship edges (warning)
          unclassified relationship types in use (warning)
          relationship targets outside the package (warning)

deep      + membership payload decoding, offsets, stored min/max agreement
          + value payload decoding and stored min/max agreement
          + asset extent recomputation
          + more than one CRS across the registered assets
                                         (MIXED_ASSET_CRS, warning)
          + content hash canonical form  (NON_CANONICAL_CONTENT_HASH, warning)
          + annotated asset part with no declared indexing convention
                                         (ASSET_PART_NO_INDEXING_PROFILE, warning)
          + city object containment cycles (containment category only)
          + annotation status / confidence / attributes-JSON domain values

external  + asset file exists            (ASSET_FILE_MISSING)
          + asset file hash unchanged    (ASSET_FILE_CHANGED)
          + asset registered unhashed    (ASSET_NOT_HASHED, warning)
```

`verify_assets(pkg.conn)` runs the external check on its own and returns one
record per asset (`ok` / `missing` / `changed` / `unhashed`) instead of a
report. Note what this does **not** do: USAP can detect that a file changed,
but it cannot rebind annotations to it — element indices into the new file
may mean something entirely different. Re-register the new version as its own
asset.

`build_project_package` validates at `deep` before committing; set
`"validation_level"` in the project config to change that.

### Domain values

`status`, `object_status` and `confidence` are constrained, on write
(`create_annotation`/`update_annotation`/`create_city_object` raise) and at
the `deep` validation level for packages written another way:

```text
annotation status    draft | accepted | rejected | superseded
city object status   accepted | temporary
confidence           NULL, or a number in [0, 1]
attributes_json      NULL, or text that parses as JSON
```

Timestamps (`usap_annotation.created_at` / `updated_at`,
`usap_edit_log.created_at`) are UTC ISO-8601 to the second, with the `Z`
offset — `2026-08-08T14:37:30Z`. Not SQLite's `CURRENT_TIMESTAMP`
(`YYYY-MM-DD HH:MM:SS`), which has no date/time separator and no timezone
marker and so is not an `xsd:dateTime`.

---

## Standards positioning

USAP's annotation model lines up with the **W3C Web Annotation Data Model**:
an annotation carries bodies identifying the semantic concept and the
authoritative city object, and targets selecting indexed elements of
operational 3D assets. The concept-registry format is likewise a thin JSON
form of a SKOS concept scheme (see "Concept registries").

**Full adoption is future work** — a published JSON-LD context, a USAP
vocabulary for the 3D-specific terms W3C leaves extensible (a digest-based
state and an indexed-element selector), and conforming export/import. USAP
does **not** currently claim conformance, and a `.usap.gpkg` is not a W3C
JSON-LD document.

What is already in place are the parts that could not be added afterwards
without rewriting existing packages:

```text
package identity      usap_profile.package_iri, minted as a UUID URN
content hashes        canonical 'algorithm:digest'
timestamps            UTC ISO-8601 with 'Z'
index provenance      usap_asset_part.indexing_profile
concept provenance    source_namespace / concept_iri, backfillable by re-seeding
```

Deliberately still open: which namespace concept IRIs live under. Nothing
above depends on that choice — recording provenance is what keeps the
identifier *derivable* whenever the namespace is settled.

The full mapping, the remaining work, and the conformance classes it would
introduce are in
[FW_FEATURE_W3C_WEB_ANNOTATION_PROFILE.md](../FW_FEATURE_W3C_WEB_ANNOTATION_PROFILE.md).