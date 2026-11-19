"""峰值需量证据与风险区间：逐格覆盖、平均需量、确认峰值、未知范围与合同状态。"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from app.models import DemandReading, Meter
from app.services import demand as demand_service
from app.services.demand_acceptance import run_demand_acceptance

PS = datetime(2026, 11, 9, 0, 0)
PE = datetime(2026, 11, 10, 0, 0)
CUTOFF = datetime(2026, 11, 10, 0, 0)


def _db():
    from app.db import SessionLocal

    return SessionLocal()


def _report(db, building: str, method: str, cutoff: datetime = CUTOFF):
    return demand_service.compute_demand_report(
        db, building, method, PS, PE, cutoff
    )


def _slot(detail: dict, slot: str) -> dict:
    for s in detail["slots"]:
        if s["slot"] == slot:
            return s
    raise KeyError(slot)


# ---------------------------------------------------------------- API surface


def test_demand_meta_lists_contracts_and_methods(client):
    client.post("/api/acceptance/reset", json={})
    resp = client.get("/api/demand/meta")
    assert resp.status_code == 200
    data = resp.json()
    assert {m["method"] for m in data["methods"]} == {"grid", "integration"}
    by_code = {b["code"]: b for b in data["buildings"]}
    assert by_code["B1"]["contract_kw"] == 900.0
    assert by_code["B3"]["contract_kw"] == 1000.0
    modes = {m["building_code"]: m["mode"] for m in data["meters"]}
    assert modes["B3"] == "register"
    assert modes["B1"] == "energy_interval"


def test_demand_acceptance_scenarios_all_pass(client):
    resp = client.post("/api/demand/acceptance/run", json={})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    checks = [
        c for step in data["steps"] for c in step["detail"].get("checks", [])
    ]
    assert checks
    assert all(c["ok"] for c in checks), [
        c for c in checks if not c["ok"]
    ]
    keys = [s["step"] for s in data["steps"]]
    assert keys == [
        "demand_grid_coverage",
        "demand_noon_gap",
        "demand_boundary_sampling",
        "demand_cumulative_rollback",
        "demand_integration_route",
        "demand_physical_bound",
        "demand_late_peak",
    ]


def test_publish_binds_cutoff_and_versions_on_late_reading(client):
    client.post("/api/acceptance/reset", json={})
    first = client.post(
        "/api/demand/reports", json={"cutoff": "2026-11-10T00:00:00"}
    ).json()
    b2_before = next(
        r
        for r in first["reports"]
        if r["building_code"] == "B2" and r["method"] == "grid"
    )
    assert first["data_cutoff"] == "2026-11-10T00:00:00Z"
    assert b2_before["contract_status"] == "not_proven"
    second = client.post(
        "/api/demand/reports", json={"cutoff": "2026-11-10T02:00:00"}
    )
    assert second.status_code == 200
    # a cutoff before the period is rejected rather than silently clamped
    bad = client.post(
        "/api/demand/reports", json={"cutoff": "2026-11-08T00:00:00"}
    )
    assert bad.status_code == 409
    # reset removes the versions published by this test
    client.post("/api/demand/acceptance/reset", json={})


# ---------------------------------------------------------------- grid route


def test_grid_full_cells_and_duplicate_arrivals(client):
    client.post("/api/demand/acceptance/reset", json={})
    with _db() as db:
        detail = _report(db, "B1", "grid")
    assert detail["counts"]["full"] == 96
    assert detail["counts"]["unknown"] == 0
    assert detail["counts"]["duplicates"] >= 1
    slot = _slot(detail, "2026-11-09T14:00:00Z")
    assert slot["status"] == "full"
    assert slot["demand_kw"] > 880
    # duplicate arrival did not double the 09:00 cell
    dup = _slot(detail, "2026-11-09T09:00:00Z")
    assert dup["status"] == "full"
    assert dup["demand_kw"] < 760


def test_grid_only_confirms_complete_cells(client):
    client.post("/api/demand/acceptance/reset", json={})
    with _db() as db:
        detail = _report(db, "B4", "grid")
    # every settlement reading straddles the grid boundary: nothing is confirmed
    assert detail["counts"]["full"] == 0
    assert detail["counts"]["partial"] == 96
    assert detail["confirmed_peak"]["demand_kw"] is None
    for slot in detail["slots"]:
        assert slot["demand_kw"] is None
        # No reading is fully inside a cell, so even the observed portion is zero:
        # the slot is unknown rather than a prorated pseudo-average.
        assert slot["observed_kw"] == 0
        assert slot["lower_kw"] == slot["observed_kw"]
        assert any(r["kind"] == "boundary_straddle" for r in slot["meters"][0]["reasons"])
    assert any(w["kind"] == "boundary_straddle" for w in detail["warnings"])


def test_integration_uses_irregular_sampling_and_recovers_peak(client):
    client.post("/api/demand/acceptance/reset", json={})
    with _db() as db:
        grid = _report(db, "B2", "grid")
        integ = _report(db, "B2", "integration")
    assert grid["counts"]["unknown"] == 4
    assert grid["contract_status"] == "not_proven"
    assert integ["counts"]["unknown"] == 0
    assert integ["confirmed_peak"]["slot"] == "2026-11-09T12:00:00Z"
    assert integ["confirmed_peak"]["demand_kw"] > 950
    assert integ["contract_status"] == "exceeded"
    # grid route keeps the unseen gap as a candidate peak with no bound
    candidates = {c["slot"] for c in grid["candidate_peak_slots"]}
    assert "2026-11-09T12:00:00Z" in candidates
    # a candidate is kept only when the contract cannot be excluded: either no bound
    # at all, or a documented bound above the contract capacity
    assert all(
        c["upper_kw"] is None or c["upper_kw"] > 850 for c in grid["candidate_peak_slots"]
    )


def test_cumulative_rollback_is_unknown_not_negative_demand(client):
    client.post("/api/demand/acceptance/reset", json={})
    with _db() as db:
        detail = _report(db, "B3", "grid")
    assert detail["counts"]["rollbacks"] == 1
    assert detail["risk_interval"]["lower_kw"] >= 0
    for slot in detail["slots"]:
        assert slot["demand_kw"] is None or slot["demand_kw"] >= 0
        assert slot["observed_kw"] >= 0
    assert detail["confirmed_peak"]["demand_kw"] > 1000
    assert detail["contract_status"] == "exceeded"


# ---------------------------------------------------------------- contract


def test_not_proven_when_uncovered_cells_exist(client):
    client.post("/api/demand/acceptance/reset", json={})
    with _db() as db:
        detail = _report(db, "B2", "grid")
    assert detail["confirmed_peak"]["demand_kw"] < 850.0
    assert detail["counts"]["unknown"] > 0
    assert detail["contract_status"] == "not_proven"
    assert "尚未证实超限" in detail["conclusion"]
    assert "已证实未超限" not in detail["conclusion"]


def test_proven_safe_only_with_full_coverage_or_documented_bound(client):
    client.post("/api/demand/acceptance/reset", json={})
    with _db() as db:
        b1 = _report(db, "B1", "grid")
        b4 = _report(db, "B4", "integration")
    assert b1["counts"]["unknown"] == 0
    assert b1["contract_status"] == "not_exceeded"
    assert b1["contract_basis"] == "full_coverage"
    # B4 integration: a couple of cells are partial but a registered adjacent-step
    # bound keeps the whole period's upper bound below the contract.
    assert b4["counts"]["unknown"] > 0
    assert b4["contract_status"] == "not_exceeded"
    assert b4["contract_basis"] == "physical_bound"
    assert b4["risk_interval"]["upper_kw"] <= 700


def test_partial_observed_lower_bound_can_prove_exceeded(client):
    """A partial cell has no confirmed 15-minute average, but its observed energy is an
    honest lower bound (loads are non-negative). If that bound already exceeds the
    contract, exceeding is proven without pretending the cell is complete."""
    client.post("/api/demand/acceptance/reset", json={})
    with _db() as db:
        meter = (
            db.execute(select(Meter).where(Meter.code == "M-DMD-B2-01"))
            .scalars()
            .one()
        )
        # extend the 13:00 cell to cover only the first half, but at a very high load
        start = datetime(2026, 11, 9, 13, 0)
        db.add(
            DemandReading(
                meter_id=meter.id,
                kind="energy",
                stream="settlement",
                measured_from=start,
                measured_to=start + timedelta(minutes=7, seconds=30),
                value=1200.0 * (450 / 3600.0),
                received_at=start + timedelta(minutes=15),
                note="test:lower_bound",
            )
        )
        db.commit()
        try:
            detail = _report(db, "B2", "grid")
        finally:
            for row in (
                db.execute(
                    select(DemandReading).where(
                        DemandReading.note == "test:lower_bound"
                    )
                )
                .scalars()
                .all()
            ):
                db.delete(row)
            db.commit()
    assert detail["counts"]["partial"] >= 1
    assert detail["contract_status"] == "exceeded"
    assert detail["contract_basis"] == "observed_lower_bound"
    slot = _slot(detail, "2026-11-09T13:00:00Z")
    assert slot["demand_kw"] is None
    assert slot["observed_kw"] > 850


def test_no_contract_means_no_verdict(client):
    client.post("/api/demand/acceptance/reset", json={})
    with _db() as db:
        from app.models import ContractCapacity

        rows = list(db.execute(select(ContractCapacity)).scalars())
        snapshot = [
            {
                "building_id": row.building_id,
                "product_field": row.product_field,
                "capacity_kw": row.capacity_kw,
                "valid_from": row.valid_from,
                "valid_to": row.valid_to,
                "source": row.source,
                "note": row.note,
            }
            for row in rows
        ]
        for row in rows:
            db.delete(row)
        db.commit()
        detail = _report(db, "B1", "grid")
        # put the contract thresholds back so later tests see the seeded values
        for item in snapshot:
            db.add(ContractCapacity(**item))
        db.commit()
    assert detail["contract_status"] == "no_contract"
    assert detail["confirmed_peak"]["demand_kw"] is not None


# ---------------------------------------------------------------- time semantics


def test_received_time_does_not_substitute_for_sampling_time(client):
    client.post("/api/demand/acceptance/reset", json={})
    """A reading that arrives late must be invisible at the earlier cutoff and appear,
    normalized to its *sampling* cell, at the later one."""
    with _db() as db:
        before = _report(db, "B2", "grid", cutoff=datetime(2026, 11, 10, 0, 0))
        meter = (
            db.execute(select(Meter).where(Meter.code == "M-DMD-B2-01"))
            .scalars()
            .one()
        )
        for i in range(4):
            start = datetime(2026, 11, 9, 12, 0) + timedelta(minutes=15 * i)
            db.add(
                DemandReading(
                    meter_id=meter.id,
                    kind="energy",
                    stream="settlement",
                    measured_from=start,
                    measured_to=start + timedelta(minutes=15),
                    value=960.0 * 0.25,
                    received_at=datetime(2026, 11, 10, 2, 0),
                    note="test:sampling_time",
                )
            )
        db.commit()
        try:
            after = _report(db, "B2", "grid", cutoff=datetime(2026, 11, 10, 2, 0))
            old_again = _report(db, "B2", "grid", cutoff=datetime(2026, 11, 10, 0, 0))
        finally:
            for row in (
                db.execute(
                    select(DemandReading).where(
                        DemandReading.note == "test:sampling_time"
                    )
                )
                .scalars()
                .all()
            ):
                db.delete(row)
            db.commit()
    assert before["confirmed_peak"]["slot"] != "2026-11-09T12:00:00Z"
    assert after["confirmed_peak"]["slot"] == "2026-11-09T12:00:00Z"
    assert after["confirmed_peak"]["demand_kw"] == 960.0
    # re-evaluating at the old cutoff still ignores the later arrival
    assert old_again["counts"]["unknown"] == 4


def test_no_due_cells_yields_no_contract_verdict(client):
    """A cutoff before the first settlement cell ends leaves zero evidence: the report
    must not claim "not exceeded"."""
    client.post("/api/demand/acceptance/reset", json={})
    with _db() as db:
        detail = _report(db, "B1", "grid", cutoff=datetime(2026, 11, 9, 0, 10))
    assert detail["counts"]["slots"] == 0
    assert detail["contract_status"] == "not_proven"
    assert detail["contract_basis"] == "no_due_cells"
    assert "不能给出合同结论" in detail["conclusion"]


def test_cells_after_cutoff_are_not_unknown(client):
    client.post("/api/demand/acceptance/reset", json={})
    with _db() as db:
        partial = _report(db, "B1", "grid", cutoff=datetime(2026, 11, 9, 12, 0))
    assert partial["counts"]["slots"] == 48
    assert partial["counts"]["unknown"] == 0
    # cells after the cutoff are simply not due yet, and the peak is that of the
    # evidence actually available (the morning peak, not the afternoon one)
    assert partial["confirmed_peak"]["slot"].startswith("2026-11-09T08")
    assert partial["confirmed_peak"]["demand_kw"] < 810


def test_report_versions_record_peak_conclusion_change(client):
    client.post("/api/demand/acceptance/reset", json={})
    with _db() as db:
        meter = (
            db.execute(select(Meter).where(Meter.code == "M-DMD-B2-01"))
            .scalars()
            .one()
        )
        for i in range(4):
            start = datetime(2026, 11, 9, 12, 0) + timedelta(minutes=15 * i)
            db.add(
                DemandReading(
                    meter_id=meter.id,
                    kind="energy",
                    stream="settlement",
                    measured_from=start,
                    measured_to=start + timedelta(minutes=15),
                    value=960.0 * 0.25,
                    received_at=datetime(2026, 11, 10, 2, 0),
                    note="test:versions",
                )
            )
        db.commit()
        demand_service.publish_all(db, PS, PE, datetime(2026, 11, 10, 0, 0))
        db.commit()
        demand_service.publish_all(db, PS, PE, datetime(2026, 11, 10, 2, 0))
        db.commit()
        latest = demand_service.latest_report(db, "B2", "grid", PS, PE)
        for row in (
            db.execute(
                select(DemandReading).where(DemandReading.note == "test:versions")
            )
            .scalars()
            .all()
        ):
            db.delete(row)
        db.commit()
    assert latest.revision == 1
    delta = (latest.details or {})["delta"]
    assert delta["conclusion_changed"] is True
    assert delta["contract_status_before"] == "not_proven"
    assert delta["contract_status_after"] == "exceeded"
    assert delta["peak_slot_after"] == "2026-11-09T12:00:00Z"
    assert delta["confirmed_peak_kw_after"] == 960.0


def test_reset_removes_late_acceptance_readings(client):
    client.post("/api/demand/acceptance/run", json={})
    resp = client.post("/api/demand/acceptance/reset", json={})
    assert resp.status_code == 200
    with _db() as db:
        late = (
            db.execute(
                select(DemandReading).where(
                    DemandReading.note == "验收：迟到补传的高峰读数"
                )
            )
            .scalars()
            .all()
        )
    assert late == []
