# USAP integration

(More information is in [REFERENCE.md](REFERENCE.md), ingestion workflows are in [INGESTION.md](INGESTION.md), and the concept/model is in [README.md](../README.md).)

## What you integrate against

USAP is a **Python library**. A package is a single `*.usap.gpkg` SQLite file for one study area. It acts as a bridge between authority-side semantic identities and stable element indices in operational 3D assets. You drive it through the `USAPPackage` object for interactive edits or a one-shot config builder for bulk import.

```python
import usap
```

## Examples

```python
from usap import USAPPackage, load_citygml_schema, register_mesh_asset

with USAPPackage.create("area.usap.gpkg", overwrite=True) as pkg:   # or .open(path)
    # USAP ships no CityGML vocabulary. Concepts are read from the OGC
    # schemas you supply, so the package asserts nothing of its own about
    # what CityGML contains.
    load_citygml_schema(pkg, "citygml-3.0-schemas/")
    mesh = register_mesh_asset(pkg, "area.obj", representation_name="lod2")
    ann = pkg.annotate_elements(                         # create an annotation
        concept="RoofSurface",
        asset_part_id=mesh.primary_asset_part_id,
        element_kind="face",
        element_indices=[40, 41, 42],
    )

    hits = pkg.annotations_for_elements(                 # reverse query: pick → claims
        asset_part_id=mesh.primary_asset_part_id,
        element_kind="face",
        selected_indices=[41],
    )
    assert pkg.validate_report().is_ok                   # integrity check
```

Assets can also be registered without a file adapter (`pkg.register_asset(...)` + `pkg.register_asset_part(...)`) when your pipeline already knows the element counts. Point clouds use `register_las_asset`; CityGML object identities, classes, and relationships use `import_citygml_semantics`. These entry points have different roles: the first establishes a geometry index space, while the second establishes authority-side semantic identity and provenance.

## Browse

Every list returns `list[dict]`; every getter returns a `dict` (or `None`).

| Call | Populates |
|---|---|
| `pkg.list_assets(asset_kind=None)` | registered assets + part/element counts |
| `pkg.list_asset_parts(asset_id=None)` | the index spaces of an asset |
| `pkg.list_city_objects(object_status=None, related_to=None, descendants_of=None, direction="out", ...)` | object list / graph walk (`related_to` = expand a node one hop, `descendants_of` = it and its parts, `direction` = `out`/`in`/`both`; `object_status="temporary"` = carrier objects) |
| `pkg.related_city_objects(uid, direction="out", ...)` | the **edges** touching an object, with link type, code space, category and role — the only view that shows a target outside the package |
| `pkg.list_relationship_types(category=None)` | the link vocabulary + edge counts; `category=None` also lists unclassified types |
| `pkg.register_relationship_type(name, code_space=..., category=...)` | classify a link type (what an ontology supplies) |
| `load_citygml_schema(pkg, path)` | register concepts and their hierarchy from the OGC XSDs |
| `load_ontology(pkg, path)` | register link types, their categories, and ADE classes from an ontology (RDF/XML built in; `.ttl`/`.n3`/`.nt`/`.jsonld` need `usap[ttl]`) |
| `pkg.list_accepted_concepts(scheme=None, search=None, in_use=None)` | the vocabulary picker |
| `pkg.list_annotations(status=None, city_object_uid=None, asset_id=None, limit=None, ...)` | annotation list (filters AND-combined); `asset_id`/`asset_part_id` is how an app loads the annotations of the asset it just opened |
| `pkg.get_annotation(annotation_id \| annotation_uid=...)` | one annotation + its assessment/membership/value summary |
| `pkg.list_assessments(annotation_id=...)` | the dated evaluations of a claim, by date and 3D asset |
| `pkg.elements_for_city_object(uid)` / `elements_for_city_objects([uids])` / `elements_for_annotation(id, assessment=None, asset_part_id=None)` | which operational-asset elements a city object or annotation covers through USAP claims |
| `load_vocabulary_folder(pkg, path)` | seed a whole configuration directory: `.xsd` schemas, `.owl`/`.ttl` ontologies, `.json` vocabularies |
| `pkg.values_for_annotation(id)` / `elements_where(id, (">", 0.5))` | a value field's data / value query |

## Edit

```python
pkg.update_annotation(ann_id, status="accepted", confidence=0.9)  # partial update
pkg.attach_annotation_elements(annotation_id=ann_id, ...)         # add another asset membership
pkg.replace_annotation_membership(ann_id, part, "face", [1, 2])   # replace selected elements
pkg.replace_value_field(ann_id, part, "face", new_values)         # rewrite a value field
pkg.update_asset(asset_id, uri="./moved/area.obj")                # repair a moved file
pkg.delete_annotation(ann_id)                                     # cascades its blocks
```

Re-evaluating a claim later is an **assessment**, not a new annotation:

```python
later = pkg.create_assessment(ann_id, asset_id, assessed_at="2027-03-01")
pkg.attach_annotation_elements(                       # the re-surveyed extent
    annotation_id=ann_id, asset_part_id=part,
    element_kind="face", element_indices=[120, 121],
    assessment=later["assessment_id"],
)
pkg.elements_for_annotation(ann_id, assessment=later["assessment_id"])
```

## Notes

- **USAP stores no source geometry.** A 3D viewer or processing pipeline remains responsible for meshes and point clouds. USAP identifies annotated elements by their stable integer index within an asset part. The application must map a viewer pick to `(asset_part_id, element_index)` and keep the registered source version immutable.
- **Register assets from the code that loads them for display.** This is the one integration mistake nothing can detect afterwards. The application owns the loader, so the application produces the indices USAP stores; if its loader triangulates quads, dedupes vertices, or orders faces differently from whatever counted the elements at registration, every membership silently points at the wrong geometry and no validation level will notice. Registering through the generic `register_asset` + `register_asset_part` path from the same code that builds the render buffers makes count and order agree by construction — and sidesteps the mesh adapter's `.glb`/`.gltf` refusal, since the adapter is then not involved. Record the convention in `indexing_profile`; `validate_report()` warns `ASSET_PART_NO_INDEXING_PROFILE` when an annotated part declares none.
- **City objects need not come from CityGML.** When the semantic source is owned by another system, create carriers on demand — `create_city_object(object_uid=gml_id, object_status="temporary")` — and skip `import_citygml_semantics` entirely. Consequence: with no imported link graph there is nothing for `elements_for_city_object(include_descendants=True)` to walk, so an application that wants "this Building and all its surfaces" walks its own hierarchy and passes the set to `elements_for_city_objects([...])`.
- **Register the semantic source with `compute_hash=False`, or not at all.** If another system edits the CityGML, a hash recorded here would report `ASSET_FILE_CHANGED` at the `external` validation level for a file USAP does not own.
- **The vocabulary is seeded into the package, not read at annotation time.** `load_vocabulary_folder` (or the individual loaders) copies concepts into `usap_semantic_class`; CRUD then validates against that copy, which is why a package is portable and why a concept cannot vanish from under existing annotations when a config file is edited. Seed on create and **re-seed on open**: seeding is idempotent and enriching, filling in what is missing and raising only on a genuine contradiction — which the application should surface rather than swallow. Note the asymmetry: concepts are gated (an unregistered one raises), link types are not (an unregistered name auto-registers).
- **One thread per connection.** `sqlite3` connections are thread-bound; using a `USAPPackage` from a worker thread raises `ProgrammingError` at runtime, not at review. Confine USAP to one thread, or open a connection per thread.
- **A city-object link and an asset membership are different.** The city-object link identifies the authoritative semantic instance that the claim represents or concerns. Membership blocks identify the selected points or faces in operational assets. `attach_annotation_elements` adds another geometric membership to the same claim; it does not create another city object.
- **USAP does not duplicate native CityGML object geometry.** When CityGML is used as the semantic authority, its own object-geometry association remains in the CityGML source. USAP is used for the separate element-level links and claims over registered operational assets.
- **Bulk vs interactive entry.** For first import, use `build_project_package_from_file("project.json")` (one config lists assets + vocabularies + annotation batches). For live editing, use the `USAPPackage` methods above.
- **Large source files stay outside the edit path.** After registration, changing an annotation updates the USAP package rather than rewriting the mesh, point cloud, or semantic authority. Query cost is governed by the stored USAP blocks and result size, not by rereading the source asset.
- **Errors.** Catch `usap.USAPError` (bad refs, constraint violations, out-of-range indices, unsupported dtypes) and its subclass `usap.USAPAmbiguityError` (a reference that matches more than one record).

## Requirements

Python ≥ 3.11; `pip install -e .`

Dependencies: `numpy`, `laspy`, `lxml`, `trimesh`, `pyroaring`. Optional extras: `[laz]` (LAZ), `[crs]` (CRS parsing), `[ttl]` (Turtle/N3/JSON-LD ontologies).

USAP supplies no CityGML vocabulary of its own. An import needs two inputs from you: the OGC CityGML 3.0 **XSDs** (`load_citygml_schema`) for the concepts and their hierarchy, and a statement of which link types mean *part of* (`load_ontology`, `register_relationship_type`, or a project-config `relationship_types` block). Without the second, edges are still recorded and queryable by name, but nothing is reported as a part and `validate_report()` warns `UNCLASSIFIED_RELATIONSHIP_TYPE`.
