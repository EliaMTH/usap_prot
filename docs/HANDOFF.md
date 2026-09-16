# Integration rules for the desktop application

This document states the rules that an application that uses USAP has to uphold, because USAP cannot enforce them from its side of the boundary. Most of them are things that fail *silently* if broken.

---

## 0. What USAP is responsible for, and what it is not

USAP stores **claims**: which indexed elements of an external 3D asset are
associated with which semantic concept, and optionally with which city object.

It stores **no geometry**, does **no spatial reasoning**, and owns **no meaning**. It never compares coordinates, never matches a point to a face,
never checks that two assets overlap. An element is an integer index into a
file it does not read.

---

## 1. Asset registration — the highest-risk rule

**The element indices USAP stores are meaningless without the numbering
convention that produced them.**

If the application's loader triangulates, reorders, merges, or filters faces
differently from whatever counted elements at registration time, then every
membership in the package points at the wrong geometry. No validation level
detects this. The package is internally consistent; it is consistent about the
wrong thing. There is no repair, because nothing records what the indices used
to mean.

So:

- **Register from the same code that loads the asset for display.** Take
  `element_count` from the loader's own arrays — the count of faces the
  application will actually draw and lasso — not from a second reader.
- Use the generic path:

  ```python
  asset_id = pkg.register_asset(uri=..., asset_kind="mesh", content_hash=...)
  part_id  = pkg.register_asset_part(
      asset_id=asset_id,
      part_path="geometry/0",
      element_kind=ELEMENT_KIND_FACE,
      element_count=len(loader_faces),
      indexing_profile="yourapp:mesh-face-order-v1",
  )
  ```

- **Always pass `indexing_profile`.** It is a free-form string naming your
  convention. It is advisory to USAP, but it is compared on re-registration,
  so reading one part under two different conventions raises instead of
  silently repointing every membership. `validate_report()` warns
  `ASSET_PART_NO_INDEXING_PROFILE` when it is absent, and that warning should
  be treated as an error during integration.
- Change the profile string whenever the loader's numbering changes. A new
  convention is a new index space, not a new version of the old one.
- **Do not use `register_mesh_asset` / `register_las_asset` from the
  application.** They are convenience readers for scripts and for building
  fixtures. They count elements with *their* reader (trimesh / laspy), which is
  not your loader, and that is exactly the mismatch this section is about.

`register_asset` is idempotent on `(uri, content_hash)` and
`register_asset_part` on `(asset_id, part_path, element_kind)`; a re-run with
different values raises rather than returning a row describing something else.

The pair is a uniqueness key, not a lookup you have to reproduce exactly, so
`content_hash` follows the same omission rule as every other field:

- **omitting it still finds the row.** `register_asset(uri, kind)` against an
  asset you registered with a hash returns that asset's id. You do not have to
  re-digest a 10 GB file to ask for an id you already own;
- **supplying one against a row that has none fills it in**, so a first pass
  with `compute_hash=False` and a later one that computes it end with a single
  record that `verify_assets` can check — not two;
- **supplying one that differs registers a separate asset.** That is a new
  version of the file, and keeping the rows apart is what the uniqueness key is
  for: the annotations indexed against the old geometry stay indexed against it.

Once a uri is on file at more than one hash it no longer names a single asset,
so a call that omits the hash raises `USAPAmbiguityError` rather than choosing —
pass the hash, or use `asset_id`. A package that already holds a uri split
across a hashed and an unhashed row reports `DUPLICATE_ASSET_URI` (warning):
re-register with the hash to merge the records.

### Detecting that the file changed

Annotations are bound to one immutable version of an external file.
`verify_assets(pkg.conn)` re-hashes every registered asset and reports `ok` /
`missing` / `changed` / `unhashed` per asset. Run it when a project is opened,
and surface `changed` prominently: the indices may now point at different
elements and nothing inside the package can tell.

Pass a `content_hash` at registration, or `verify_assets` can only ever report
`unhashed`.

**Register assets under a uri relative to the package, not an absolute path.**
A relative uri is resolved against the directory holding the `.usap.gpkg` — the
rule glTF uses for external buffers and 3D Tiles for tileset content — so a
project folder can be moved, renamed, copied to another machine or handed to
someone else and every asset still verifies. An absolute path is used as given,
which bakes one machine's layout into the package and makes `verify_assets`
report `missing` everywhere else.

```python
pkg.register_asset(uri="catania.obj", ...)          # beside the package
pkg.register_asset(uri="meshes/catania.obj", ...)   # in a subfolder
```

The uri is also the string an annotation batch references as `asset_uri`, so
whatever you choose here is what the rest of the pipeline names the asset by.

A `file://` uri is understood — `file://catania.obj` resolves beside the package
exactly like the bare filename — and stored as written. Any other scheme
(`http://`, `s3://`, …) is refused at registration: USAP resolves asset uris as
paths and fetches nothing, so such a row could only ever verify as `missing`.

### Is the file the user just opened the one this package was built on?

`verify_assets` answers the opposite question — it starts from the registered
uri and checks the file it finds there. In an interactive application the user
picks the file, and the registered uri is *not* an identity: the file may have
been moved or renamed. The rule:

- **When the package carries a `content_hash`, decide on the hash.** Compare it
  against the hash of the chosen file, in the documented form. Do not compare
  file names — a rename defeats that, and it looks like a check while being none.
- **When it does not, say so.** Report *unverified* to the user rather than
  falling back to a name comparison dressed up as verification. `content_hash`
  is `None` more often than you would expect: the generic `register_asset` above
  defaults it that way, so this is the common path, not the edge case.

Use the exported helpers rather than reimplementing the tolerance rules:

```python
from usap import canonical_hash, parse_content_hash

# Compare parsed, not as text. A stored hash is not guaranteed to be in the
# canonical 'sha256:<lowercase hex>' form -- a bare 64-hex digest is also
# valid and is read as SHA-256 -- so `==` on the raw strings silently misses
# a match that is really there. parse_content_hash is the tolerance rule.
wanted = parse_content_hash(canonical_hash(chosen_file))

match = next(
    (
        a for a in pkg.list_assets()
        if a["content_hash"] is not None
        and parse_content_hash(a["content_hash"]) == wanted
    ),
    None,
)

if match is not None:
    # The step worth not stopping short of: record where the file actually is,
    # so the package verifies from here on instead of reporting `missing`
    # every time it is opened.
    pkg.update_asset(match["asset_id"], uri=str(chosen_file))
```

`canonical_hash` and `parse_content_hash` are part of the public API precisely
so every application does not invent its own reading of the spec. `None` from
`parse_content_hash` means the stored value is not a recognizable digest at all
— `validate_report()` reports those as `NON_CANONICAL_CONTENT_HASH` — and such
a row can never be matched, which is a case to report rather than to skip past.

A uri you write back is held to the same rule as one you register: a scheme
USAP cannot resolve (`http://`, `s3://`, …) is refused, since a record repaired
into one that can only ever verify as `missing` is not repaired.

---

## 2. City objects are identity anchors, nothing else

In this integration USAP holds **carrier** city objects: an identity to hang a
claim on, created on demand. The application never calls
`import_citygml_semantics`, and the CityGML file remains the only place object
classes, attributes and hierarchy live.

### 2.1 `object_uid` must be the `gml:id`

```python
pkg.create_city_object(object_uid=gml_id, gml_id=gml_id, object_status="temporary")
```

`object_uid` is an **input**, never derived by USAP: uniqueness is delegated to
whoever owns the semantic model, and within a CityGML document that is
`gml:id`. Passing it means a repeat call names the same instance, which is why
`create_city_object` returning the existing row is correct rather than lax.

Nothing in USAP enforces the equality. If the application ever passes something
else, the object tree, the lasso result list and the detail panel will disagree
about what to call the same object.

**Recommendation: derive `object_uid` from `gml_id` in one place** — a single
helper, or the façade function — so no call site can get it wrong
independently.

### 2.2 Do not pass the annotation's concept as the object's class

The object's class belongs to the CityGML (`RoofSurface`). The annotation's
concept is usually something else (`EnergyRoof`). They are different facts
about different things, and the second one belongs on the annotation.

Pass `semantic_class_id=None` when creating carriers. As of 0.4.2, passing a
*different* class for an existing `object_uid` raises rather than discarding it
— but the fix is to not pass one, not to catch the error. The annotation batch
now follows the same rule: carriers it creates with
`create_missing_city_objects` are classless, and the entry's `concept` classes
the annotation only.

The same holds for `gml_id`, `source_asset_id`, `source_object_id` and
`attributes_json`: supplying a value that *contradicts* the stored row raises.
Fields you omit are not compared, so `create_city_object(uid)` remains a valid
"give me the id for this uid" call.

**Supplying a value for a field that is still empty fills it in.**
that is an enrichment, not a conflict — it is how a carrier acquires its class,
its `gml_id` and its provenance when the CityGML that owns them arrives. The
distinction is exactly: NULL → value fills, value → different value raises.

An empty string counts as empty for `gml_id` and `source_object_id`. If your
writer emits `""` for "unknown", it is stored as NULL, and rows an older build
already wrote that way complete on the next call supplying a real value — you do
not need a migration or raw SQL to repair them.

**Do not write `attributes_json` onto a carrier.** This is the one column where
the fill-what-is-empty rule works against you, and the failure is much larger
than the row. `import_citygml_semantics` writes its own provenance there
(`source`, `citygml_local_name`, `citygml_namespace`), so a carrier already
holding a value the import would have to change raises — and because the import
runs as one transaction, that **aborts the entire import**. No other object in
the file is aligned either, however many thousands were fine. There is no API to
clear the column (see §2.3), so the package cannot then be aligned through the
SDK at all.

Per-object notes belong on the annotation, not the object: an annotation has a
`label` for a human-readable name and an `attributes` field of its own. Leave
the city object's `attributes_json` to the semantic source that owns it.

### 2.3 There is no `update_city_object`

By design. To change what an object *is*, change the CityGML. USAP is not the
place that fact lives. A stored value that is simply wrong means the package
disagrees with its source: rebuild it rather than editing it here.

**One narrow exception, and it does not change what the object is.**
`accept_city_object(uid)` moves `object_status` from `temporary` to `accepted`.
It records that the alignment *happened* — the class, the `gml_id` and the
provenance arrive through `create_city_object` backfill, from the semantic
source that owns them; this only clears the marker saying one was still
expected. It is one-way and idempotent, and `import_citygml_semantics` calls it
for you when it aligns a carrier. Call it yourself when something other than the
CityGML importer is what settled the object's identity — otherwise the object
stays listed by `list_city_objects(object_status="temporary")`, "still awaiting
alignment", for the life of the package.

---

## 3. Retrieving "this object and its parts"

**Use `elements_for_city_objects` (plural). Never
`elements_for_city_object(uid, include_descendants=True)`.**

The descendants form walks USAP's own link graph. In carrier-only mode that
graph is empty — no relationships are imported — so it returns the object's own
elements and nothing else, with no error and no warning. A building would
highlight none of its surfaces and the call would look like it worked.

Note `include_descendants` **defaults to `True`**, so the plain
`elements_for_city_object(uid)` is already the footgun. It is not a call the
application should make at all.

The application owns the CityGML tree, so it walks it:

```python
uids = citygml.subtree_ids(building_gml_id)   # your side
blocks = pkg.elements_for_city_objects(uids, expand=True)
```

Blocks come back de-duplicated by `membership_block_id`, so an annotation
reached through two objects appears once. Each block names its `asset_part_id`,
which is how the result is routed to the right viewport layer.

### 3.1 If you ever do build a link graph, classify it

Carrier-only mode has no edges, so this does not arise there. It arises the
moment a package is built by importing a CityGML — which is what
`build_project_package` does, and what any package handed to you was probably
made with.

An imported edge records the property name it came from (`boundary`), but
**nothing in CityGML says which properties mean *part of***. Until a link type
is classified, its edges are stored and queryable by name and skipped by every
subtree query, and `validate_report()` reports
`UNCLASSIFIED_RELATIONSHIP_TYPE`.

Project configs declare this in a `relationship_types` block. **The application
has no config**, so it must assert the same thing directly:

```python
pkg.register_relationship_type(
    "boundary",
    code_space="http://www.opengis.net/citygml/3.0",
    category="containment",
)
```

REFERENCE.md → *City object relationships* → **What to assert, for CityGML
3.0** carries the full thirteen-property mapping. Assert once, at build; the
category is stored on `usap_relationship_type` and survives close/reopen.
Re-asserting a *different* non-NULL category raises rather than overwriting.

---

## 4. Display labels

An annotation carries a `label`: a free-text caption, yours to set and edit,
returned by every read path. Where the user stories say "label", that column is
what they mean.

It is deliberately **not** an identifier. No UNIQUE constraint, no index, and no
lookup accepts it — `get_annotation` takes an `annotation_id` or an
`annotation_uid` and nothing else. Two annotations may carry the same caption,
and USAP will not disambiguate them for you.

**A label is optional, so have a fallback.** It is NULL until something sets it,
and nothing sets it implicitly. Compose the fallback from what is always there:

| Case | Fallback when `label` is NULL |
|---|---|
| annotation linked to a city object | `semantic_class` + `primary_city_object_gml_id` |
| not linked | `semantic_class` + `annotation_uid` |

The unlinked case is not an edge case. A lasso annotation with no city object
is explicitly permitted, and **every** value-field annotation has
`primary_city_object_id` NULL by design — value fields are properties of the
geometry, not of an object.

All three read paths — `get_annotation`, `list_annotations`,
`annotations_for_elements` — return `label` alongside `primary_city_object_uid`
and `primary_city_object_gml_id`, so the lasso result list and the detail panel
render the identical string without a second query per row.

---

## 5. Loading a project

`list_annotations(asset_id=...)` is the load step: it returns the annotations
with membership in the asset just opened, without walking every annotation.
`asset_part_id` narrows further, to one index space within the asset.

An annotation spanning two assets appears in **both** lists — this is a filter,
not a partition, and cross-asset claims are USAP's headline capability. An
annotation with no membership anywhere (freshly created, or value-field only)
appears in neither.

The vocabulary is loaded once at startup from a **configuration folder**, not a
single file:

```python
load_vocabulary_folder(pkg, config_dir)
```

The folder holds the OGC CityGML 3.0 XSDs (concepts **and** hierarchy — the
only artifact that carries `substitutionGroup`), `.owl` files in RDF/XML (ADE
concepts, link types, `usap:category`), and optionally `.json` registries. All
of them load in one pass, in dependency order.

Two consequences for the installer:

- **The OGC XSDs are not vendored.** USAP ships no CityGML vocabulary of its
  own, deliberately. The application installer must place them in the
  configuration folder or the package comes up with zero CityGML concepts.
- **Ontologies must be RDF/XML.** Turtle and the other non-XML RDF syntaxes are
  refused with a "convert to RDF/XML" message — including inside the folder,
  where being passed over would surface as "fewer concepts than configured" and
  nothing would say why.

Re-seeding on every open is the intended usage: it is idempotent and additive,
and raises only on a genuine contradiction.

---

## 6. Assessments

An annotation is the logical claim; an **assessment** is one dated evaluation
of it against **one asset**. Membership and value blocks hang off the
assessment, not the annotation.

- `annotate_elements(...)` without `assessed_at` puts the selection in the
  annotation's undated default assessment. That is the ordinary single-pass
  case and needs no assessment handling at all.
- To record a re-assessment, do **not** call `annotate_elements` again — it
  would mint a second annotation. Call `create_assessment(annotation_id, asset,
  assessed_at=...)` then `attach_annotation_elements(..., assessment=...)`.
- Once an annotation has more than one assessment on an asset, a write that
  does not say which one raises `USAPAmbiguityError`, listing the options. This
  is deliberate: guessing would silently edit the wrong evaluation. The
  application must catch it and pass `assessment=`.
- `annotations_for_elements` returns **one entry per (annotation, assessment)**.
  Two evaluations covering different elements are two answers; collapsing them
  would report an extent no single evaluation ever claimed.

An assessment's asset cannot be repointed after creation — membership is
indexed against it.

---

## 6b. Ordered paths — reading a road centreline

Most annotations are sets: `elements_for_annotation` gives you the faces, in
ascending order, and the order carries no meaning. Some are **sequences** — a
road centreline, a traversal — and for those the order *is* the data.

**Telling the two apart costs you nothing.** Every read that returns an
annotation or an assessment summary carries `path_run_count`, and so does the
view:

```sql
SELECT annotation_uid, label, path_run_count
FROM usap_annotations_view
WHERE path_run_count > 0;
```

`0` means the claim is unordered and `elements_for_annotation` is the whole
story. Above `0`, the sequence is in `usap_path_block`.

**Reading it.** One row per *segment*, ordered by `segment_ordinal` within the
assessment:

```sql
SELECT asset_part_id, segment_ordinal, continues_previous,
       element_count, encoding, payload
FROM usap_path_block
WHERE assessment_id = ?
ORDER BY segment_ordinal;
```

Concatenate segments in ordinal order. Start a new **run** — a disjoint stretch
of the path — at every segment whose `continues_previous` is `0`. A run whose
segments name different `asset_part_id`s is one continuous stretch crossing a
part boundary; a new run is a genuine interruption.

**The payload.** `encoding` is `u32-seq-zlib`: zlib-inflate, then read
contiguous little-endian `uint32`. No count prefix, so the number of indices is
`len(inflated) / 4`.

The byte layout is identical to the `u32-zlib` you already decode, so reuse the
inflater — but **not its assumptions**, because all three are inverted:

- the values are **absolute element indices**, not offsets: there is no
  `block_start` here and nothing to add;
- they are in **sequence order**, not ascending — do not sort, and do not binary
  search;
- they may **repeat**, where a route doubles back over the same face.

Switch on the per-row `encoding` column, as you already do for membership. Never
on the profile version.

**Do not write membership for an ordered claim.** The membership is derived from
the path and rewriting it directly would leave the path describing a selection
that no longer exists. Through the SDK that raises; writing SQL directly it will
not, and `usap validate` reports it as `PATH_MEMBERSHIP_MISMATCH`.

**If you meet `usap:path` in `attributes`**, the package predates the table. The
key is no longer read, it cannot say which asset part its indices belong to, and
`usap validate` reports it as `PATH_IN_ATTRIBUTES`. Rebuild the package.

## 7. The two-file commit protocol

USAP guarantees its own half is atomic:

```python
with pkg.transaction():
    ...          # several SDK calls; all commit or none do
```

It cannot guarantee the `.gml` / `.usap.gpkg` **pair**. Several user stories
require that neither file is left partially updated, and that is the
application's transaction to run, because it owns both files.

Recommended ordering:

1. Write the new CityGML to a **temporary file** beside the target.
2. Commit the USAP transaction.
3. On success, atomically rename the temp file over the CityGML.
4. On USAP failure, discard the temp file. Nothing moved.

The rename is last because it is the only step that is atomic at the filesystem
level and cannot be rolled back. Any other ordering has a window where one file
is updated and the other is not.

If the rename itself fails (permissions, a lock, a full disk), that is a
recoverable state to report to the user, with the temp file left in place — not
a state to fix by rolling USAP back, which would need a second write that can
also fail.

---

## 8. Concurrency — open, to be agreed

This is not settled and should be agreed before the C header is frozen, since
the header either promises thread-safety or does not.

Two things constrain it:

- `sqlite3` connections are bound to the thread that created them. A
  `USAPPackage` is not a thread-safe object.
- **WAL is off and should stay off** unless there is a reason to change it. WAL
  leaves `-wal` and `-shm` sidecar files, which breaks the single-file
  GeoPackage interop the format depends on — a `.usap.gpkg` is meant to be one
  file you can hand to someone or open in QGIS.

Recommendation: one open package, one dedicated worker thread, all calls
marshalled to it, no concurrent access. That makes WAL unnecessary and the
contract easy to state.

---

## 9. Validation

`pkg.validate_report()` runs at increasing levels; `external` additionally
re-hashes files. Two results worth deciding about in advance:

- `ASSET_PART_NO_INDEXING_PROFILE` — see §1. Do not ship with this warning
  present.
- `MIXED_ASSET_CRS` — a georeferenced point cloud registered alongside a
  local-coordinate mesh trips this. It is by design, not a defect: USAP does no
  spatial reasoning and simply reports that the assets do not share a reference
  system. Decide now whether it is acceptable in the delivered configuration,
  because the application will surface it to users.

---

## 10. Quick checklist

- [ ] `element_count` comes from the application's own loader
- [ ] `indexing_profile` is set on every part, and versioned with the loader
- [ ] `register_mesh_asset` / `register_las_asset` are not called from the app
- [ ] `content_hash` is recorded, and `verify_assets` runs on project open
- [ ] `object_uid == gml:id`, derived in exactly one place
- [ ] carriers are created with `semantic_class_id=None`
- [ ] subtree queries use `elements_for_city_objects` (plural)
- [ ] a NULL `label` falls back to concept + gml:id, and to concept + uid when unlinked
- [ ] the CityGML XSDs ship with the installer
- [ ] the CityGML write and the USAP commit follow the temp-file protocol
- [ ] `USAPAmbiguityError` is caught wherever re-assessment is possible
- [ ] the threading contract is agreed and the header matches it
