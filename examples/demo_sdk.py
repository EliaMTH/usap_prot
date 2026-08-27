# Reminder:
# def method(self, ...)
#     I need one existing package.

# @classmethod
# def method(cls, ...)
#     I need the USAPPackage class, usually to create a package.

# @staticmethod
# def method(...)
#     I do not need either; I am just a helper.


from __future__ import annotations

import _bootstrap  # noqa: F401  -- puts src/ on sys.path from a checkout

from usap import ELEMENT_KIND_FACE, USAPPackage
from usap.constants import (
    CITYGML_3_0_BUILDING_NS,
    CITYGML_3_0_CONSTRUCTION_NS,
    CITYGML_3_0_CORE_NS,
    concept_uri,
)

def main() -> None:
    with USAPPackage.create(
        "demo_sdk.usap.gpkg",
        overwrite=True,
    ) as pkg:
        # ------------------------------------------------------------
        # 1. External mesh asset
        # ------------------------------------------------------------
        # No file backs this demo, so there is nothing to hash. Registering
        # without one is honest; a placeholder token in the digest column
        # would make the asset look verifiable and trips the
        # NON_CANONICAL_CONTENT_HASH warning at deep validation. Real
        # registrations get 'sha256:<hex>' from the adapters.
        asset_id = pkg.register_asset(
            uri="city_mesh.ply",
            asset_kind="mesh",
            media_type="application/ply",
        )

        # ------------------------------------------------------------
        # 2. Stable mesh primitive / asset part
        # ------------------------------------------------------------
        asset_part_id = pkg.register_asset_part(
            asset_id=asset_id,
            part_path="node=0/mesh=0/primitive=0",
            element_kind=ELEMENT_KIND_FACE,
            element_count=10000,
            # State which convention numbered these faces. A part registered
            # without one is reported by validate_report(): an index only means
            # something relative to the reader that assigned it, and nothing can
            # detect a mismatch after the fact.
            indexing_profile="usap:demo-face-order-v1",
        )

        # ------------------------------------------------------------
        # 3. Semantic classes
        # ------------------------------------------------------------
        # Created by hand here to keep the demo self-contained; a real package
        # reads them from a schema with load_citygml_schema(). The identity
        # must match either way — note RoofSurface is a *construction* concept
        # in CityGML 3.0, not a building one.
        building_class_id = pkg.create_semantic_class(
            scheme="citygml",
            scheme_version="3.0",
            class_uri=concept_uri(CITYGML_3_0_BUILDING_NS, "Building"),
            local_name="Building",
            source_namespace=CITYGML_3_0_BUILDING_NS,
        )

        roof_class_id = pkg.create_semantic_class(
            scheme="citygml",
            scheme_version="3.0",
            class_uri=concept_uri(CITYGML_3_0_CONSTRUCTION_NS, "RoofSurface"),
            local_name="RoofSurface",
            source_namespace=CITYGML_3_0_CONSTRUCTION_NS,
        )

        # ------------------------------------------------------------
        # 4. City objects
        # ------------------------------------------------------------
        building_id = pkg.create_city_object(
            object_uid="building_1",
            semantic_class_id=building_class_id,
        )

        roof_id = pkg.create_city_object(
            object_uid="building_1_roof_1",
            semantic_class_id=roof_class_id,
        )

        # ------------------------------------------------------------
        # 5. usap_default object graph
        # ------------------------------------------------------------
        # The link type is the CityGML property the edge stands for, with the
        # namespace that gives it identity. `category` says what the link
        # *means* for traversal — no CityGML artifact states that, so the
        # builder does. Without it the edge is still recorded and queryable by
        # name, but the roof is not reported as part of the building.
        pkg.link_city_objects(
            building_id,
            roof_id,
            "boundary",
            code_space=CITYGML_3_0_CORE_NS,
            category="containment",
        )

        # ------------------------------------------------------------
        # 6. Annotation
        # ------------------------------------------------------------
        annotation_id = pkg.create_annotation(
            annotation_uid="ann_building_1_roof_mesh",
            semantic_class_id=roof_class_id,
            primary_city_object_id=roof_id,
            status="accepted",
            confidence=1.0,
        )

        # ------------------------------------------------------------
        # 7. Membership replacement
        #
        # This replaces all existing membership for this annotation
        # in this asset part.
        # ------------------------------------------------------------
        pkg.replace_annotation_membership(
            annotation_id=annotation_id,
            asset_part_id=asset_part_id,
            element_kind=ELEMENT_KIND_FACE,
            element_indices=[100, 101, 102, 6000, 6001],
        )

        # ------------------------------------------------------------
        # 8. Query selected elements -> annotations
        # ------------------------------------------------------------
        selected_faces = [100, 101, 6000]

        matches = pkg.annotations_for_elements(
            asset_part_id=asset_part_id,
            element_kind=ELEMENT_KIND_FACE,
            selected_indices=selected_faces,
        )

        print("Selected faces:", selected_faces)
        print()

        for match in matches:
            print("Annotation:", match["annotation_uid"])
            print("Assessed at:", match["assessed_at"] or "(undated)")
            print("Status:", match["status"])
            print("Semantic class:", match["semantic_class"])
            print("Primary city object:", match["primary_city_object_uid"])
            print("Matched faces:", match["matched_elements"])
            print()

        # ------------------------------------------------------------
        # 9. Query annotation -> elements
        # ------------------------------------------------------------
        print("Annotation membership, expanded:")
        blocks = pkg.elements_for_annotation(
            annotation_id=annotation_id,
            expand=True,
        )

        for block in blocks:
            print(block)

        print()

        # ------------------------------------------------------------
        # 10. Query semantic class -> compact blocks
        # ------------------------------------------------------------
        print("RoofSurface membership blocks, compact:")
        roof_blocks = pkg.elements_for_semantic_class(
            semantic_class_id=roof_class_id,
            include_subclasses=True,
            expand=False,
        )

        for block in roof_blocks:
            print(block)

        print()

        # ------------------------------------------------------------
        # 11. Query city object -> compact blocks
        # ------------------------------------------------------------
        print("building_1 membership blocks through usap_default:")
        building_blocks = pkg.elements_for_city_object(
            object_uid="building_1",
            include_descendants=True,
            graph_name="usap_default",
            expand=False,
        )

        for block in building_blocks:
            print(block)

        print()

        # ------------------------------------------------------------
        # 12. Basic validation
        # ------------------------------------------------------------
        report = pkg.validate_report()

        if not report.issues:
            print("Validation: OK")
        else:
            print("Validation problems:")
            for issue in report.issues:
                print("-", issue.format())


if __name__ == "__main__":
    main()