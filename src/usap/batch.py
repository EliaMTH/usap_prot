from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._util import _check_keys, require_str
from .core import USAPPackage
from .errors import USAPAmbiguityError, USAPError
from .domain_vocab import seed_vocabulary_file
from .constants import normalize_element_kind


# Every key a batch payload understands. A batch is generated, not hand-typed,
# so a key the importer does not read is a key whose intent is dropped on every
# entry of every run -- silently, since nothing downstream reads it either. The
# same check project_builder applies to configs; see _util._check_keys.
_BATCH_KEYS = frozenset({
    "vocabularies", "annotations", "create_missing_city_objects",
})
_ITEM_KEYS = frozenset({
    "annotation_uid", "city_object_id", "city_object_uid",
    "gml_id", "source_object_id",
    "concept", "scheme", "label", "status", "confidence",
    "attributes", "attributes_json", "assessed_at",
    "memberships", "value_fields", "path",
})
_MEMBERSHIP_KEYS = frozenset({
    "asset_part_id", "asset_uri", "part_path",
    "element_kind", "element_indices",
})
_VALUE_FIELD_KEYS = frozenset({
    "asset_part_id", "asset_uri", "part_path",
    "element_kind", "values", "value_dtype",
})
# A path segment names its own part, because element indices restart at zero in
# every one -- which is the whole reason the sequence is a table and not a key
# in attributes. 'continues_previous' marks a segment that carries on the
# previous one's run rather than starting a new one.
_PATH_SEGMENT_KEYS = frozenset({
    "asset_part_id", "asset_uri", "part_path",
    "element_kind", "element_indices", "continues_previous",
})


@dataclass(frozen=True)
class BatchAnnotationResult:
    annotation_id: int
    annotation_uid: str
    concept: str
    membership_count: int
    value_field_count: int = 0
    path_segment_count: int = 0


@dataclass
class BatchImportResult:
    annotation_count: int = 0
    membership_count: int = 0
    value_field_count: int = 0
    path_segment_count: int = 0
    created_city_object_count: int = 0
    created_city_object_uids: list[str] = field(default_factory=list)
    annotations: list[BatchAnnotationResult] = field(default_factory=list)


def apply_annotation_batch_file(
    pkg: USAPPackage,
    path: str | Path,
    *,
    load_vocabularies: bool = True,
    replace_existing: bool = False,
) -> BatchImportResult:
    batch_path = Path(path)

    if not batch_path.exists():
        raise FileNotFoundError(f"Batch file not found: {batch_path}")

    data = json.loads(batch_path.read_text(encoding="utf-8"))

    return apply_annotation_batch(
        pkg,
        data,
        base_dir=batch_path.parent,
        load_vocabularies=load_vocabularies,
        replace_existing=replace_existing,
    )


def apply_annotation_batch(
    pkg: USAPPackage,
    data: dict[str, Any],
    *,
    base_dir: str | Path = ".",
    load_vocabularies: bool = True,
    replace_existing: bool = False,
) -> BatchImportResult:
    """
    Apply a batch of annotations (the "linking JSON" of the ingestion
    procedures — see INGESTION.md).

    Batch format:

    {
      "vocabularies": ["my_vocabulary.json"],   # optional, paths are yours
      "create_missing_city_objects": false,
      "annotations": [
        {
          "annotation_uid": "ann_001",          # optional when a city object
                                                # is linked (derived)
          "concept": "EnergyRoof",              # optional when the linked
                                                # object already has a class
          "city_object_uid": "building_1_roof_1",
          "label": "Via Etnea, tratto 4",  # optional: display name
          "status": "draft",
          "confidence": 0.8,
          "attributes": {...},
          "assessed_at": "2026-06-30T14:00:00Z",  # optional: dates this
                                                  # evaluation (US-ANN-08)
          "memberships": [
            {
              "asset_part_id": 1,               # or "asset_uri" (+ optional
                                                # "part_path")
              "element_kind": "point",          # optional: the part's kind
              "element_indices": [100, 101]
            }
          ]
        }
      ]
    }

    An entry without "assessed_at" writes into the annotation's undated
    assessment, so a single-pass batch needs to know nothing about assessments.
    Re-running the same file with a *different* "assessed_at" on the same
    annotation_uid records a second evaluation beside the first rather than
    overwriting it — which is how a re-survey is imported.

    With "create_missing_city_objects": true (the minimal-vocabulary
    procedure), an unknown city_object_uid creates a carrier city object on
    the fly: an identity, object_status='temporary' (the marker for later
    alignment with a CityGML-backed object), nothing more. It is deliberately
    **classless** — an object's class belongs to the semantic source, and
    arrives when the CityGML import that aligns the carrier backfills it.
    Without the flag, unknown names fail loudly — which is the guardrail
    against mixing ad-hoc names into a CityGML-built package.

    An entry's "gml_id" and "source_object_id" say what the *object* is in the
    CityGML, and are written whether the object is created here or already
    existed — on the same terms as create_city_object itself: a column still
    empty is filled, a column already holding something else raises. They are
    not conditional on "create_missing_city_objects", since an object that is
    already there is exactly the case that flag does not cover.
    """
    base_path = Path(base_dir)

    vocabularies = data.get("vocabularies", [])
    annotations = data.get("annotations")
    create_missing_city_objects = bool(
        data.get("create_missing_city_objects", False)
    )

    if load_vocabularies and not isinstance(vocabularies, list):
        raise ValueError("'vocabularies' must be a list when provided.")

    if not isinstance(annotations, list):
        raise ValueError("Batch data must contain an 'annotations' list.")

    # Before the transaction opens, not inside it: a typo in the last of 8,869
    # entries should not roll back the first 8,868. One pass over the payload
    # is cheap next to the writes it guards.
    _check_batch_keys(data, annotations)

    result = BatchImportResult()

    # One transaction for the whole batch, vocabularies included, so a
    # failing annotation does not leave half-seeded vocabularies behind.
    with pkg.transaction():
        if load_vocabularies:
            for vocab in vocabularies:
                if not isinstance(vocab, str):
                    raise ValueError(f"Invalid vocabulary path: {vocab!r}")

                vocab_path = Path(vocab)

                if not vocab_path.is_absolute():
                    vocab_path = base_path / vocab_path

                seed_vocabulary_file(pkg, vocab_path)

        for item in annotations:
            annotation_result = _apply_one_annotation(
                pkg,
                item,
                replace_existing=replace_existing,
                create_missing_city_objects=create_missing_city_objects,
                created_city_object_uids=result.created_city_object_uids,
            )

            result.annotations.append(annotation_result)
            result.annotation_count += 1
            result.membership_count += annotation_result.membership_count
            result.value_field_count += annotation_result.value_field_count
            result.path_segment_count += annotation_result.path_segment_count

        result.created_city_object_count = len(result.created_city_object_uids)

    return result


def _check_batch_keys(
    data: dict[str, Any],
    annotations: list[Any],
) -> None:
    """
    Refuse field names nothing in the batch importer reads.

    Values are already checked where they are used — an unknown concept, an
    index past the part's element_count and a malformed assessed_at all raise
    today. What escapes is a key the importer never looks for: a value you
    never read is a value you cannot validate.
    """
    _check_keys(data, _BATCH_KEYS, where="the batch")

    for position, item in enumerate(annotations, start=1):
        if not isinstance(item, dict):
            # Shape is _apply_one_annotation's to report, with its own message.
            continue

        where = item.get("annotation_uid") or item.get("city_object_uid")
        where = (
            f"annotation entry {where!r}"
            if isinstance(where, str)
            else f"annotation entry {position}"
        )

        _check_keys(item, _ITEM_KEYS, where=where)

        for field_name, known in (
            ("memberships", _MEMBERSHIP_KEYS),
            ("value_fields", _VALUE_FIELD_KEYS),
            ("path", _PATH_SEGMENT_KEYS),
        ):
            blocks = item.get(field_name)

            if not isinstance(blocks, list):
                continue

            for block in blocks:
                if isinstance(block, dict):
                    _check_keys(
                        block,
                        known,
                        where=f"a {field_name!r} block of {where}",
                    )


def _object_uid_for(pkg: USAPPackage, city_object_id: int) -> str:
    """
    The object_uid of a city object named by its id.

    An entry may reference its object either way, but `create_city_object` is
    keyed on the uid -- that column is the row's identity -- so the int form
    needs this before it can write anything back.
    """
    return str(
        pkg.conn.execute(
            """
            SELECT object_uid
            FROM usap_city_object
            WHERE city_object_id = ?
            """,
            (city_object_id,),
        ).fetchone()["object_uid"]
    )


def _apply_one_annotation(
    pkg: USAPPackage,
    item: dict[str, Any],
    *,
    replace_existing: bool,
    create_missing_city_objects: bool,
    created_city_object_uids: list[str],
) -> BatchAnnotationResult:
    if not isinstance(item, dict):
        raise ValueError(f"Annotation entry must be an object: {item!r}")

    concept = item.get("concept")
    city_object_id = item.get("city_object_id")
    city_object_uid = item.get("city_object_uid")

    # Errors may occur before the annotation_uid is known (it can be
    # derived), so label entries by whatever identity they do carry.
    entry_label = (
        item.get("annotation_uid") or city_object_uid or "annotation entry"
    )

    if city_object_id is not None and city_object_uid is not None:
        raise USAPError(
            f"{entry_label}: provide city_object_id or city_object_uid, not both."
        )

    resolved_city_object_id: int | None = None

    # What the entry says about the object's own identity, as opposed to the
    # annotation's. Omitting a field claims nothing; create_city_object below
    # treats a None the same way, but filtering here keeps the "did this entry
    # say anything at all" test in one place.
    identity = {
        key: item[key]
        for key in ("gml_id", "source_object_id")
        if item.get(key) is not None
    }

    if city_object_id is not None:
        resolved_city_object_id = pkg.resolve_city_object(int(city_object_id))

        if identity:
            # create_city_object is keyed on object_uid, so the int form needs
            # the uid before it can write. One SELECT, so an entry referencing
            # its object by id is not quietly worse than one naming it.
            pkg.create_city_object(
                object_uid=_object_uid_for(pkg, resolved_city_object_id),
                **identity,
            )

    if city_object_uid is not None:
        # Only the lookup belongs in the try. An identity write raises
        # USAPError on a contradiction, and catching that here would read a
        # genuine conflict as "no such object" and mint a carrier for a name
        # that already exists.
        object_existed = True

        try:
            resolved_city_object_id = pkg.resolve_city_object(
                str(city_object_uid)
            )
        except USAPAmbiguityError:
            raise
        except USAPError:
            object_existed = False

            if not create_missing_city_objects:
                raise

            # The carrier itself is classless; the concept is still needed,
            # because it is what classes the *annotation* being written.
            if concept is None:
                raise ValueError(
                    f"{entry_label}: creating city object "
                    f"{city_object_uid!r} needs 'concept' to class the "
                    "annotation. The carrier object is created classless: its "
                    "own class belongs to the CityGML that later aligns it."
                )

            # Carrier object for the minimal-vocabulary procedure: an
            # identity and nothing more. Deliberately classless -- not because
            # an annotation's concept is the wrong *kind* of value here (any
            # registered concept is legal in this column, and nothing
            # validates the pairing), but because the column is the CityGML's
            # to fill and the annotator is not the CityGML. Guessing it is
            # silent: the package validates clean at every level, and the
            # contradiction surfaces only when the import that should align
            # this carrier meets a class already stored and has to refuse. The
            # class, and anything else still missing, arrives with that
            # alignment (create_city_object backfills).
            resolved_city_object_id = pkg.create_city_object(
                object_uid=str(city_object_uid),
                object_status="temporary",
                **identity,
            )
            created_city_object_uids.append(str(city_object_uid))

        # The object already existed, which is every re-run and every object
        # something else pre-created. What the entry knows about the CityGML
        # behind it is exactly as true here as on first creation; writing it
        # only in the branch above is how the identity went missing for every
        # entry that was not the first to name its object.
        if object_existed and identity:
            # Keyed by the row resolve actually matched, not by the string the
            # entry gave. resolve_city_object matches object_uid OR gml_id,
            # while create_city_object is keyed on object_uid alone and inserts
            # when it finds nothing -- so an entry naming its object by gml_id
            # would miss the resolved row and mint a second one, leaving two
            # rows claiming one gml_id (DUPLICATE_GML_ID) and every later
            # reference to that value permanently ambiguous.
            pkg.create_city_object(
                object_uid=_object_uid_for(pkg, resolved_city_object_id),
                **identity,
            )

    # The annotation's concept: explicit wins; otherwise it is inherited
    # from the linked city object's class (semantics from the CityGML side).
    if concept is not None:
        semantic_class_id = pkg.resolve_semantic_class(
            concept,
            scheme=item.get("scheme"),
        )
    elif resolved_city_object_id is not None:
        row = pkg.conn.execute(
            """
            SELECT semantic_class_id
            FROM usap_city_object
            WHERE city_object_id = ?
            """,
            (resolved_city_object_id,),
        ).fetchone()

        if row is None or row["semantic_class_id"] is None:
            raise ValueError(
                f"{entry_label}: linked city object has no semantic class; "
                "provide 'concept'. A carrier object created by this batch is "
                "deliberately classless, so items that reference one must "
                "carry their own 'concept'."
            )

        semantic_class_id = int(row["semantic_class_id"])
    else:
        raise ValueError(
            f"{entry_label}: provide 'concept' and/or a city object reference."
        )

    concept_local_name = pkg.conn.execute(
        """
        SELECT local_name
        FROM usap_semantic_class
        WHERE semantic_class_id = ?
        """,
        (semantic_class_id,),
    ).fetchone()["local_name"]

    annotation_uid = item.get("annotation_uid")

    if annotation_uid is not None:
        annotation_uid = require_str(item, "annotation_uid")
    else:
        # Deterministic default so re-applying the same file (procedure 3,
        # replace_existing=True) edits in place instead of duplicating.
        if resolved_city_object_id is None:
            raise ValueError(
                f"{entry_label}: annotation_uid is required when no city "
                "object is linked."
            )

        object_uid = pkg.conn.execute(
            """
            SELECT object_uid
            FROM usap_city_object
            WHERE city_object_id = ?
            """,
            (resolved_city_object_id,),
        ).fetchone()["object_uid"]

        annotation_uid = f"ann_{object_uid}_{concept_local_name}"

    existing = pkg.get_annotation(
        annotation_uid=annotation_uid,
        include_membership_summary=False,
    )

    if existing is not None and not replace_existing:
        raise USAPError(
            f"Annotation already exists: {annotation_uid}. "
            "Use replace_existing=True to update it."
        )

    attributes = item.get("attributes")
    attributes_json = item.get("attributes_json")

    if attributes is not None and attributes_json is not None:
        raise USAPError(
            f"{annotation_uid}: provide attributes or attributes_json, not both."
        )

    if existing is None:
        annotation = pkg.create_concept_annotation(
            concept=semantic_class_id,
            annotation_uid=annotation_uid,
            city_object_id=resolved_city_object_id,
            label=item.get("label"),
            status=item.get("status", "draft"),
            confidence=item.get("confidence"),
            attributes=attributes,
            attributes_json=attributes_json,
        )

        annotation_id = int(annotation["annotation_id"])

    else:
        # Replace is a partial update: only fields present in the batch entry
        # are changed. Omitted fields keep their existing values (relying on
        # update_annotation's _UNSET-by-omission behavior).
        update_kwargs: dict[str, Any] = {
            "semantic_class_id": semantic_class_id,
        }

        if "label" in item:
            update_kwargs["label"] = item.get("label")

        if "status" in item:
            update_kwargs["status"] = item.get("status")

        if "confidence" in item:
            update_kwargs["confidence"] = item.get("confidence")

        if city_object_id is not None or city_object_uid is not None:
            update_kwargs["primary_city_object_id"] = resolved_city_object_id

        if attributes is not None:
            update_kwargs["attributes_json"] = json.dumps(attributes)
        elif attributes_json is not None:
            update_kwargs["attributes_json"] = attributes_json

        updated = pkg.update_annotation(
            int(existing["annotation_id"]),
            **update_kwargs,
        )

        annotation_id = int(updated["annotation_id"])

        # No explicit link call here: update_annotation moves the 'represents'
        # link with primary_city_object_id, so linking again would only be able
        # to re-add a stale link for a previous object.

    memberships = item.get("memberships")
    value_fields = item.get("value_fields")
    path = item.get("path")

    if memberships is not None and (
        not isinstance(memberships, list) or not memberships
    ):
        raise ValueError(
            f"{annotation_uid}: 'memberships' must be a non-empty list "
            "when provided."
        )

    if value_fields is not None and (
        not isinstance(value_fields, list) or not value_fields
    ):
        raise ValueError(
            f"{annotation_uid}: 'value_fields' must be a non-empty list "
            "when provided."
        )

    if path is not None and (not isinstance(path, list) or not path):
        raise ValueError(
            f"{annotation_uid}: 'path' must be a non-empty list when provided."
        )

    if memberships is None and value_fields is None and path is None:
        raise ValueError(
            f"{annotation_uid}: provide at least one of 'memberships', "
            "'value_fields' or 'path'."
        )

    # One date for the whole entry: memberships and value fields written by the
    # same entry are one evaluation of it, so they must land in one assessment.
    assessed_at = item.get("assessed_at")

    membership_count = 0

    for membership in memberships or []:
        _apply_one_membership(
            pkg,
            annotation_id=annotation_id,
            annotation_uid=annotation_uid,
            membership=membership,
            assessed_at=assessed_at,
        )

        membership_count += 1

    value_field_count = 0

    for value_field in value_fields or []:
        _apply_one_value_field(
            pkg,
            annotation_id=annotation_id,
            annotation_uid=annotation_uid,
            value_field=value_field,
            assessed_at=assessed_at,
        )

        value_field_count += 1

    path_segment_count = 0

    if path is not None:
        # Applied last, and in one call rather than one per segment: a path is
        # cross-part by construction, and its ordinals are contiguous across
        # the whole assessment.
        path_segment_count = _apply_path(
            pkg,
            annotation_id=annotation_id,
            annotation_uid=annotation_uid,
            path=path,
            memberships=memberships,
            assessed_at=assessed_at,
        )

    return BatchAnnotationResult(
        annotation_id=annotation_id,
        annotation_uid=annotation_uid,
        concept=concept if isinstance(concept, str) else concept_local_name,
        membership_count=membership_count,
        value_field_count=value_field_count,
        path_segment_count=path_segment_count,
    )


def _resolve_part_reference(
    pkg: USAPPackage,
    *,
    annotation_uid: str,
    payload: dict[str, Any],
    field_name: str,
) -> tuple[int, int]:
    """
    Resolve a membership/value_field target to (asset_part_id, element_kind).

    The part is referenced by exactly one of:
    - "asset_part_id" (int, as in the build manifest)
    - "asset_uri" (str, + "part_path" when the asset has several parts)

    "element_kind" is optional: it defaults to the part's stored kind (when
    given, core still validates that it matches).
    """
    asset_part_id = payload.get("asset_part_id")
    asset_uri = payload.get("asset_uri")

    if (asset_part_id is None) == (asset_uri is None):
        raise ValueError(
            f"{annotation_uid}: {field_name} needs exactly one of "
            "'asset_part_id' or 'asset_uri'."
        )

    if asset_part_id is not None:
        if not isinstance(asset_part_id, int):
            raise ValueError(
                f"{annotation_uid}: {field_name}.asset_part_id must be int."
            )

        resolved_part_id = pkg.resolve_asset_part(asset_part_id)
    else:
        if not isinstance(asset_uri, str) or not asset_uri:
            raise ValueError(
                f"{annotation_uid}: {field_name}.asset_uri must be a "
                "non-empty string."
            )

        resolved_part_id = pkg.resolve_asset_part(
            asset_uri,
            part_path=payload.get("part_path"),
        )

    if "element_kind" in payload:
        element_kind = normalize_element_kind(payload["element_kind"])
    else:
        element_kind = int(
            pkg.conn.execute(
                """
                SELECT element_kind
                FROM usap_asset_part
                WHERE asset_part_id = ?
                """,
                (resolved_part_id,),
            ).fetchone()["element_kind"]
        )

    return resolved_part_id, element_kind


def _apply_one_membership(
    pkg: USAPPackage,
    *,
    annotation_id: int,
    annotation_uid: str,
    membership: dict[str, Any],
    assessed_at: str | None = None,
) -> None:
    if not isinstance(membership, dict):
        raise ValueError(
            f"{annotation_uid}: membership must be an object: {membership!r}"
        )

    asset_part_id, element_kind = _resolve_part_reference(
        pkg,
        annotation_uid=annotation_uid,
        payload=membership,
        field_name="membership",
    )

    element_indices = membership.get("element_indices")

    if not isinstance(element_indices, list) or not element_indices:
        raise ValueError(
            f"{annotation_uid}: membership.element_indices must be a non-empty list."
        )

    if not all(isinstance(i, int) for i in element_indices):
        raise ValueError(
            f"{annotation_uid}: membership.element_indices must contain only ints."
        )

    # Element-kind match and index-bounds are validated by
    # core.replace_annotation_membership (via _validate_membership_indices),
    # so we do not duplicate those checks here.
    pkg.attach_annotation_elements(
        annotation_id=annotation_id,
        asset_part_id=asset_part_id,
        element_kind=element_kind,
        element_indices=element_indices,
        assessment=_assessment_for_entry(
            pkg,
            annotation_id=annotation_id,
            asset_part_id=asset_part_id,
            assessed_at=assessed_at,
        ),
    )


def _apply_path(
    pkg: USAPPackage,
    *,
    annotation_id: int,
    annotation_uid: str,
    path: list,
    memberships: list | None,
    assessed_at: str | None = None,
) -> int:
    """
    Apply an entry's ordered path, in one call.

    Unlike memberships and value fields, this is not per-block: a path's
    segment ordinals run across the whole assessment, so the segments have to
    arrive together. The membership is derived from it by set_annotation_path,
    which is why an entry may carry `path` and no `memberships` at all.
    """
    segments = []

    for position, segment in enumerate(path):
        if not isinstance(segment, dict):
            raise ValueError(
                f"{annotation_uid}: path segment {position} must be an object: "
                f"{segment!r}"
            )

        asset_part_id, _element_kind = _resolve_part_reference(
            pkg,
            annotation_uid=annotation_uid,
            payload=segment,
            field_name="path",
        )

        element_indices = segment.get("element_indices")

        if not isinstance(element_indices, list) or not element_indices:
            raise ValueError(
                f"{annotation_uid}: path segment {position} needs a non-empty "
                "'element_indices' list."
            )

        if not all(isinstance(i, int) for i in element_indices):
            raise ValueError(
                f"{annotation_uid}: path segment {position} element_indices "
                "must contain only ints."
            )

        segments.append(
            {
                "asset_part_id": asset_part_id,
                "element_indices": element_indices,
                "continues_previous": bool(
                    segment.get("continues_previous", False)
                ),
            }
        )

    # An entry that also writes membership for a part the path covers is
    # asserting the same geometry twice, and the derived one would win. That is
    # the drift this design exists to prevent, so it is refused here rather
    # than resolved silently -- and refused before anything is written, so the
    # entry is not half-applied.
    path_parts = {segment["asset_part_id"] for segment in segments}

    for membership in memberships or []:
        if not isinstance(membership, dict):
            continue

        membership_part, _kind = _resolve_part_reference(
            pkg,
            annotation_uid=annotation_uid,
            payload=membership,
            field_name="membership",
        )

        if membership_part in path_parts:
            raise ValueError(
                f"{annotation_uid}: asset part {membership_part} is covered by "
                "both 'path' and 'memberships'. The membership is derived from "
                "the path, so listing both states the same geometry twice. "
                "Drop the 'memberships' entry for that part."
            )

    # One assessment for the whole path, not one per segment: the ordinals are
    # scoped to the assessment, and segments in parts of different assets would
    # otherwise resolve to different ones.
    assessment = _assessment_for_entry(
        pkg,
        annotation_id=annotation_id,
        asset_part_id=segments[0]["asset_part_id"],
        assessed_at=assessed_at,
    )

    pkg.set_annotation_path(annotation_id, segments, assessment=assessment)

    return len(segments)


def _assessment_for_entry(
    pkg: USAPPackage,
    *,
    annotation_id: int,
    asset_part_id: int,
    assessed_at: str | None,
) -> int | None:
    """
    The assessment a batch entry writes into, or None for the default.

    Resolved per (annotation, asset) rather than once per entry: one entry may
    carry memberships on several assets, and each asset is evaluated by its own
    assessment.
    """
    if assessed_at is None:
        return None

    part = pkg.list_asset_parts()
    asset_id = next(
        (p["asset_id"] for p in part if p["asset_part_id"] == asset_part_id),
        None,
    )

    if asset_id is None:
        raise USAPError(f"Unknown asset_part_id: {asset_part_id}")

    assessment = pkg.create_assessment(
        annotation_id,
        int(asset_id),
        assessed_at=assessed_at,
    )

    return int(assessment["assessment_id"])


def _apply_one_value_field(
    pkg: USAPPackage,
    *,
    annotation_id: int,
    annotation_uid: str,
    value_field: dict[str, Any],
    assessed_at: str | None = None,
) -> None:
    if not isinstance(value_field, dict):
        raise ValueError(
            f"{annotation_uid}: value_field must be an object: {value_field!r}"
        )

    asset_part_id, element_kind = _resolve_part_reference(
        pkg,
        annotation_uid=annotation_uid,
        payload=value_field,
        field_name="value_field",
    )

    values = value_field.get("values")

    if not isinstance(values, list) or not values:
        raise ValueError(
            f"{annotation_uid}: value_field.values must be a non-empty list."
        )

    if not all(v is None or isinstance(v, (int, float)) for v in values):
        raise ValueError(
            f"{annotation_uid}: value_field.values must contain only "
            "numbers or null."
        )

    # JSON null means "no value" -> NaN. Full coverage, dtype handling, and
    # the NaN-vs-integer-dtype rule are validated by core.replace_value_field.
    pkg.replace_value_field(
        annotation_id=annotation_id,
        asset_part_id=asset_part_id,
        element_kind=element_kind,
        values=[float("nan") if v is None else v for v in values],
        value_dtype=value_field.get("value_dtype"),
        assessment=_assessment_for_entry(
            pkg,
            annotation_id=annotation_id,
            asset_part_id=asset_part_id,
            assessed_at=assessed_at,
        ),
    )

