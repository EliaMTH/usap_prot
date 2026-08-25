# Integration rules for the desktop application

For the team building the C++ desktop application around USAP.

This is not an API listing — [API.md](API.md) is that, and
[REFERENCE.md](REFERENCE.md) is the manual. This document states the rules the
**application** has to uphold, because USAP cannot enforce them from its side
of the boundary. Most of them are things that fail *silently* if broken: the
package stays valid, every query answers, and the answers are wrong.

Read it once before writing integration code, and again before shipping.

---

## 0. What USAP is responsible for, and what it is not

USAP stores **claims**: which indexed elements of an external 3D asset are
associated with which semantic concept, and optionally with which city object.

It stores **no geometry**, does **no spatial reasoning**, and owns **no
meaning**. It never compares coordinates, never matches a point to a face,
never checks that two assets overlap. An element is an integer index into a
file it does not read.

The split, restated as ownership:

| Layer | Owner |
|---|---|
| Which objects and concepts exist, their attributes, their hierarchy | the semantic source (CityGML + the vocabulary folder) |
| Which elements of which asset are claimed as what | USAP |
| Viewport, lasso, layer panel, CRS handling, session and project state | the application |

Every rule below follows from that split.

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

### Detecting that the file changed

Annotations are bound to one immutable version of an external file.
`verify_assets(pkg.conn)` re-hashes every registered asset and reports `ok` /
`missing` / `changed` / `unhashed` per asset. Run it when a project is opened,
and surface `changed` prominently: the indices may now point at different
elements and nothing inside the package can tell.

Pass a `content_hash` at registration, or `verify_assets` can only ever report
`unhashed`.

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

Pass `semantic_class_id=None` when creating carriers. As of 0.4.1, passing a
*different* class for an existing `object_uid` raises rather than discarding it
— but the fix is to not pass one, not to catch the error.

The same now holds for `gml_id`, `source_asset_id`, `source_object_id` and
`attributes_json`: supplying a value that contradicts the stored row raises.
Fields you omit are not compared, so `create_city_object(uid)` remains a valid
"give me the id for this uid" call.

### 2.3 There is no `update_city_object`

By design. To change what an object *is*, change the CityGML. USAP is not the
place that fact lives.

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

---

## 4. Display labels

`usap_annotation` has **no label column**. It was removed in 0.4.0 as a
temporary field nobody used. Where the user stories say "label", they mean the
concept and the object identity, composed for display:

| Case | Label |
|---|---|
| annotation linked to a city object | `semantic_class` + `primary_city_object_gml_id` |
| not linked | `semantic_class` + `annotation_uid` |

The unlinked case is not an edge case. A lasso annotation with no city object
is explicitly permitted, and **every** value-field annotation has
`primary_city_object_id` NULL by design — value fields are properties of the
geometry, not of an object.

All three read paths — `get_annotation`, `list_annotations`,
`annotations_for_elements` — return both `primary_city_object_uid` and
`primary_city_object_gml_id`, so the lasso result list and the detail panel can
render the identical string. (Before 0.4.1 the reverse query returned only the
uid; if you are reading an older build, that is why.)

Uniqueness of displayed labels is the application's problem. Two annotations
may legitimately carry the same concept on the same object; USAP returns both
and does not disambiguate them, because a raw answer is useful and a
de-duplicated one is not recoverable.

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
- [ ] labels are composed from concept + gml:id, with an unlinked fallback
- [ ] the CityGML XSDs ship with the installer
- [ ] the CityGML write and the USAP commit follow the temp-file protocol
- [ ] `USAPAmbiguityError` is caught wherever re-assessment is possible
- [ ] the threading contract is agreed and the header matches it
