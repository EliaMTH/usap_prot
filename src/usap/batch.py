from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._util import require_str
from .core import USAPPackage
from .errors import USAPAmbiguityError, USAPError
from .domain_vocab import seed_vocabulary_file
from .constants import normalize_element_kind


@dataclass(frozen=True)
class BatchAnnotationResult:
    annotation_id: int
    annotation_uid: str
    concept: str
    membership_count: int
    value_field_count: int = 0


@dataclass
class BatchImportResult:
    annotation_count: int = 0
    membership_count: int = 0
    value_field_count: int = 0
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
          "label": "Energy roof annotation",
          "status": "draft",
          "confidence": 0.8,
          "attributes": {...},
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

    With "create_missing_city_objects": true (the minimal-vocabulary
    procedure), an unknown city_object_uid creates a carrier city object on
    the fly: classed by the entry's concept, object_status='temporary' (the
    marker for later alignment with a CityGML-backed object), nothing else.
    Without the flag, unknown names fail loudly — which is the guardrail
    against mixing ad-hoc names into a CityGML-built package.
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

        result.created_city_object_count = len(result.created_city_object_uids)

    return result


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

    if city_object_id is not None:
        resolved_city_object_id = pkg.resolve_city_object(int(city_object_id))

    if city_object_uid is not None:
        try:
            resolved_city_object_id = pkg.resolve_city_object(
                str(city_object_uid)
            )
        except USAPAmbiguityError:
            raise
        except USAPError:
            if not create_missing_city_objects:
                raise

            if concept is None:
                raise ValueError(
                    f"{entry_label}: creating city object "
                    f"{city_object_uid!r} needs 'concept' to say what it is."
                )

            # Carrier object for the minimal-vocabulary procedure: classed
            # by "what it is", marked temporary for later alignment with a
            # CityGML-backed object, nothing else.
            resolved_city_object_id = pkg.create_city_object(
                object_uid=str(city_object_uid),
                semantic_class_id=pkg.resolve_semantic_class(
                    concept,
                    scheme=item.get("scheme"),
                ),
                object_status="temporary",
            )
            created_city_object_uids.append(str(city_object_uid))

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
                "provide 'concept'."
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

    if memberships is None and value_fields is None:
        raise ValueError(
            f"{annotation_uid}: provide at least one of 'memberships' "
            "or 'value_fields'."
        )

    membership_count = 0

    for membership in memberships or []:
        _apply_one_membership(
            pkg,
            annotation_id=annotation_id,
            annotation_uid=annotation_uid,
            membership=membership,
        )

        membership_count += 1

    value_field_count = 0

    for value_field in value_fields or []:
        _apply_one_value_field(
            pkg,
            annotation_id=annotation_id,
            annotation_uid=annotation_uid,
            value_field=value_field,
        )

        value_field_count += 1

    return BatchAnnotationResult(
        annotation_id=annotation_id,
        annotation_uid=annotation_uid,
        concept=concept if isinstance(concept, str) else concept_local_name,
        membership_count=membership_count,
        value_field_count=value_field_count,
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
    )


def _apply_one_value_field(
    pkg: USAPPackage,
    *,
    annotation_id: int,
    annotation_uid: str,
    value_field: dict[str, Any],
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
    )

