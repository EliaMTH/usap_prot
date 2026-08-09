from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ._util import require_str
from .constants import (
    CITY_OBJECT_ROOT_LOCAL_NAME,
    CITYGML_NAMESPACE_MARKER,
    concept_uri,
)
from .core import USAPPackage
from .errors import USAPAmbiguityError, USAPError

_CITYGML_NAMESPACE_MARKER = CITYGML_NAMESPACE_MARKER


@dataclass(frozen=True)
class VocabularyResult:
    by_name: dict[str, int]
    by_uri: dict[str, int]


# Shipped inside the package (see pyproject package-data), not next to the
# repo checkout, so the default vocabularies load from a plain wheel install too.
_VOCABULARY_DIR = Path(__file__).resolve().parent / "data" / "vocabularies"

DEFAULT_ADE_VOCABULARY_PATH = _VOCABULARY_DIR / "usap_ade_prototype.json"


def seed_vocabulary_file(
    pkg: USAPPackage,
    path: str | Path,
) -> VocabularyResult:
    """
    Seed semantic classes from an external vocabulary JSON file.

    The JSON file is the accepted concept registry for one scheme/version.
    Concepts are idempotently inserted using class_uri (globally unique).

    This minimal format is intentionally a thin JSON form of a SKOS concept
    scheme; the fields map 1:1 to SKOS, so a vocabulary here can be generated
    from / exported to standard SKOS:
        class_uri   -> the concept IRI (a skos:Concept)
        local_name  -> skos:prefLabel
        parent_uri  -> skos:broader            (omit for top concepts)
        scheme / scheme_version -> the skos:ConceptScheme
        is_ade      -> marks an ADE/extension scheme vs a base one
    Richer per-concept metadata (definitions, units, applicable features, ...)
    is deliberately out of scope here: that is the application schema
    (e.g. the CityGML ADE XSD / SHACL), not the concept scheme.

    Minimal / no-ontology vocabularies: class_uri may be omitted, in which
    case it is derived as "{scheme}:{local_name}". parent_uri accepts either
    a class_uri or the local name of an already-registered concept (resolved
    within the same scheme first).

    Provenance (both optional, both nullable, neither populated by the shipped
    registries yet):
        source_namespace  the authority's namespace this concept comes from.
                          For a CityGML-derived concept, the XML namespace URI
                          — with local_name that is the QName the .gml uses.
                          May be given once at the top level as the default for
                          every concept in the file, and overridden per concept.
        concept_iri       the authority's own IRI for the concept, per concept,
                          when it publishes one.
    Recording these is what keeps a stable IRI *derivable* later without
    re-deciding anything, which is why the format carries them before any
    decision about IRIs has been made.

    Re-running this function on an updated file is additive and idempotent, and
    also **enriching**: a field that is still NULL on an existing concept is
    filled in, so provenance added to a registry later reaches packages that
    already exist by re-seeding rather than rebuilding. A field that already
    holds a *different* value raises instead of being silently rewritten.
    """
    vocab_path = Path(path)

    if not vocab_path.exists():
        raise FileNotFoundError(f"Vocabulary file not found: {vocab_path}")

    data = json.loads(vocab_path.read_text(encoding="utf-8"))

    scheme = require_str(data, "scheme", source=str(vocab_path))
    scheme_version = data.get("scheme_version")
    is_ade = bool(data.get("is_ade", False))

    # One namespace usually covers a whole registry, so it is a top-level
    # default; a concept may still override it (a CityGML registry spans
    # several module namespaces).
    default_source_namespace = data.get("source_namespace")

    concepts = data.get("concepts")

    if not isinstance(concepts, list):
        raise ValueError(
            f"Vocabulary {vocab_path} must contain a 'concepts' list."
        )

    by_name: dict[str, int] = {}
    by_uri: dict[str, int] = {}

    # First pass: create in the order given by the file.
    # Parent concepts should appear before children.
    for item in concepts:
        if not isinstance(item, dict):
            raise ValueError(f"Invalid concept entry in {vocab_path}: {item!r}")

        local_name = require_str(item, "local_name", source=str(vocab_path))

        if item.get("class_uri") is not None:
            class_uri = require_str(item, "class_uri", source=str(vocab_path))
        else:
            # Minimal-vocabulary convenience: a concept without an explicit
            # URI gets a scheme-derived one, so local/temporary schemes only
            # need names.
            class_uri = f"{scheme}:{local_name}"

        parent_class_id = None
        parent_uri = item.get("parent_uri")

        if parent_uri is not None:
            parent_class_id = _resolve_optional_parent(
                pkg,
                parent_uri,
                scheme=scheme,
                vocab_path=vocab_path,
                child_uri=class_uri,
            )

        # create_semantic_class is itself idempotent on class_uri (the globally
        # unique key), which is what makes vocabulary seeding idempotent, and
        # backfills NULL fields, which is what makes a re-seed enriching.
        class_id = pkg.create_semantic_class(
            scheme=scheme,
            scheme_version=scheme_version,
            class_uri=class_uri,
            local_name=local_name,
            parent_class_id=parent_class_id,
            is_ade=is_ade,
            source_namespace=item.get(
                "source_namespace",
                default_source_namespace,
            ),
            concept_iri=item.get("concept_iri"),
        )

        by_name[local_name] = class_id
        by_uri[class_uri] = class_id

    return VocabularyResult(by_name=by_name, by_uri=by_uri)


def seed_default_ade_vocabulary(pkg: USAPPackage) -> VocabularyResult:
    return seed_vocabulary_file(pkg, DEFAULT_ADE_VOCABULARY_PATH)


@dataclass(frozen=True)
class _SchemaElement:
    """One top-level xs:element declaration in a CityGML module schema."""

    local_name: str
    namespace: str
    parent: tuple[str, str] | None  # (namespace, local_name) of substitutionGroup
    abstract: bool

    @property
    def key(self) -> tuple[str, str]:
        return (self.namespace, self.local_name)

    @property
    def class_uri(self) -> str:
        return f"{self.namespace}#{self.local_name}"


def load_citygml_schema(
    pkg: USAPPackage,
    path: str | Path,
    *,
    scheme: str = "citygml",
    scheme_version: str | None = None,
) -> VocabularyResult:
    """
    Register CityGML concepts from the OGC XSD, hierarchy included.

    This replaces the hand-written registry that used to ship with USAP. Every
    fact here is read from the normative schema instead of asserted by us: the
    element name, the module's real targetNamespace, whether the class is
    abstract, and — the part no OWL rendering of CityGML carries — the parent,
    from ``substitutionGroup``.

    ``path`` is a single .xsd or a directory searched recursively; pass the
    unpacked OGC distribution (schemas.opengis.net/citygml/citygml-3_0_0.zip).
    Nothing is vendored: the caller supplies the schemas.

    Identity:
        class_uri         "{targetNamespace}#{local_name}"
        source_namespace  the targetNamespace, so source_namespace + local_name
                          is exactly the QName a .gml uses — the only exact
                          join key back to the document.

    Concepts are created parents-first so the closure is correct in one pass.
    A substitutionGroup pointing outside CityGML (gml:AbstractFeature) ends the
    chain and leaves parent_class_id NULL. Re-running is idempotent and
    enriching, like seed_vocabulary_file: create_semantic_class keys on
    class_uri and backfills what is still NULL.

    Not every registered concept is a city object — CityObjectRelation, Role,
    CityModel, Address and the appearance/versioning classes are real CityGML
    classes that no .gml instantiates as a city object. They are registered
    (they exist) but excluded by is_city_object_class, which is what the
    importer filters on.
    """
    from lxml import etree

    root_path = Path(path)

    if not root_path.exists():
        raise FileNotFoundError(f"CityGML schema path not found: {root_path}")

    if root_path.is_dir():
        schema_files = sorted(root_path.rglob("*.xsd"))
    else:
        schema_files = [root_path]

    if not schema_files:
        raise USAPError(f"No .xsd files found under {root_path}.")

    elements: dict[tuple[str, str], _SchemaElement] = {}

    for schema_file in schema_files:
        elements.update(_read_schema_elements(schema_file, etree))

    if not elements:
        raise USAPError(
            f"No CityGML element declarations found under {root_path}. "
            f"Expected schemas in a *{_CITYGML_NAMESPACE_MARKER}* namespace; "
            "this looks like a different schema set."
        )

    by_name: dict[str, int] = {}
    by_uri: dict[str, int] = {}
    class_ids: dict[tuple[str, str], int] = {}

    for element in _parents_first(elements):
        parent_class_id = None

        if element.parent is not None:
            # None when the chain leaves CityGML (gml:AbstractFeature): the
            # concept is a root here, which is correct — USAP does not mirror
            # the GML object model.
            parent_class_id = class_ids.get(element.parent)

        class_id = pkg.create_semantic_class(
            scheme=scheme,
            scheme_version=scheme_version,
            class_uri=element.class_uri,
            local_name=element.local_name,
            parent_class_id=parent_class_id,
            is_ade=False,
            source_namespace=element.namespace,
            metadata_json=json.dumps({"abstract": element.abstract}),
        )

        class_ids[element.key] = class_id
        by_uri[element.class_uri] = class_id

        # Local names are unique across CityGML 3.0 modules, but do not rely on
        # it: first writer wins here, and callers that need certainty use the
        # class_uri. resolve_semantic_class raises on a genuine ambiguity.
        by_name.setdefault(element.local_name, class_id)

    return VocabularyResult(by_name=by_name, by_uri=by_uri)


def _read_schema_elements(
    schema_file: Path,
    etree,
) -> dict[tuple[str, str], _SchemaElement]:
    """
    Top-level element declarations of one .xsd, keyed by (namespace, name).

    Only CityGML namespaces are returned: the OGC distribution also carries
    xAL, whose elements are addresses rather than city concepts.
    """
    xs = "{http://www.w3.org/2001/XMLSchema}"

    try:
        tree = etree.parse(str(schema_file))
    except etree.XMLSyntaxError as exc:
        raise USAPError(f"Malformed schema file {schema_file}: {exc}") from exc

    root = tree.getroot()
    namespace = root.get("targetNamespace")

    if namespace is None or _CITYGML_NAMESPACE_MARKER not in namespace:
        return {}

    found: dict[tuple[str, str], _SchemaElement] = {}

    # Direct children only. A nested <element> inside a complexType is a
    # property declaration, not a class.
    for node in root:
        if node.tag != xs + "element":
            continue

        local_name = node.get("name")

        if not local_name:
            continue

        found[(namespace, local_name)] = _SchemaElement(
            local_name=local_name,
            namespace=namespace,
            parent=_resolve_qname(node.get("substitutionGroup"), root.nsmap),
            abstract=node.get("abstract") == "true",
        )

    return found


def _resolve_qname(
    qname: str | None,
    nsmap: dict[str | None, str],
) -> tuple[str, str] | None:
    """
    Expand a 'prefix:Local' substitutionGroup against the schema's own nsmap.

    The prefix has to be resolved rather than stripped: core:AbstractFeature
    substitutes gml:AbstractFeature, and on local names alone those two are
    the same string — a self-referencing parent that would loop.
    """
    if not qname:
        return None

    prefix, _, local_name = qname.rpartition(":")
    namespace = nsmap.get(prefix or None)

    if namespace is None or _CITYGML_NAMESPACE_MARKER not in namespace:
        return None

    return (namespace, local_name)


def _parents_first(
    elements: dict[tuple[str, str], _SchemaElement],
) -> list[_SchemaElement]:
    """
    Order elements so every parent precedes its children.

    create_semantic_class can backfill a parent that arrives late, but only
    inserting in order gives a correct closure without a second pass. Sorted by
    substitution depth, then by key, so the output is stable across runs.
    """

    def depth(element: _SchemaElement) -> int:
        seen: set[tuple[str, str]] = set()
        steps = 0
        current = element

        while current.parent is not None and current.key not in seen:
            seen.add(current.key)
            ancestor = elements.get(current.parent)

            if ancestor is None:
                break

            steps += 1
            current = ancestor

        return steps

    return sorted(elements.values(), key=lambda e: (depth(e), e.key))


def city_object_classes(pkg: USAPPackage) -> dict[tuple[str, str], int]:
    """
    Every registered concept that is instantiable as a city object.

    Keyed by ``(source_namespace, local_name)`` — the QName a .gml actually
    writes — so a document element resolves exactly, without relying on local
    names being unique across modules or across an ADE.

    This is what ``import_citygml_semantics`` reads instead of seeding its own
    vocabulary. Concepts with no source_namespace are skipped: they cannot be
    matched against a document element with certainty, and guessing is how the
    old adapter mapped a 2.0 Building onto a 3.0 class URI.
    """
    rows = pkg.conn.execute(
        """
        SELECT DISTINCT
            descendant.semantic_class_id,
            descendant.source_namespace,
            descendant.local_name
        FROM usap_semantic_class_closure AS c
        JOIN usap_semantic_class AS ancestor
            ON ancestor.semantic_class_id = c.ancestor_class_id
        JOIN usap_semantic_class AS descendant
            ON descendant.semantic_class_id = c.descendant_class_id
        WHERE ancestor.local_name = ?
          AND descendant.source_namespace IS NOT NULL
        """,
        (CITY_OBJECT_ROOT_LOCAL_NAME,),
    ).fetchall()

    return {
        (row["source_namespace"], row["local_name"]): int(row["semantic_class_id"])
        for row in rows
    }


def is_city_object_class(pkg: USAPPackage, semantic_class_id: int) -> bool:
    """
    True when a concept reaches AbstractCityObject by substitution.

    Answered from usap_semantic_class_closure, so it costs one indexed lookup
    and stays correct as concepts are added. This is the filter the CityGML
    importer applies: a class outside this branch (CityObjectRelation, Role,
    CityModel, Address, AbstractPointCloud, appearance and versioning classes)
    is a genuine CityGML class but never a city object, and instantiating one
    would put a relation object into usap_city_object.
    """
    row = pkg.conn.execute(
        """
        SELECT 1
        FROM usap_semantic_class_closure AS c
        JOIN usap_semantic_class AS ancestor
            ON ancestor.semantic_class_id = c.ancestor_class_id
        WHERE c.descendant_class_id = ?
          AND ancestor.local_name = ?
        LIMIT 1
        """,
        (semantic_class_id, CITY_OBJECT_ROOT_LOCAL_NAME),
    ).fetchone()

    return row is not None


def _resolve_optional_parent(
    pkg: USAPPackage,
    parent_ref: str,
    *,
    scheme: str,
    vocab_path: Path,
    child_uri: str,
) -> int:
    try:
        # First try same-scheme resolution. This is useful for local names.
        return pkg.resolve_semantic_class(
            parent_ref,
            scheme=scheme,
        )
    except USAPAmbiguityError:
        # Ambiguity is definitive (and its message lists the options);
        # do not mask it as "not registered".
        raise
    except USAPError:
        pass

    try:
        # Then try global resolution. This allows ADE/custom concepts
        # to inherit from CityGML class URIs.
        return pkg.resolve_semantic_class(parent_ref)
    except USAPAmbiguityError:
        raise
    except USAPError as exc:
        raise ValueError(
            f"{vocab_path}: parent concept {parent_ref!r} for "
            f"{child_uri!r} is not registered yet. Put parent concepts "
            "before children, and load parent vocabularies before child "
            "vocabularies."
        ) from exc

# ---------------------------------------------------------------------------
# Ontology loading (RDF/XML)
# ---------------------------------------------------------------------------

# USAP's own predicate, for the one fact no CityGML artifact carries: whether a
# link type means "part of". A URN because it needs to be globally unique
# without USAP owning a domain — the same reasoning as usap_profile.package_iri.
#
# In RDF/XML:
#     xmlns:usap="urn:usap:"
#     <owl:ObjectProperty rdf:about="http://www.opengis.net/citygml/3.0#boundary">
#         <usap:category>containment</usap:category>
#     </owl:ObjectProperty>
USAP_ONTOLOGY_NAMESPACE = "urn:usap:"

_RDF = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"
_RDFS = "{http://www.w3.org/2000/01/rdf-schema#}"
_OWL = "{http://www.w3.org/2002/07/owl#}"
_USAP = f"{{{USAP_ONTOLOGY_NAMESPACE}}}"


@dataclass(frozen=True)
class OntologyResult:
    relationship_types: dict[str, int]
    concepts: dict[str, int]
    categorised: int
    imports: list[str]


def _split_iri(iri: str) -> tuple[str, str] | None:
    """
    Split an IRI into (namespace, local name).

    Two conventions are recognised. A plain fragment or last path segment is
    the usual one. The other is ISO 19150-2's ``Class.property`` form, which
    is how CityGML's published OWL renderings name properties — there,
    ``core#AbstractSpace.boundary`` is the property ``boundary``, and keeping
    the class prefix would leave a type name no document ever writes.
    """
    for separator in ("#", "/"):
        namespace, found, local_name = iri.rpartition(separator)

        if found and local_name:
            owner, dot, property_name = local_name.partition(".")

            if (
                dot
                and owner[:1].isupper()
                and property_name[:1].islower()
            ):
                local_name = property_name

            return namespace, local_name

    return None


def load_ontology(
    pkg: USAPPackage,
    path: str | Path,
    *,
    scheme: str = "ontology",
    scheme_version: str | None = None,
) -> OntologyResult:
    """
    Read link types, their categories, and ADE concepts from an RDF/XML file.

    This is how a package is initialised on an ontology: supply a different
    one, or extend the one you have, and the package's link vocabulary changes
    with it. USAP asserts nothing here itself.

    What it reads:

        owl:ObjectProperty   a link type. Identity is the IRI's namespace plus
                             its local name, so it matches what an import
                             registers from the document.
        usap:category        the one fact no CityGML artifact carries: whether
                             that link means part-of. See
                             USAP_ONTOLOGY_NAMESPACE for the predicate.
        owl:Class            a concept, with rdfs:subClassOf as its parent when
                             that parent is also declared here. Meant for ADE
                             classes; CityGML's own come from the XSD, which is
                             the only artifact carrying their hierarchy.

    **RDF/XML only.** Parsing is deliberately narrow — flat declarations, read
    with the lxml USAP already depends on, so ontology support costs no extra
    install. A Turtle file is refused with a clear message rather than
    half-read. Anything the reader does not recognise is left alone: this adds
    facts, it never removes or overrides them.

    ``owl:imports`` is **not** followed; the imported IRIs are returned so a
    caller can load them too if it wants. Following them would mean fetching
    over the network from inside a package build.

    A category only takes effect if the property's IRI namespace matches the
    namespace the source document uses — that pair is the type's identity. If
    they differ, the category lands on a type nothing uses, and the type the
    import did register stays unclassified and is reported as
    UNCLASSIFIED_RELATIONSHIP_TYPE.
    """
    from lxml import etree

    ontology_path = Path(path)

    if not ontology_path.exists():
        raise FileNotFoundError(f"Ontology file not found: {ontology_path}")

    try:
        root = etree.parse(str(ontology_path)).getroot()
    except etree.XMLSyntaxError as exc:
        raise USAPError(
            f"{ontology_path} is not readable as RDF/XML: {exc}. The built-in "
            "reader handles RDF/XML only — convert a Turtle or N-Triples file "
            "first (for example with rdflib or Protégé)."
        ) from exc

    if root.tag != f"{_RDF}RDF":
        raise USAPError(
            f"{ontology_path} is XML but not RDF/XML: the root element is "
            f"{root.tag!r}, expected rdf:RDF."
        )

    imports = [
        value
        for node in root.iter(f"{_OWL}imports")
        if (value := node.get(f"{_RDF}resource"))
    ]

    relationship_types: dict[str, int] = {}
    concepts: dict[str, int] = {}
    categorised = 0

    with pkg.transaction():
        for node in root.iter(f"{_OWL}ObjectProperty"):
            iri = node.get(f"{_RDF}about")

            if not iri:
                # A property defined inline with no IRI cannot be referred to
                # from anywhere else, so it can never match a stored edge.
                continue

            split = _split_iri(iri)

            if split is None:
                continue

            code_space, local_name = split
            category_node = node.find(f"{_USAP}category")
            category = None

            if category_node is not None and category_node.text:
                category = category_node.text.strip() or None

            relationship_types[iri] = pkg.register_relationship_type(
                local_name,
                code_space=code_space,
                category=category,
            )

            if category is not None:
                categorised += 1

        # Two passes over classes so a parent declared after its child still
        # resolves, mirroring load_citygml_schema's parents-first ordering.
        class_nodes = [
            node
            for node in root.iter(f"{_OWL}Class")
            if node.get(f"{_RDF}about")
        ]

        for node in class_nodes:
            iri = node.get(f"{_RDF}about")
            split = _split_iri(iri)

            if split is None:
                continue

            source_namespace, local_name = split

            concepts[iri] = pkg.create_semantic_class(
                scheme=scheme,
                scheme_version=scheme_version,
                class_uri=iri,
                local_name=local_name,
                source_namespace=source_namespace,
                concept_iri=iri,
            )

        for node in class_nodes:
            iri = node.get(f"{_RDF}about")

            if iri not in concepts:
                continue

            for parent_node in node.findall(f"{_RDFS}subClassOf"):
                parent_iri = parent_node.get(f"{_RDF}resource")

                # Only a named parent declared in this same file. A parent
                # elsewhere (an anonymous owl:Restriction, or a class from an
                # import) is not a hierarchy USAP can resolve, and guessing
                # one would be worse than leaving the concept a root.
                if parent_iri is None or parent_iri not in concepts:
                    continue

                pkg.create_semantic_class(
                    scheme=scheme,
                    scheme_version=scheme_version,
                    class_uri=iri,
                    local_name=_split_iri(iri)[1],
                    parent_class_id=concepts[parent_iri],
                )

                break

    return OntologyResult(
        relationship_types=relationship_types,
        concepts=concepts,
        categorised=categorised,
        imports=imports,
    )
