# USAP integration

(More information is in [REFERENCE.md](REFERENCE.md), ingestion workflows are in [INGESTION.md](INGESTION.md), and the concept/model is in [README.md](../README.md).)

## What you integrate against

USAP is a **Python library**. A package is a single `*.usap.gpkg` SQLite file for one study area. It acts as a bridge between authority-side semantic identities and stable element indices in operational 3D assets. You drive it through the `USAPPackage` object for interactive edits or a one-shot config builder for bulk import.

```python
import usap
```

## Examples

```python
from usap import USAPPackage, register_mesh_asset, seed_default_citygml_vocabulary

with USAPPackage.create("area.usap.gpkg", overwrite=True) as pkg:   # or .open(path)
    seed_default_citygml_vocabulary(pkg)                 # register accepted concepts
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
| `pkg.list_city_objects(object_status=None, parent_object=None, ...)` | object list / tree (`parent_object` = expand a node to its direct children; `object_status="temporary"` = carrier objects) |
| `pkg.list_accepted_concepts(scheme=None, search=None, in_use=None)` | the vocabulary picker |
| `pkg.list_annotations(status=None, city_object_uid=None, limit=None, ...)` | annotation list (filters AND-combined) |
| `pkg.get_annotation(annotation_id \| annotation_uid=...)` | one annotation + its membership/value summary |
| `pkg.elements_for_city_object(uid)` / `elements_for_annotation(id)` | which operational-asset elements a city object or annotation covers through USAP claims |
| `pkg.values_for_annotation(id)` / `elements_where(id, (">", 0.5))` | a value field's data / value query |

## Edit

```python
pkg.update_annotation(ann_id, status="accepted", confidence=0.9)  # partial update
pkg.attach_annotation_elements(annotation_id=ann_id, ...)         # add another asset membership
pkg.replace_annotation_membership(ann_id, part, "face", [1, 2])   # replace selected elements
pkg.replace_value_field(ann_id, part, "face", new_values)         # rewrite a value field
pkg.delete_annotation(ann_id)                                     # cascades its blocks
```

## Notes

- **USAP stores no source geometry.** A 3D viewer or processing pipeline remains responsible for meshes and point clouds. USAP identifies annotated elements by their stable integer index within an asset part. The application must map a viewer pick to `(asset_part_id, element_index)` and keep the registered source version immutable.
- **A city-object link and an asset membership are different.** The city-object link identifies the authoritative semantic instance that the claim represents or concerns. Membership blocks identify the selected points or faces in operational assets. `attach_annotation_elements` adds another geometric membership to the same claim; it does not create another city object.
- **USAP does not duplicate native CityGML object geometry.** When CityGML is used as the semantic authority, its own object-geometry association remains in the CityGML source. USAP is used for the separate element-level links and claims over registered operational assets.
- **Bulk vs interactive entry.** For first import, use `build_project_package_from_file("project.json")` (one config lists assets + vocabularies + annotation batches). For live editing, use the `USAPPackage` methods above.
- **Large source files stay outside the edit path.** After registration, changing an annotation updates the USAP package rather than rewriting the mesh, point cloud, or semantic authority. Query cost is governed by the stored USAP blocks and result size, not by rereading the source asset.
- **Errors.** Catch `usap.USAPError` (bad refs, constraint violations, out-of-range indices, unsupported dtypes) and its subclass `usap.USAPAmbiguityError` (a reference that matches more than one record).

## Requirements

Python ≥ 3.11; `pip install -e .`

Dependencies: `numpy`, `laspy`, `lxml`, `trimesh`.
