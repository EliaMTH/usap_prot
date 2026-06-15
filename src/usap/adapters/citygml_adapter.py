from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lxml import etree

from ..core import USAPPackage
from ..domain_vocab import seed_citygml_basic_classes


# CITYGML_OBJECT_CLASSES = {
#     "Building",
#     "BuildingPart",
#     "RoofSurface",
#     "WallSurface",
#     "GroundSurface",
#     "ClosureSurface",
#     "OuterCeilingSurface",
#     "OuterFloorSurface",
#     "Window",
#     "Door",
# }


ROLE_BY_CLASS = {
    "Building": "building",
    "BuildingPart": "building_part",
    "RoofSurface": "roof",
    "WallSurface": "wall",
    "GroundSurface": "ground",
    "ClosureSurface": "closure",
    "OuterCeilingSurface": "outer_ceiling",
    "OuterFloorSurface": "outer_floor",
    "Window": "window",
    "Door": "door",
}


RELATIONSHIP_BY_CONTEXT = {
    "boundedBy": "boundedBy",
    "boundary": "boundedBy",
    "opening": "opening",
    "consistsOfBuildingPart": "consistsOf",
    "buildingPart": "consistsOf",
    "cityObjectMember": "contains",
    "featureMember": "contains",
}


@dataclass(frozen=True)
class ImportedCityObject:
    city_object_id: int
    object_uid: str
    gml_id: str | None
    local_name: str
    semantic_class_id: int


@dataclass(frozen=True)
class ImportedRelationship:
    relationship_id: int
    parent_uid: str
    child_uid: str
    relationship_type: str
    role: str | None
    graph_name: str


@dataclass
class CityGMLImportResult:
    asset_id: int
    path: Path
    object_count: int
    relationship_count: int
    imported_objects: list[ImportedCityObject] = field(default_factory=list)
    imported_relationships: list[ImportedRelationship] = field(default_factory=list)


def _sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()

    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)

            if not chunk:
                break

            digest.update(chunk)

    return digest.hexdigest()


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]

    return tag


def _namespace_uri(tag: str) -> str | None:
    if tag.startswith("{") and "}" in tag:
        return tag[1:].split("}", 1)[0]

    return None


def _get_gml_id(element: etree._Element) -> str | None:
    for key, value in element.attrib.items():
        if _local_name(key) == "id":
            return value

    return None


def _safe_uid(
    *,
    gml_id: str | None,
    local_name: str,
    sequence_number: int,
) -> str:
    if gml_id:
        return gml_id

    return f"generated_{local_name}_{sequence_number:06d}"


def _relationship_type_from_context(context_name: str | None) -> str:
    if context_name is None:
        return "contains"

    return RELATIONSHIP_BY_CONTEXT.get(context_name, context_name)


def _role_for_child(local_name: str) -> str | None:
    known = ROLE_BY_CLASS.get(local_name)

    if known is not None:
        return known

    if not local_name:
        return None

    return local_name[:1].lower() + local_name[1:]


def _citygml_version_hint(root: etree._Element) -> str | None:
    namespaces = set(root.nsmap.values())

    for namespace in namespaces:
        if namespace is None:
            continue

        if "citygml/3.0" in namespace:
            return "3.0"

        if "citygml/2.0" in namespace:
            return "2.0"

        if "citygml/1.0" in namespace:
            return "1.0"

    return None


def import_citygml_semantics(
    pkg: USAPPackage,
    citygml_path: str | Path,
    *,
    uri: str | None = None,
    compute_hash: bool = True,
    graph_name: str = "citygml_import",
    also_usap_default: bool = True,
) -> CityGMLImportResult:
    """
    Import a narrow semantic subset from a CityGML file.

    Prototype scope:
        - register the CityGML file as a USAP source asset
        - create semantic classes for basic building concepts
        - create city objects for known CityGML elements with gml:id
        - create object relationships from XML nesting/property context
        - optionally mirror those relationships into usap_default

    Not implemented here:
        - CityGML geometry import
        - XLink resolution
        - full schema validation
        - ADE XML parsing
        - LoD geometry mapping
    """
    path = Path(citygml_path)

    if not path.exists():
        raise FileNotFoundError(f"CityGML file not found: {path}")

    parser = etree.XMLParser(
        huge_tree=True,
        remove_blank_text=True,
        recover=True,
    )

    tree = etree.parse(str(path), parser)
    root = tree.getroot()

    version_hint = _citygml_version_hint(root)
    content_hash = _sha256_file(path) if compute_hash else None

    asset_metadata = {
        "adapter": "citygml_adapter",
        "citygml_version_hint": version_hint,
        "note": (
            "Prototype import: semantic objects and relationships only; "
            "geometry is not imported."
        ),
    }

    with pkg.transaction():
        source_asset_id = pkg.register_asset(
            uri=uri if uri is not None else str(path),
            asset_kind="citygml",
            media_type="application/gml+xml",
            content_hash=content_hash,
            metadata_json=json.dumps(asset_metadata),
        )

        classes = seed_citygml_basic_classes(pkg)
        citygml_object_classes = set(classes.by_name.keys())

        imported_objects: list[ImportedCityObject] = []
        imported_relationships: list[ImportedRelationship] = []

        sequence_number = 0

        def ensure_class(local_name: str) -> int:
            if local_name in classes.by_name:
                return classes.by_name[local_name]

            class_uri = f"citygml:{local_name}"

            class_id = pkg.create_semantic_class(
                scheme="citygml",
                scheme_version=version_hint,
                class_uri=class_uri,
                local_name=local_name,
                parent_class_id=None,
                is_ade=False,
            )

            classes.by_name[local_name] = class_id
            classes.by_uri[class_uri] = class_id

            return class_id

        def walk(
            element: etree._Element,
            *,
            object_stack: list[ImportedCityObject],
            relationship_context: str | None,
        ) -> None:
            nonlocal sequence_number

            local_name = _local_name(element.tag)

            is_city_object = local_name in citygml_object_classes

            if is_city_object:
                sequence_number += 1

                gml_id = _get_gml_id(element)
                object_uid = _safe_uid(
                    gml_id=gml_id,
                    local_name=local_name,
                    sequence_number=sequence_number,
                )

                semantic_class_id = ensure_class(local_name)

                city_object_id = pkg.create_city_object(
                    object_uid=object_uid,
                    semantic_class_id=semantic_class_id,
                    gml_id=gml_id,
                    source_asset_id=source_asset_id,
                    source_object_id=gml_id,
                    object_status="accepted",
                    attributes_json=json.dumps(
                        {
                            "source": "citygml_adapter",
                            "citygml_local_name": local_name,
                            "citygml_namespace": _namespace_uri(element.tag),
                        }
                    ),
                )

                imported = ImportedCityObject(
                    city_object_id=city_object_id,
                    object_uid=object_uid,
                    gml_id=gml_id,
                    local_name=local_name,
                    semantic_class_id=semantic_class_id,
                )

                imported_objects.append(imported)

                if object_stack:
                    parent = object_stack[-1]

                    relationship_type = _relationship_type_from_context(
                        relationship_context
                    )
                    role = _role_for_child(local_name)

                    relationship_id = pkg.link_city_objects(
                        parent_city_object_id=parent.city_object_id,
                        child_city_object_id=city_object_id,
                        relationship_type=relationship_type,
                        role=role,
                        graph_name=graph_name,
                        source_asset_id=source_asset_id,
                        source_relation_id=(
                            f"{parent.object_uid}/{relationship_context or 'contains'}/"
                            f"{object_uid}"
                        ),
                        metadata_json=json.dumps(
                            {
                                "source": "citygml_adapter",
                                "relationship_context": relationship_context,
                            }
                        ),
                        rebuild_closure=False,
                    )

                    imported_relationships.append(
                        ImportedRelationship(
                            relationship_id=relationship_id,
                            parent_uid=parent.object_uid,
                            child_uid=object_uid,
                            relationship_type=relationship_type,
                            role=role,
                            graph_name=graph_name,
                        )
                    )

                    if also_usap_default:
                        default_relationship_id = pkg.link_city_objects(
                            parent_city_object_id=parent.city_object_id,
                            child_city_object_id=city_object_id,
                            relationship_type=relationship_type,
                            role=role,
                            graph_name="usap_default",
                            source_asset_id=source_asset_id,
                            source_relation_id=(
                                f"{parent.object_uid}/{relationship_context or 'contains'}/"
                                f"{object_uid}"
                            ),
                            metadata_json=json.dumps(
                                {
                                    "source": "citygml_adapter",
                                    "mirrored_from_graph": graph_name,
                                    "relationship_context": relationship_context,
                                }
                            ),
                            rebuild_closure=False,
                        )

                        imported_relationships.append(
                            ImportedRelationship(
                                relationship_id=default_relationship_id,
                                parent_uid=parent.object_uid,
                                child_uid=object_uid,
                                relationship_type=relationship_type,
                                role=role,
                                graph_name="usap_default",
                            )
                        )

                next_stack = object_stack + [imported]

            else:
                next_stack = object_stack

            next_context = local_name if not is_city_object else relationship_context

            for child in element:
                if not isinstance(child.tag, str):
                    continue

                walk(
                    child,
                    object_stack=next_stack,
                    relationship_context=next_context,
                )

        walk(
            root,
            object_stack=[],
            relationship_context=None,
        )

        pkg.rebuild_city_object_closure(graph_name=graph_name)

        if also_usap_default:
            pkg.rebuild_city_object_closure(graph_name="usap_default")

    return CityGMLImportResult(
        asset_id=source_asset_id,
        path=path,
        object_count=len(imported_objects),
        relationship_count=len(imported_relationships),
        imported_objects=imported_objects,
        imported_relationships=imported_relationships,
    )