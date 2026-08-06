**Status:** implemented (2026-07-07) — see `INGESTION.md` for the three
procedures as documented, runnable flows (`build_project_package_from_file`
with `annotation_batches` / `update=True`; carrier city objects via
`create_missing_city_objects`; minimal entry = id + concept + elements).

Initializations required:
1) given a series of 3D assets, a cityGML describing all city objects, a json with all the links between the cityObjects and the elements of the 3D assets (names are the id used in the cityGML), it creates the usap
2) given a series of 3D assets, a minimal vocabulary, a json with all the links between the cityObjects and the elements of the 3D assets (names can be anithing, only assumption is that they are unique), it creates the usap
3) give a usap built as in the previous cases, it can can be edited by adding a new assets, or editing the current annotations, with json formatted as in the previous points. Using temporary/custom names as in (2) in usap files built as in (1) is discouraged. 
