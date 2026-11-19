"""Window evaluation rules: slots, maintenance, frequency splits, verification."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from app.models import (
    BreachFact,
    HealthEvaluation,
    MaintenanceWindow,
    Meter,
    MeterCommitment,
    Sample,
)
from app.services.health import evaluate_window, latest_evaluation
from app.services.ingest import generate_samples
from app.services.simulator import get_clock, run_checks

DAY = datetime(2026, 11, 10, 0, 0)


def _db():
    from app.db import SessionLocal

    return SessionLocal()


def _source(db, code: str):
    from app.models import Source

    return db.execute(select(Source).where(Source.code == code)).scalars().one()


def _meter(db, code: str):
    return db.execute(select(Meter).where(Meter.code == code)).scalars().one()


def test_expected_slots_skip_maintenance_and_beyond_cutoff(client):
    client.post("/api/acceptance/reset", json={})
    with _db() as db:
        source = _source(db, "electricity")
        ev = evaluate_window(db, source, DAY, DAY + timedelta(hours=6), DAY + timedelta(hours=6))
        details = ev.details
        # 5 meters x 24 slots = 120; B3 maintenance removes 4 slots -> 116
        assert details["coverage_den"] == 116
        assert details["coverage_num"] == 116
        assert details["counts"]["maintenance"] == 4
        assert ev.certified is True
        b3 = next(m for m in details["meters"] if m["meter_code"] == "M-ELE-B3-01")
        assert len(b3["maintenance"]) == 4
        assert b3["segments"][0]["denominator"] == 20


def test_beyond_cutoff_slots_are_not_missing_or_zero(client):
    client.post("/api/acceptance/reset", json={})
    # let meters emit up to 07:00, then evaluate the still-running 06:00-12:00 window
    from app.services.simulator import advance_clock

    with _db() as db:
        advance_clock(db, 60, run=False)
    with _db() as db:
        source = _source(db, "electricity")
        ev = evaluate_window(db, source, DAY + timedelta(hours=6), DAY + timedelta(hours=12), DAY + timedelta(hours=7))
        details = ev.details
        assert details["counts"]["missing"] == 0
        assert details["beyond_cutoff_count"] > 0
        assert details["coverage_num"] == details["coverage_den"]
        assert ev.certified is False  # window not complete yet
        assert ev.status == "pending"
        meter = details["meters"][0]
        assert any(s["status"] == "beyond_cutoff" for s in meter["slot_states"])


def test_declared_zero_is_a_valid_value(client):
    client.post("/api/acceptance/reset", json={})
    with _db() as db:
        source = _source(db, "pv")
        ev = evaluate_window(db, source, DAY, DAY + timedelta(hours=6), DAY + timedelta(hours=6))
        assert ev.details["counts"]["declared_zero"] == 24
        assert ev.details["counts"]["missing"] == 0
        meter = ev.details["meters"][0]
        assert all(s["status"] in ("covered",) for s in meter["slot_states"])


def test_duplicate_samples_occupy_one_slot(client):
    client.post("/api/acceptance/reset", json={})
    with _db() as db:
        source = _source(db, "water")
        meter = _meter(db, "M-WAT-B1-01")
        slot = DAY + timedelta(hours=1)
        db.add(
            Sample(
                meter_id=meter.id,
                slot_start=slot,
                value=99.0,
                received_at=DAY + timedelta(hours=5, minutes=45),
                is_correction=True,
            )
        )
        db.commit()
        ev = evaluate_window(db, source, DAY, DAY + timedelta(hours=6), DAY + timedelta(hours=6))
        detail = next(m for m in ev.details["meters"] if m["meter_code"] == meter.code)
        assert detail["duplicate_slots"] == 1
        # denominator unchanged and still fully covered
        assert detail["expected"] == detail["observed"] == 24


def test_frequency_change_splits_denominator(client):
    client.post("/api/acceptance/reset", json={})
    with _db() as db:
        water = _meter(db, "M-WAT-B1-01")
        c = db.execute(
            select(MeterCommitment)
            .where(MeterCommitment.meter_id == water.id)
            .order_by(MeterCommitment.valid_from.desc())
        ).scalars().first()
        c.valid_to = DAY + timedelta(hours=2)
        db.add(
            MeterCommitment(
                meter_id=water.id,
                valid_from=DAY + timedelta(hours=2),
                cadence_minutes=30,
                allowed_lateness_minutes=30,
            )
        )
        db.commit()
        generate_samples(db, DAY, DAY + timedelta(hours=6))
        db.commit()
        source = _source(db, "water")
        ev = evaluate_window(db, source, DAY, DAY + timedelta(hours=6), DAY + timedelta(hours=6))
        detail = next(m for m in ev.details["meters"] if m["meter_code"] == water.code)
        assert len(detail["segments"]) == 2
        assert [s["cadence_minutes"] for s in detail["segments"]] == [15, 30]
        # 8 slots at 15 min + 8 slots at 30 min
        assert [s["denominator"] for s in detail["segments"]] == [8, 8]
        assert detail["expected"] == 16
        assert detail["observed"] == 16


def test_retroactive_maintenance_keeps_breach_fact(client):
    client.post("/api/acceptance/reset", json={})
    with _db() as db:
        source = _source(db, "water")
        meter = _meter(db, "M-WAT-B2-01")
        start = DAY + timedelta(hours=3)
        end = DAY + timedelta(hours=3, minutes=30)
        for row in (
            db.execute(
                select(Sample).where(
                    Sample.meter_id == meter.id,
                    Sample.slot_start >= start,
                    Sample.slot_start < end,
                )
            )
            .scalars()
            .all()
        ):
            db.delete(row)
        db.commit()
        ev = evaluate_window(db, source, DAY, DAY + timedelta(hours=6), DAY + timedelta(hours=6))
        first = next(m for m in ev.details["meters"] if m["meter_code"] == meter.code)
        assert len(first["missing"]) == 2
        facts_before = (
            db.execute(
                select(BreachFact).where(
                    BreachFact.meter_id == meter.id,
                    BreachFact.slot_start >= start,
                    BreachFact.slot_start < end,
                )
            )
            .scalars()
            .all()
        )
        assert len(facts_before) == 2
        assert all(f.resolved_at is None for f in facts_before)

        # maintenance is registered only long after the breach: the denominator shrinks
        # but the facts stay and are annotated as retroactive.
        db.add(
            MaintenanceWindow(
                meter_id=meter.id,
                starts_at=start,
                ends_at=end,
                allowed_deduction=True,
                recorded_at=DAY + timedelta(hours=10),
                reason="事后补录",
            )
        )
        db.commit()
        ev2 = evaluate_window(db, source, DAY, DAY + timedelta(hours=6), DAY + timedelta(hours=6))
        second = next(m for m in ev2.details["meters"] if m["meter_code"] == meter.code)
        assert second["expected"] == 22
        assert second["observed"] == 22
        retro = [m for m in second["maintenance"] if m["retroactive"]]
        assert len(retro) == 2
        facts_after = (
            db.execute(
                select(BreachFact).where(
                    BreachFact.meter_id == meter.id,
                    BreachFact.slot_start >= start,
                    BreachFact.slot_start < end,
                    BreachFact.kind == "missing",
                )
            )
            .scalars()
            .all()
        )
        assert len(facts_after) == 2  # never erased
        assert all(f.resolved_at is not None for f in facts_after)
        assert any("事后补录" in (f.note or "") for f in facts_after)


def test_cumulative_reset_is_not_savings(client):
    client.post("/api/acceptance/reset", json={})
    with _db() as db:
        source = _source(db, "steam")
        meter = _meter(db, "M-STM-B2-01")
        row = (
            db.execute(
                select(Sample)
                .where(
                    Sample.meter_id == meter.id,
                    Sample.slot_start == DAY + timedelta(hours=4),
                )
                .order_by(Sample.received_at.desc())
            )
            .scalars()
            .first()
        )
        row.value = 1.0
        db.commit()
        ev = evaluate_window(db, source, DAY, DAY + timedelta(hours=6), DAY + timedelta(hours=6))
        detail = next(m for m in ev.details["meters"] if m["meter_code"] == meter.code)
        assert any(v["kind"] == "reset" for v in detail["verification"])
        assert ev.verification_ok is False
        assert ev.status == "breached"


def test_recompute_cache_reuses_revision_when_result_unchanged(client):
    client.post("/api/acceptance/reset", json={})
    with _db() as db:
        source = _source(db, "electricity")
        ws, we = DAY, DAY + timedelta(hours=6)
        cutoff = DAY + timedelta(hours=6)
        ev1 = evaluate_window(db, source, ws, we, cutoff)
        db.commit()
        rev1 = ev1.revision
        count1 = ev1.recompute_count
        ev2 = evaluate_window(db, source, ws, we, cutoff)
        db.commit()
        assert ev2.id == ev1.id
        assert ev2.revision == rev1
        assert ev2.recompute_count > count1


def test_late_arrival_updates_curve_and_completeness_together(client):
    """A retransmitted sample must move both the product output and the health facts —
    it can never refresh the curve while leaving completeness stale."""
    client.post("/api/acceptance/reset", json={})
    from app.models import ProductRun
    from app.services.products import latest_product

    with _db() as db:
        source = _source(db, "water")
        meter = _meter(db, "M-WAT-B1-01")
        slot = DAY + timedelta(hours=2)
        for row in (
            db.execute(
                select(Sample).where(
                    Sample.meter_id == meter.id,
                    Sample.slot_start == slot,
                )
            )
            .scalars()
            .all()
        ):
            db.delete(row)
        db.commit()
        ev = evaluate_window(db, source, DAY, DAY + timedelta(hours=6), DAY + timedelta(hours=6))
        run_before = latest_product(db, "energy.realtime", "water", DAY, DAY + timedelta(hours=6))
        series_before = len(run_before.output["series"])
        assert ev.coverage_num < ev.coverage_den
        assert any(x["slot"].startswith("2026-11-10T02:00") for x in ev.details["missing"])
        covered_before = ev.coverage_num
        value_before = run_before.output["total"]

        # retransmission at 06:30, inside a fresh evaluation
        db.add(
            Sample(
                meter_id=meter.id,
                slot_start=slot,
                value=7.5,
                received_at=DAY + timedelta(hours=6, minutes=30),
                is_correction=True,
            )
        )
        db.commit()
        from app.services.simulator import run_checks

        run_checks(db, DAY + timedelta(hours=6, minutes=30))
        ev2 = latest_evaluation(db, source.id, DAY, DAY + timedelta(hours=6))
        run_after = latest_product(db, "energy.realtime", "water", DAY, DAY + timedelta(hours=6))
        assert ev2.coverage_num == ev2.coverage_den == covered_before + 1
        # series is per-slot (aggregated over buildings), so its length is unchanged;
        # the value at the repaired slot and the window total must both move.
        slots_after = {s["slot"]: s["value"] for s in run_after.output["series"]}
        assert slots_after["2026-11-10T02:00:00Z"] > 0
        assert run_after.output["total"] > value_before
        assert run_after.data_cutoff >= DAY + timedelta(hours=6, minutes=30)


def test_installation_gap_splits_expected_slots(client):
    """A meter that leaves and re-enters service mid-window contributes only the slots
    its valid installation periods cover; the gap is not missing data."""
    client.post("/api/acceptance/reset", json={})
    with _db() as db:
        water = _meter(db, "M-WAT-B4-01")
        current = db.execute(
            select(MeterCommitment)
            .where(MeterCommitment.meter_id == water.id)
            .order_by(MeterCommitment.valid_from.desc())
        ).scalars().first()
        current.valid_to = DAY + timedelta(hours=3)
        db.add(
            MeterCommitment(
                meter_id=water.id,
                valid_from=DAY + timedelta(hours=4),
                cadence_minutes=15,
                allowed_lateness_minutes=30,
            )
        )
        db.commit()
        source = _source(db, "water")
        ev = evaluate_window(db, source, DAY, DAY + timedelta(hours=6), DAY + timedelta(hours=6))
        detail = next(m for m in ev.details["meters"] if m["meter_code"] == water.code)
        assert len(detail["segments"]) == 2
        assert [s["denominator"] for s in detail["segments"]] == [12, 8]
        assert detail["expected"] == 20
        assert detail["observed"] == 20
        assert detail["missing"] == []
        assert ev.certified is True


def test_meter_replacement_with_unbridgeable_ratio_is_a_verification_gap(client):
    """A replaced meter whose first reading cannot be bridged by the declared ratio is a
    verification gap, not zero consumption and not a saving."""
    client.post("/api/acceptance/reset", json={})
    from app.models import Building

    with _db() as db:
        source = _source(db, "electricity")
        old = _meter(db, "M-ELE-B1-02")
        building = db.execute(select(Building).where(Building.code == "B1")).scalars().one()
        new = Meter(
            code="M-ELE-B1-02R",
            name="研发楼 2# 进线（换表后）",
            source_id=source.id,
            building_id=building.id,
            cumulative=True,
            multiplier=1.0,
            linked_from_meter_id=old.id,
            link_ratio=1.0,
        )
        db.add(new)
        db.flush()
        # old meter stops at 01:45 with reading 10000; new meter starts at 02:00 at 5000
        for row in (
            db.execute(
                select(Sample).where(
                    Sample.meter_id == old.id,
                    Sample.slot_start >= DAY + timedelta(hours=2),
                )
            )
            .scalars()
            .all()
        ):
            db.delete(row)
        db.add_all(
            [
                Sample(
                    meter_id=old.id,
                    slot_start=DAY + timedelta(hours=1, minutes=45),
                    value=10000.0,
                    received_at=DAY + timedelta(hours=1, minutes=45),
                ),
                Sample(
                    meter_id=new.id,
                    slot_start=DAY + timedelta(hours=2),
                    value=5000.0,
                    received_at=DAY + timedelta(hours=2),
                ),
                Sample(
                    meter_id=new.id,
                    slot_start=DAY + timedelta(hours=2, minutes=15),
                    value=5030.0,
                    received_at=DAY + timedelta(hours=2, minutes=15),
                ),
            ]
        )
        db.add(
            MeterCommitment(
                meter_id=new.id,
                valid_from=DAY + timedelta(hours=2),
                cadence_minutes=15,
                allowed_lateness_minutes=30,
            )
        )
        db.commit()
        ev = evaluate_window(db, source, DAY, DAY + timedelta(hours=6), DAY + timedelta(hours=6))
        detail = next(m for m in ev.details["meters"] if m["meter_code"] == new.code)
        kinds = [v["kind"] for v in detail["verification"]]
        assert "ratio" in kinds
        assert ev.verification_ok is False
        # only the following normal delta is counted, never a negative "saving"
        values = [v for v in detail["consumption"]["by_slot"].values() if v is not None]
        assert all(v >= 0 for v in values)


def test_verification_fact_uses_real_slot_length_after_frequency_change(client):
    """With a 30-minute commitment the verification fact must span 30 minutes, not 15."""
    client.post("/api/acceptance/reset", json={})
    with _db() as db:
        steam = _meter(db, "M-STM-B2-01")
        current = db.execute(
            select(MeterCommitment)
            .where(MeterCommitment.meter_id == steam.id)
            .order_by(MeterCommitment.valid_from.desc())
        ).scalars().first()
        current.valid_to = DAY + timedelta(hours=1)
        db.add(
            MeterCommitment(
                meter_id=steam.id,
                valid_from=DAY + timedelta(hours=1),
                cadence_minutes=30,
                allowed_lateness_minutes=30,
            )
        )
        row = (
            db.execute(
                select(Sample)
                .where(
                    Sample.meter_id == steam.id,
                    Sample.slot_start == DAY + timedelta(hours=2),
                )
            )
            .scalars()
            .first()
        )
        row.value = 1.0  # force a reset on a 30-minute slot
        db.commit()
        source = _source(db, "steam")
        ev = evaluate_window(db, source, DAY, DAY + timedelta(hours=6), DAY + timedelta(hours=6))
        facts = (
            db.execute(
                select(BreachFact).where(
                    BreachFact.meter_id == steam.id,
                    BreachFact.kind == "verification",
                )
            )
            .scalars()
            .all()
        )
        assert facts
        assert (facts[0].slot_end - facts[0].slot_start).total_seconds() == 30 * 60
