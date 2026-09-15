"""
Filling in a city object that already exists, and accepting a carrier.

`create_city_object` is idempotent on object_uid, and since 0.4.2 a repeat call
supplying a *different* value for a stored field raises rather than discarding
it. That hardening caught a real mistake — an annotation's concept arriving as
the object's class — but it also refused the case where the stored value is
simply absent, which is how a carrier created by the batch is meant to acquire
its class and its gml_id when the CityGML that owns them arrives.

These tests pin the distinction: a NULL is an enrichment and is filled, a
different value is a contradiction and raises.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import make_pkg
from usap import USAPError


def _concepts(pkg) -> tuple[int, int]:
    roof = pkg.create_semantic_class(
        scheme="local", class_uri="local:RoofSurface", local_name="RoofSurface"
    )
    energy = pkg.create_semantic_class(
        scheme="local", class_uri="local:EnergyRoof", local_name="EnergyRoof"
    )
    return roof, energy


def _row(pkg, object_uid: str):
    return pkg.conn.execute(
        """
        SELECT semantic_class_id, gml_id, source_object_id,
               source_asset_id, attributes_json, object_status
        FROM usap_city_object
        WHERE object_uid = ?
        """,
        (object_uid,),
    ).fetchone()


def test_backfill_fills_every_still_empty_column(tmp_path: Path) -> None:
    with make_pkg(tmp_path) as pkg:
        roof, _ = _concepts(pkg)
        asset_id = pkg.register_asset(uri="city.gml", asset_kind="citygml")

        # A carrier: an identity and nothing else, exactly what the batch mints.
        first = pkg.create_city_object(object_uid="b1_roof", object_status="temporary")

        stored = _row(pkg, "b1_roof")
        assert stored["semantic_class_id"] is None
        assert stored["gml_id"] is None

        # The alignment call: everything arrives at once.
        again = pkg.create_city_object(
            object_uid="b1_roof",
            semantic_class_id=roof,
            gml_id="b1_roof",
            source_asset_id=asset_id,
            source_object_id="b1_roof",
            attributes_json='{"source": "citygml_adapter"}',
        )

        assert again == first, "backfill must return the same row, not a new one"

        stored = _row(pkg, "b1_roof")
        assert stored["semantic_class_id"] == roof
        assert stored["gml_id"] == "b1_roof"
        assert stored["source_asset_id"] == asset_id
        assert stored["source_object_id"] == "b1_roof"
        assert stored["attributes_json"] == '{"source": "citygml_adapter"}'


def test_backfill_is_recorded_in_the_edit_log(tmp_path: Path) -> None:
    # A package is an audit trail as much as a store; a write nothing recorded
    # is a write nobody can explain later.
    with make_pkg(tmp_path) as pkg:
        roof, _ = _concepts(pkg)
        pkg.create_city_object(object_uid="b1_roof", object_status="temporary")
        pkg.create_city_object(object_uid="b1_roof", semantic_class_id=roof)

        operations = [
            row["operation"]
            for row in pkg.conn.execute(
                "SELECT operation FROM usap_edit_log ORDER BY edit_id"
            )
        ]

        assert "backfill_city_object" in operations


def test_a_contradiction_still_raises(tmp_path: Path) -> None:
    # The 0.4.2 hardening's actual target. Filling a blank was collateral;
    # this case must keep raising, and the message must name the column.
    with make_pkg(tmp_path) as pkg:
        roof, energy = _concepts(pkg)

        pkg.create_city_object(
            object_uid="b1_roof", semantic_class_id=roof, gml_id="b1_roof"
        )

        with pytest.raises(USAPError, match="different semantic_class_id"):
            pkg.create_city_object(object_uid="b1_roof", semantic_class_id=energy)

        with pytest.raises(USAPError, match="different gml_id"):
            pkg.create_city_object(object_uid="b1_roof", gml_id="something_else")

        # Nothing was written by the refused calls.
        stored = _row(pkg, "b1_roof")
        assert stored["semantic_class_id"] == roof
        assert stored["gml_id"] == "b1_roof"


def test_the_bare_give_me_the_id_call_stays_valid(tmp_path: Path) -> None:
    # The carrier lookup idiom: requesting nothing claims nothing, so it must
    # not conflict with a fully populated row.
    with make_pkg(tmp_path) as pkg:
        roof, _ = _concepts(pkg)
        first = pkg.create_city_object(
            object_uid="b1_roof", semantic_class_id=roof, gml_id="b1_roof"
        )

        assert pkg.create_city_object(object_uid="b1_roof") == first


def test_an_empty_gml_id_is_stored_as_null(tmp_path: Path) -> None:
    # "" is not an identity. Stored as one it survives the "no claim" filter
    # and then compares as a value, which would make the row uncompletable for
    # good -- the exact failure this pair of fixes exists to end.
    with make_pkg(tmp_path) as pkg:
        pkg.create_city_object(object_uid="b1_roof", gml_id="", source_object_id="")

        stored = _row(pkg, "b1_roof")
        assert stored["gml_id"] is None
        assert stored["source_object_id"] is None


def test_a_row_already_holding_an_empty_string_still_completes(tmp_path: Path) -> None:
    # Rows written this way by an older build must complete on the next real
    # value, without a repair API or a migration script. Written through raw
    # SQL because the normalisation above is what now prevents it.
    with make_pkg(tmp_path) as pkg:
        pkg.create_city_object(object_uid="b1_roof", object_status="temporary")

        with pkg.transaction():
            pkg.conn.execute(
                """
                UPDATE usap_city_object
                SET gml_id = '', source_object_id = ''
                WHERE object_uid = 'b1_roof'
                """
            )

        pkg.create_city_object(
            object_uid="b1_roof", gml_id="b1_roof", source_object_id="b1_roof"
        )

        stored = _row(pkg, "b1_roof")
        assert stored["gml_id"] == "b1_roof"
        assert stored["source_object_id"] == "b1_roof"


def test_attributes_json_respelled_is_not_a_conflict(tmp_path: Path) -> None:
    # Two spellings of the same JSON are the same claim; reporting them as a
    # conflict would make an idempotent re-import fail on whitespace.
    with make_pkg(tmp_path) as pkg:
        first = pkg.create_city_object(
            object_uid="b1_roof", attributes_json='{"a": 1, "b": 2}'
        )

        assert (
            pkg.create_city_object(
                object_uid="b1_roof", attributes_json='{"b":2,"a":1}'
            )
            == first
        )


def test_accept_city_object_promotes_once_and_has_no_reverse(tmp_path: Path) -> None:
    with make_pkg(tmp_path) as pkg:
        pkg.create_city_object(object_uid="b1_roof", object_status="temporary")
        assert _row(pkg, "b1_roof")["object_status"] == "temporary"

        pkg.accept_city_object("b1_roof")
        assert _row(pkg, "b1_roof")["object_status"] == "accepted"

        # Idempotent: a second call is a quiet no-op, not an error.
        pkg.accept_city_object("b1_roof")
        assert _row(pkg, "b1_roof")["object_status"] == "accepted"

        # And there is deliberately no way back through the API: create_city_object
        # does not backfill object_status, so asking for 'temporary' changes nothing.
        pkg.create_city_object(object_uid="b1_roof", object_status="temporary")
        assert _row(pkg, "b1_roof")["object_status"] == "accepted"


def test_accept_city_object_rejects_an_unknown_object(tmp_path: Path) -> None:
    with make_pkg(tmp_path) as pkg:
        with pytest.raises(USAPError, match="City object not found"):
            pkg.accept_city_object("no_such_object")
