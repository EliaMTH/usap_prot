from __future__ import annotations

import json
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .._util import canonical_hash
from ..constants import CITYGML_NAMESPACE_MARKER, DEFAULT_GRAPH_NAME
from ..core import USAPPackage
from ..domain_vocab import city_object_classes
from ..errors import USAPError

if TYPE_CHECKING:
    from lxml import etree


# No rename table, and no role table. The link type stored is the CityGML
# property the edge came through, verbatim, paired with that property's
# namespace — `boundary` stays `boundary`. What used to sit here mapped a
# handful of property names onto CityGML 2.0 tokens ('boundary' ->
# 'boundedBy'), destroying the identity of the property while ~30 others
# passed through raw, so a package held two spellings of one idea and neither
# could be resolved back to a definition. `role` likewise is read only from
# grp:Role, never derived from the target's class.

_XLINK_HREF = "{http://www.w3.org/1999/xlink}href"

# The two constructs that objectify a relation: the property points at a
# relation object, which carries the qualifier and then points at the real
# target. Handled by descending one level rather than by a separate code path.
_CITY_OBJECT_RELATION = "CityObjectRelation"
_GROUP_ROLE = "Role"


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
    from_uid: str
    to_uid: str | None
    to_external_uri: str | None
    relationship_type: str
    code_space: str | None
    role: str | None
    graph_name: str


@dataclass(frozen=True)
class UnresolvedTarget:
    """An xlink whose target is not in this document."""

    from_uid: str
    relationship_type: str
    href: str


@dataclass
class CityGMLImportResult:
    asset_id: int
    path: Path
    object_count: int
    relationship_count: int
    imported_objects: list[ImportedCityObject] = field(default_factory=list)
    imported_relationships: list[ImportedRelationship] = field(default_factory=list)

    # An xlink that leaves the document is kept as an edge with
    # to_external_uri set; this lists them so a caller can act on it.
    unresolved_targets: list[UnresolvedTarget] = field(default_factory=list)

    # Property elements that looked like relationships but were not accepted,
    # e.g. an appearance href. Reported rather than silently dropped.
    skipped_references: list[str] = field(default_factory=list)

    warnings: list[str] = field(default_factory=list)


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]

    return tag


def _namespace_uri(tag: str) -> str | None:
    if tag.startswith("{") and "}" in tag:
        return tag[1:].split("}", 1)[0]

    return None


# Every CityGML module namespace, in every version, is under this host path:
#   http://www.opengis.net/citygml/2.0
#   http://www.opengis.net/citygml/building/3.0   (and relief/, vegetation/, ...)
# Matching on the local name alone would import ANY XML that happens to use
# the word "Building" as a CityGML building — plausible rows stating something
# false, which is worse than importing nothing.
_CITYGML_NAMESPACE_MARKER = CITYGML_NAMESPACE_MARKER

# gml:id lives in the GML namespace (3.1.1 uses .../gml, 3.2 .../gml/3.2).
# An unqualified id= attribute belongs to some other vocabulary and must not
# be mistaken for a stable CityGML object identity.
_GML_NAMESPACE_MARKER = "opengis.net/gml"


def _is_citygml_namespace(namespace: str | None) -> bool:
    return namespace is not None and _CITYGML_NAMESPACE_MARKER in namespace


def _get_gml_id(element: etree._Element) -> str | None:
    for key, value in element.attrib.items():
        if _local_name(key) != "id":
            continue

        namespace = _namespace_uri(key)

        if namespace is not None and _GML_NAMESPACE_MARKER in namespace:
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


@dataclass
class _ObjectIndex:
    """What pass 1 learned, and pass 2 resolves against."""

    by_element: dict[Any, ImportedCityObject] = field(default_factory=dict)
    by_gml_id: dict[str, ImportedCityObject] = field(default_factory=dict)
    known_types: set[tuple[str, str | None]] = field(default_factory=set)


@dataclass(frozen=True)
class _EdgeTarget:
    kind: str  # 'inline' | 'xlink' | 'objectified'
    city_object: ImportedCityObject | None
    external_uri: str | None


@dataclass(frozen=True)
class _ResolvedEdge:
    target: _EdgeTarget
    type_local_name: str
    type_code_space: str | None
    role: str | None
    property_path: str
    qualifier: dict[str, str]


def _is_relation_object(element: etree._Element) -> bool:
    """
    True for CityGML's two objectified relations.

    Neither is a city object — both substitute gml:AbstractGML — and both are
    consumed whole by _resolve_edges, qualifier and inner target together.
    """
    return _local_name(element.tag) in (
        _CITY_OBJECT_RELATION,
        _GROUP_ROLE,
    ) and _is_citygml_namespace(_namespace_uri(element.tag))


def _href_target(
    property_element: etree._Element,
    index: _ObjectIndex,
) -> tuple[ImportedCityObject | None, str | None]:
    """
    Resolve an xlink:href to a city object, or report it as external.

    A local '#gml_id' that matches nothing in this document is treated the
    same as a cross-document URL: the link is real, its target simply is not
    here. Returning it rather than dropping it is the whole point — dropping
    is how an xlink-serialized file used to import as unrelated roots.
    """
    href = property_element.get(_XLINK_HREF)

    if not href:
        return None, None

    if href.startswith("#"):
        found = index.by_gml_id.get(href[1:])

        if found is not None:
            return found, None

    return None, href


def _resolve_edges(
    property_element: etree._Element,
    *,
    index: _ObjectIndex,
    path_prefix: str = "",
) -> tuple[list[_ResolvedEdge], list[str]]:
    """
    Every city-object edge a single property element asserts.

    One function for the three shapes a CityGML relationship takes, because
    they differ only in where the target is written:

      inline       the target's definition sits inside the property element
      xlink        the property carries xlink:href and the target is elsewhere
      objectified  the property holds a CityObjectRelation or a Role, which
                   carries the qualifier and then points at the target itself

    Returns (edges, skipped). `skipped` names references that looked like
    relationships but were not accepted.

    Empty list means "not a city-object relationship property", which is the
    common case: most elements under a city object are geometry or attributes.
    """
    local_name = _local_name(property_element.tag)
    namespace = _namespace_uri(property_element.tag)

    if not _is_citygml_namespace(namespace):
        return [], []

    property_path = f"{path_prefix}{local_name}"
    edges: list[_ResolvedEdge] = []
    skipped: list[str] = []

    # (c) objectified — descend one level, take the qualifier, then recurse on
    # the inner property so the target resolution is not duplicated.
    for child in property_element:
        if not isinstance(child.tag, str):
            continue

        child_name = _local_name(child.tag)

        if not _is_citygml_namespace(_namespace_uri(child.tag)):
            continue

        if child_name not in (_CITY_OBJECT_RELATION, _GROUP_ROLE):
            continue

        qualifier: dict[str, str] = {"property": property_path}
        role: str | None = None
        relation_type: str | None = None
        relation_code_space: str | None = None

        for field_element in child:
            if not isinstance(field_element.tag, str):
                continue

            field_name = _local_name(field_element.tag)

            if field_name == "relationType":
                # gml:CodeType — the value plus the register it comes from.
                # This, not the carrier property, is the edge's real type:
                # 'relatedTo' is generic plumbing, 'adjacentTo' is the claim.
                relation_type = (field_element.text or "").strip() or None
                relation_code_space = field_element.get("codeSpace")
            elif field_name == "role":
                # The single role qualifier in all of CityGML 3.0.
                role = (field_element.text or "").strip() or None

        for inner in child:
            if not isinstance(inner.tag, str):
                continue

            inner_edges, inner_skipped = _resolve_edges(
                inner,
                index=index,
                path_prefix=f"{property_path}/{child_name}/",
            )
            skipped.extend(inner_skipped)

            for edge in inner_edges:
                edges.append(
                    _ResolvedEdge(
                        target=_EdgeTarget(
                            kind="objectified",
                            city_object=edge.target.city_object,
                            external_uri=edge.target.external_uri,
                        ),
                        type_local_name=relation_type or edge.type_local_name,
                        type_code_space=(
                            relation_code_space
                            if relation_type
                            else edge.type_code_space
                        ),
                        role=role,
                        property_path=edge.property_path,
                        qualifier=qualifier,
                    )
                )

        if edges or skipped:
            return edges, skipped

    # (a) inline — a nested element that pass 1 already registered as a city
    # object. The target is *proven*, so an unfamiliar property is fine.
    for child in property_element:
        if not isinstance(child.tag, str):
            continue

        found = index.by_element.get(child)

        if found is not None:
            edges.append(
                _ResolvedEdge(
                    target=_EdgeTarget("inline", found, None),
                    type_local_name=local_name,
                    type_code_space=namespace,
                    role=None,
                    property_path=property_path,
                    qualifier={},
                )
            )

    if edges:
        return edges, skipped

    # (b) xlink — nothing proves the target is a city object, so the property
    # itself has to be one USAP has already met. The appearance module is
    # under opengis.net/citygml too, so without this gate an <app:target
    # xlink:href="#roof"> mints a bogus city-object edge and a bogus type —
    # the exact failure class the deleted rename table caused.
    href = property_element.get(_XLINK_HREF)

    if not href:
        return [], skipped

    found, external = _href_target(property_element, index)

    if found is None and (local_name, namespace) not in index.known_types:
        skipped.append(f"{property_path} -> {href}")

        return [], skipped

    edges.append(
        _ResolvedEdge(
            target=_EdgeTarget("xlink", found, external),
            type_local_name=local_name,
            type_code_space=namespace,
            role=None,
            property_path=property_path,
            qualifier={},
        )
    )

    return edges, skipped


def import_citygml_semantics(
    pkg: USAPPackage,
    citygml_path: str | Path,
    *,
    uri: str | None = None,
    compute_hash: bool = True,
    graph_name: str = DEFAULT_GRAPH_NAME,
) -> CityGMLImportResult:
    """
    Import city-object identities and their relationships from a CityGML file.

    Two passes, because a relationship is not the same thing as XML nesting.
    Nesting is one of two ways to *serialize* a relationship: the relationship
    is the named property element, and its target may sit inside that element
    or behind an xlink:href pointing anywhere in — or out of — the document.
    Most CityGML properties accept either form, so a single recursive walk
    that only looks at nesting silently loses every edge in a file written by
    reference, and cannot see the six properties that are xlink-only at all
    (generalizesTo, relatedTo, predecessor, successor, groupMember, parent).

        pass 1  create every city object and index it by element and gml:id
        pass 2  for each relationship property, resolve its target against
                that index and emit one edge

    Concepts are a precondition: USAP ships no CityGML vocabulary, so load one
    first (load_citygml_schema, or an ontology). Link types are not — an
    unseen property registers itself, and is reported as unclassified until
    something says what category it belongs to.

    Only elements in a CityGML namespace are imported, at any version. The
    detected version is recorded on the asset; concepts and link types both
    carry the namespace the document actually used, so a 2.0 document and a
    3.0 one stay distinguishable.

    Not implemented here:
        - CityGML geometry import
        - full schema validation
        - LoD geometry mapping
    """
    from lxml import etree

    path = Path(citygml_path)

    if not path.exists():
        raise FileNotFoundError(f"CityGML file not found: {path}")

    parser = etree.XMLParser(
        huge_tree=True,
        remove_blank_text=True,
    )

    try:
        tree = etree.parse(str(path), parser)
    except etree.XMLSyntaxError as exc:
        raise USAPError(f"Malformed CityGML file: {path}: {exc}") from exc

    root = tree.getroot()

    # Refuse a document that declares no CityGML namespace at all rather than
    # importing zero objects from it: silence would look like "this file has
    # no buildings" when it actually means "this is not CityGML".
    if not any(
        _is_citygml_namespace(namespace)
        for namespace in root.nsmap.values()
    ):
        declared = sorted(
            namespace for namespace in root.nsmap.values() if namespace
        )

        raise USAPError(
            f"Not a CityGML document: {path}. No CityGML namespace "
            f"(*{_CITYGML_NAMESPACE_MARKER}*) is declared; found: "
            f"{declared or 'no namespaces at all'}."
        )

    version_hint = _citygml_version_hint(root)
    content_hash = canonical_hash(path) if compute_hash else None

    asset_metadata = {
        "adapter": "citygml_adapter",
        "citygml_version_hint": version_hint,
        "note": (
            "Semantic objects and relationships only; geometry is not "
            "imported."
        ),
    }

    import_warnings: list[str] = []

    with pkg.transaction():
        source_asset_id = pkg.register_asset(
            uri=uri if uri is not None else str(path),
            asset_kind="citygml",
            media_type="application/gml+xml",
            content_hash=content_hash,
            metadata_json=json.dumps(asset_metadata),
        )

        # The concept registry is a precondition, not something this import
        # creates. USAP no longer ships a CityGML vocabulary to fall back on,
        # and inventing one here is what let a 2.0 Building be filed under a
        # 3.0 class URI. Keyed by the QName the document actually writes.
        citygml_object_classes = city_object_classes(pkg)

        if not citygml_object_classes:
            raise USAPError(
                f"No city-object concepts are registered, so nothing in "
                f"{path} could be classified. Load a concept source first, "
                "e.g. load_citygml_schema(pkg, '<path to the OGC CityGML 3.0 "
                "schemas>')."
            )

        index = _ObjectIndex(
            known_types={
                (t["local_name"], t["code_space"])
                for t in pkg.list_relationship_types()
            }
        )

        imported_objects = _collect_objects(
            root,
            pkg=pkg,
            index=index,
            citygml_object_classes=citygml_object_classes,
            source_asset_id=source_asset_id,
            import_warnings=import_warnings,
        )

        imported_relationships, unresolved, skipped = _emit_edges(
            root,
            pkg=pkg,
            index=index,
            source_asset_id=source_asset_id,
            graph_name=graph_name,
        )

    if unresolved:
        import_warnings.append(
            f"{len(unresolved)} relationship target(s) are outside {path.name}; "
            "stored with to_external_uri and reported as "
            "UNRESOLVED_RELATIONSHIP_TARGET."
        )

    for message in import_warnings:
        warnings.warn(message, UserWarning, stacklevel=2)

    return CityGMLImportResult(
        asset_id=source_asset_id,
        path=path,
        object_count=len(imported_objects),
        relationship_count=len(imported_relationships),
        imported_objects=imported_objects,
        imported_relationships=imported_relationships,
        unresolved_targets=unresolved,
        skipped_references=skipped,
        warnings=import_warnings,
    )


def _collect_objects(
    root: etree._Element,
    *,
    pkg: USAPPackage,
    index: _ObjectIndex,
    citygml_object_classes: dict[tuple[str, str], int],
    source_asset_id: int,
    import_warnings: list[str],
) -> list[ImportedCityObject]:
    """
    Pass 1: create every city object, and index it for pass 2.

    Indexed twice: by element identity, so an inline target is found without
    re-deriving anything, and by gml:id, so an xlink can be resolved no matter
    where in the document its target is declared — including before it.
    """
    imported: list[ImportedCityObject] = []
    sequence_number = 0

    for element in root.iter():
        if not isinstance(element.tag, str):
            continue

        local_name = _local_name(element.tag)
        namespace = _namespace_uri(element.tag)

        # Exact QName match against the registry. Matching on the local name
        # alone would let a same-named element from another module — or
        # another CityGML version — adopt the wrong class.
        semantic_class_id = citygml_object_classes.get((namespace, local_name))

        if semantic_class_id is None or not _is_citygml_namespace(namespace):
            continue

        sequence_number += 1

        gml_id = _get_gml_id(element)
        object_uid = _safe_uid(
            gml_id=gml_id,
            local_name=local_name,
            sequence_number=sequence_number,
        )

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
                    "citygml_namespace": namespace,
                }
            ),
        )

        entry = ImportedCityObject(
            city_object_id=city_object_id,
            object_uid=object_uid,
            gml_id=gml_id,
            local_name=local_name,
            semantic_class_id=semantic_class_id,
        )

        imported.append(entry)

        # Keyed on the element itself, not id(): lxml builds proxy objects on
        # demand, so an int id neither keeps one alive nor stays unique. Using
        # the element as the key holds a reference and pins its identity for
        # the whole import.
        index.by_element[element] = entry

        if gml_id:
            if gml_id in index.by_gml_id:
                import_warnings.append(
                    f"Duplicate gml:id {gml_id!r}; the first occurrence is "
                    "the one xlinks resolve to."
                )
            else:
                index.by_gml_id[gml_id] = entry

    return imported


def _emit_edges(
    root: etree._Element,
    *,
    pkg: USAPPackage,
    index: _ObjectIndex,
    source_asset_id: int,
    graph_name: str,
) -> tuple[list[ImportedRelationship], list[UnresolvedTarget], list[str]]:
    """
    Pass 2: one edge per relationship property, whatever its serialization.

    An edge's source is the nearest enclosing city object, which is what a
    property element belonging to that object means. Its target comes from
    _resolve_edges, which handles inline, xlink and objectified alike.
    """
    relationships: list[ImportedRelationship] = []
    unresolved: list[UnresolvedTarget] = []
    skipped: list[str] = []

    def walk(element: etree._Element, owner: ImportedCityObject | None) -> None:
        entry = index.by_element.get(element)

        if entry is not None:
            # A city object: it becomes the owner for the properties below it.
            for child in element:
                if isinstance(child.tag, str):
                    walk(child, entry)

            return

        if owner is not None:
            edges, property_skipped = _resolve_edges(element, index=index)
            skipped.extend(property_skipped)

            for edge in edges:
                relationships.append(
                    _write_edge(
                        pkg=pkg,
                        owner=owner,
                        edge=edge,
                        source_asset_id=source_asset_id,
                        graph_name=graph_name,
                        unresolved=unresolved,
                    )
                )

        # Descend, with one exception. An inline target is itself a city
        # object, so the branch above picks it up as the new owner rather than
        # re-emitting it, and a wall's openings are only seen because the walk
        # continues through it. A relation object (CityObjectRelation, Role)
        # is different: _resolve_edges already consumed its inner property, so
        # re-entering it would emit the same edge a second time.
        for child in element:
            if isinstance(child.tag, str) and not _is_relation_object(child):
                walk(child, owner)

    walk(root, None)

    return relationships, unresolved, skipped


def _write_edge(
    *,
    pkg: USAPPackage,
    owner: ImportedCityObject,
    edge: _ResolvedEdge,
    source_asset_id: int,
    graph_name: str,
    unresolved: list[UnresolvedTarget],
) -> ImportedRelationship:
    target_uid = (
        edge.target.city_object.object_uid
        if edge.target.city_object is not None
        else None
    )

    if edge.target.external_uri is not None:
        unresolved.append(
            UnresolvedTarget(
                from_uid=owner.object_uid,
                relationship_type=edge.type_local_name,
                href=edge.target.external_uri,
            )
        )

    metadata = {
        "source": "citygml_adapter",
        "property_path": edge.property_path,
        "target_kind": edge.target.kind,
    }
    metadata.update(edge.qualifier)

    relationship_id = pkg.link_city_objects(
        owner.city_object_id,
        (
            edge.target.city_object.city_object_id
            if edge.target.city_object is not None
            else None
        ),
        edge.type_local_name,
        to_external_uri=edge.target.external_uri,
        # The property's own namespace, so 'boundedBy' from CityGML 2.0 and
        # from an ADE stay distinct rows in the type registry.
        code_space=edge.type_code_space,
        role=edge.role,
        graph_name=graph_name,
        source_asset_id=source_asset_id,
        source_relation_id=(
            f"{owner.object_uid}/{edge.property_path}/"
            f"{target_uid or edge.target.external_uri}"
        ),
        metadata_json=json.dumps(metadata),
    )

    return ImportedRelationship(
        relationship_id=relationship_id,
        from_uid=owner.object_uid,
        to_uid=target_uid,
        to_external_uri=edge.target.external_uri,
        relationship_type=edge.type_local_name,
        code_space=edge.type_code_space,
        role=edge.role,
        graph_name=graph_name,
    )
