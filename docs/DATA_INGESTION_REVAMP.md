# Data-ingestion workflow revamp

**Status:** implemented (2026-07-07). See [INGESTION.md](INGESTION.md) for the documented, runnable workflows based on `build_project_package_from_file`, `annotation_batches`, `update=True`, and optional carrier city objects created with `create_missing_city_objects`.

The implemented procedures are:

1. **CityGML-backed initialization:** given operational 3D assets, a CityGML semantic authority, and a linking JSON, create a USAP package. CityGML supplies authoritative city-object identities, classes, and relationships. The linking JSON associates indexed elements of the operational assets with those city objects.
2. **Minimal-vocabulary initialization:** given operational 3D assets, a minimal vocabulary, and a linking JSON, create a USAP package without CityGML. City-object identifiers may be project-defined as long as they are unique; the minimal annotation entry is object id + concept + element indices.
3. **Package update:** add assets or concepts and create, replace, or edit annotations in a package produced by either initialization procedure. Reusing procedure-2 temporary/custom city-object names in a CityGML-backed package is discouraged unless explicitly requested.

Across all three procedures, USAP stores the claim layer — object/concept links, element memberships, status, confidence, provenance, and optional value fields. It does not copy source geometry or authoritative city-object properties.
