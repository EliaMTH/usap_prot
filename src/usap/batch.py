from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .core import USAPPackage
from .errors import USAPError
from .domain_vocab import seed_vocabulary_file
from .constants import normalize_element_kind


@dataclass(frozen=True)
class BatchAnnotationResult:
    annotation_id: int
    annotation_uid: str
    concept: str
    membership_count: int


@dataclass
class BatchImportResult:
    annotation_count: int = 0
    membership_count: int = 0
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
    Apply a batch of annotations.

    Batch format:

    {
      "vocabularies": ["vocabularies/citygml_3_0_mvp.json"],
      "annotations": [
        {
          "annotation_uid": "ann_001",
          "concept": "EnergyRoof",
          "city_object_uid": "building_1_roof_1",
          "label": "Energy roof annotation",
          "status": "draft",
          "confidence": 0.8,
          "attributes": {...},
          "memberships": [
            {
              "asset_part_id": 1,
              "element_kind": "point",
              "element_indices": [100, 101]
            }
          ]
        }
      ]
    }
    """
    base_path = Path(base_dir)

    vocabularies = data.get("vocabularies", [])
    annotations = data.get("annotations")

    if load_vocabularies:
        if not isinstance(vocabularies, list):
            raise ValueError("'vocabularies' must be a list when provided.")

        for vocab in vocabularies:
            if not isinstance(vocab, str):
                raise ValueError(f"Invalid vocabulary path: {vocab!r}")

            vocab_path = Path(vocab)

            if not vocab_path.is_absolute():
                vocab_path = base_path / vocab_path

            seed_vocabulary_file(pkg, vocab_path)

    if not isinstance(annotations, list):
        raise ValueError("Batch data must contain an 'annotations' list.")

    result = BatchImportResult()

    with pkg.transaction():
        for item in annotations:
            annotation_result = _apply_one_annotation(
                pkg,
                item,
                replace_existing=replace_existing,
            )

            result.annotations.append(annotation_result)
            result.annotation_count += 1
            result.membership_count += annotation_result.membership_count

    return result


def _apply_one_annotation(
    pkg: USAPPackage,
    item: dict[str, Any],
    *,
    replace_existing: bool,
) -> BatchAnnotationResult:
    if not isinstance(item, dict):
        raise ValueError(f"Annotation entry must be an object: {item!r}")

    annotation_uid = _required_str(item, "annotation_uid")
    concept = _required_str(item, "concept")

    existing = pkg.get_annotation(
        annotation_uid=annotation_uid,
        include_membership_summary=False,
    )

    if existing is not None and not replace_existing:
        raise USAPError(
            f"Annotation already exists: {annotation_uid}. "
            "Use replace_existing=True to update it."
        )

    city_object_id = item.get("city_object_id")
    city_object_uid = item.get("city_object_uid")

    if city_object_id is not None and city_object_uid is not None:
        raise USAPError(
            f"{annotation_uid}: provide city_object_id or city_object_uid, not both."
        )

    attributes = item.get("attributes")
    attributes_json = item.get("attributes_json")

    if attributes is not None and attributes_json is not None:
        raise USAPError(
            f"{annotation_uid}: provide attributes or attributes_json, not both."
        )

    if existing is None:
        annotation = pkg.create_concept_annotation(
            concept=concept,
            annotation_uid=annotation_uid,
            city_object_id=city_object_id,
            city_object_uid=city_object_uid,
            label=item.get("label"),
            status=item.get("status", "draft"),
            confidence=item.get("confidence"),
            attributes=attributes,
            attributes_json=attributes_json,
            scheme=item.get("scheme"),
        )

        annotation_id = int(annotation["annotation_id"])

    else:
        semantic_class_id = pkg.resolve_semantic_class(
            concept,
            scheme=item.get("scheme"),
        )

        resolved_city_object_id = None

        if city_object_id is not None:
            resolved_city_object_id = pkg.resolve_city_object(int(city_object_id))

        if city_object_uid is not None:
            resolved_city_object_id = pkg.resolve_city_object(str(city_object_uid))

        stored_attributes_json = attributes_json

        if attributes is not None:
            stored_attributes_json = json.dumps(attributes)

        updated = pkg.update_annotation(
            int(existing["annotation_id"]),
            semantic_class_id=semantic_class_id,
            primary_city_object_id=resolved_city_object_id,
            label=item.get("label"),
            status=item.get("status", existing["status"]),
            confidence=item.get("confidence"),
            attributes_json=stored_attributes_json,
        )

        annotation_id = int(updated["annotation_id"])

    memberships = item.get("memberships")

    if not isinstance(memberships, list) or not memberships:
        raise ValueError(
            f"{annotation_uid}: 'memberships' must be a non-empty list."
        )

    membership_count = 0

    for membership in memberships:
        _apply_one_membership(
            pkg,
            annotation_id=annotation_id,
            annotation_uid=annotation_uid,
            membership=membership,
        )

        membership_count += 1

    return BatchAnnotationResult(
        annotation_id=annotation_id,
        annotation_uid=annotation_uid,
        concept=concept,
        membership_count=membership_count,
    )


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

    asset_part_id = membership.get("asset_part_id")
    raw_element_kind = membership.get("element_kind")
    element_kind = normalize_element_kind(raw_element_kind)
    element_indices = membership.get("element_indices")

    if not isinstance(asset_part_id, int):
        raise ValueError(f"{annotation_uid}: membership.asset_part_id must be int.")

    if not isinstance(element_indices, list):
        raise ValueError(
            f"{annotation_uid}: membership.element_indices must be a list."
        )

    if not all(isinstance(i, int) for i in element_indices):
        raise ValueError(
            f"{annotation_uid}: membership.element_indices must contain only ints."
        )

    _validate_asset_part_and_indices(
        pkg,
        annotation_uid=annotation_uid,
        asset_part_id=asset_part_id,
        element_kind=element_kind,
        element_indices=element_indices,
    )

    pkg.attach_annotation_elements(
        annotation_id=annotation_id,
        asset_part_id=asset_part_id,
        element_kind=element_kind,
        element_indices=element_indices,
    )


def _validate_asset_part_and_indices(
    pkg: USAPPackage,
    *,
    annotation_uid: str,
    asset_part_id: int,
    element_kind: str,
    element_indices: list[int],
) -> None:
    row = pkg.conn.execute(
        """
        SELECT asset_part_id, element_kind, element_count
        FROM usap_asset_part
        WHERE asset_part_id = ?
        """,
        (asset_part_id,),
    ).fetchone()

    if row is None:
        raise USAPError(
            f"{annotation_uid}: asset_part_id does not exist: {asset_part_id}"
        )

    stored_element_kind = row["element_kind"]
    if stored_element_kind != element_kind:
        raise USAPError(
            f"{annotation_uid}: element_kind mismatch for asset_part_id "
            f"{asset_part_id}. Got {element_kind!r}, expected "
            f"{stored_element_kind!r}."
        )

    element_count = int(row["element_count"])

    bad = [
        i for i in element_indices
        if i < 0 or i >= element_count
    ]

    if bad:
        preview = bad[:10]

        raise USAPError(
            f"{annotation_uid}: element indices out of range for asset_part_id "
            f"{asset_part_id}. element_count={element_count}, bad={preview}"
        )


def _required_str(
    item: dict[str, Any],
    key: str,
) -> str:
    value = item.get(key)

    if not isinstance(value, str) or not value:
        raise ValueError(f"Missing required string field: {key}")

    return value

