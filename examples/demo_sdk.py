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

from usap import ELEMENT_KIND_FACE, USAPPackage

def main() -> None:
    with USAPPackage.create(
        "demo_sdk.usap.gpkg",
        overwrite=True,
    ) as pkg:
        # ------------------------------------------------------------
        # 1. External mesh asset
        # ------------------------------------------------------------
        asset_id = pkg.register_asset(
            uri="city_mesh.glb",
            asset_kind="mesh",
            media_type="model/gltf-binary",
            content_hash="fake_hash_for_phase_1a",
        )

        # ------------------------------------------------------------
        # 2. Stable mesh primitive / asset part
        # ------------------------------------------------------------
        asset_part_id = pkg.register_asset_part(
            asset_id=asset_id,
            part_path="node=0/mesh=0/primitive=0",
            element_kind=ELEMENT_KIND_FACE,
            element_count=10000,
        )

        # ------------------------------------------------------------
        # 3. Semantic classes
        # ------------------------------------------------------------
        building_class_id = pkg.create_semantic_class(
            scheme="citygml",
            scheme_version="3.0",
            class_uri="citygml-3.0:building:Building",
            local_name="Building",
        )

        roof_class_id = pkg.create_semantic_class(
            scheme="citygml",
            scheme_version="3.0",
            class_uri="citygml-3.0:building:RoofSurface",
            local_name="RoofSurface",
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
        pkg.link_city_objects(
            parent_city_object_id=building_id,
            child_city_object_id=roof_id,
            relationship_type="boundedBy",
            role="roof",
            graph_name="usap_default",
        )

        # ------------------------------------------------------------
        # 6. Annotation
        # ------------------------------------------------------------
        annotation_id = pkg.create_annotation(
            annotation_uid="ann_building_1_roof_mesh",
            semantic_class_id=roof_class_id,
            primary_city_object_id=roof_id,
            label="Roof of building_1 in mesh",
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
            print("Label:", match["label"])
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