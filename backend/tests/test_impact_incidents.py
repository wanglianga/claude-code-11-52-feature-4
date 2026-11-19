"""Consumer impact and incident lifecycle rules."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from app.models import Consumer, ConsumerImpact, Incident, RecoveryCheck
from app.services.impact import compute_consumer_impact
from app.services.incidents import attempt_close

from .conftest import consumer_status, source_status, step, window_obs

DAY = datetime(2026, 11, 10, 0, 0)


def _db():
    from app.db import SessionLocal

    return SessionLocal()


def _consumer(db, code):
    return db.execute(select(Consumer).where(Consumer.code == code)).scalars().one()


def test_same_version_different_tolerance_different_impact(acceptance):
    step_data = step(acceptance, "tolerance_split")
    window = window_obs(step_data["observation"])
    assert consumer_status(window, "property_dashboard")["status"] == "ok"
    settlement = consumer_status(window, "settlement_billing")
    assert settlement["status"] == "unavailable"
    assert settlement["affected_slots"] > 0
    # assessment only depends on B1/B2 electricity and PV, so B4 has no effect
    assert consumer_status(window, "energy_assessment")["status"] == "ok"
    assert settlement["thresholds"]["tolerance_minutes"] == 30
    assert consumer_status(window, "property_dashboard")["thresholds"]["tolerance_minutes"] == 60


def test_missing_meter_does_not_disable_unrelated_building(acceptance):
    data = step(acceptance, "dependency_branch")
    window = window_obs(data["observation"])
    pv = source_status(window, "pv")
    assert pv["coverage_num"] < pv["coverage_den"]
    property_dash = consumer_status(window, "property_dashboard")
    # property only depends on electricity/water, so a PV gap cannot touch it
    assert property_dash["status"] == "ok"
    assessment = consumer_status(window, "energy_assessment")
    assert assessment["status"] in ("unavailable", "restricted")
    buildings = {b for c in window["consumers"] for b in (c.get("affected_buildings") or [])}
    assert "B2" not in buildings or property_dash["status"] == "ok"


def test_consumer_with_multiple_products_merges_ranges_once(client):
    """A consumer depending on two broken products must not double count one gap."""
    client.post("/api/acceptance/reset", json={})
    from app.models import ConsumerDependency, Source
    from app.services.simulator import get_clock

    with _db() as db:
        consumer = _consumer(db, "property_dashboard")
        # make it also depend on steam, then break steam and water on the same slot
        db.add(
            ConsumerDependency(
                consumer_id=consumer.id,
                product_code="energy.realtime",
                product_field="steam",
                building_code=None,
                valid_from=datetime(2026, 1, 1),
            )
        )
        db.commit()
        for field, code in (("water", "M-WAT-B1-01"), ("steam", "M-STM-B2-01")):
            source = db.execute(select(Source).where(Source.code == field)).scalars().one()
            from app.models import Meter, Sample

            meter = db.execute(select(Meter).where(Meter.code == code)).scalars().one()
            for row in (
                db.execute(
                    select(Sample).where(
                        Sample.meter_id == meter.id,
                        Sample.slot_start == DAY + timedelta(hours=1),
                    )
                )
                .scalars()
                .all()
            ):
                db.delete(row)
        db.commit()
        client.post("/api/health/reevaluate", json={})
        impact = compute_consumer_impact(
            db, consumer, DAY, DAY + timedelta(hours=6), DAY + timedelta(hours=7), persist=False
        )
        # two different fields broke the same slot: still counted once
        slots = [s for r in impact["ranges"] for s in r["slots"]]
        assert len(slots) == len(set(slots))
        assert impact["affected_slots"] == 1


def test_threshold_preview_changes_verdict_not_facts(client):
    client.post("/api/acceptance/run", json={})
    resp = client.post(
        "/api/consumers/property_dashboard/preview",
        json={
            "window_start": "2026-11-10T12:00:00",
            "tolerance_minutes": 600,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["facts_unchanged"] is True
    assert data["changed"] is True
    assert data["preview"]["affected_slots"] < data["current"]["affected_slots"]


def test_close_requires_recovery_check(client):
    client.post("/api/acceptance/reset", json={})
    with _db() as db:
        source_id = db.execute(select(Incident)).scalars().first()
    # create a fresh incident by dropping a slot
    client.post(
        "/api/sim/action",
        json={
            "action": "drop",
            "meter": "M-WAT-B1-01",
            "from": "2026-11-10T02:00:00",
            "to": "2026-11-10T02:30:00",
        },
    )
    incidents = client.get("/api/incidents").json()
    open_inc = next(i for i in incidents if i["status"] in ("open", "recovering", "reopened"))
    resp = client.post(f"/api/incidents/{open_inc['id']}/close", json={})
    body = resp.json()
    assert body["ok"] is False
    assert "恢复评估" in body["error"]


def test_close_blocked_when_new_gap_appears(acceptance):
    data = step(acceptance, "close_blocked_new_gap")
    assert data["detail"]["close_result"]["ok"] is False
    assert data["detail"]["incident_status"] in ("open", "reopened")
    assert "新的未解决缺口" in data["detail"]["close_result"]["error"]


def test_recurrence_creates_new_linked_incident(acceptance):
    data = step(acceptance, "recurrence_after_recovery")
    detail = data["detail"]
    assert detail["new_incident"] != detail["prior_incident_code"]
    assert detail["prior_incident_id"] is not None
    assert detail["prior_incident_closed"] == "closed"
    window = window_obs(data["observation"], "2026-11-10T12")
    electricity = source_status(window, "electricity")
    assert electricity["status"] == "breached"


def test_incident_ranges_exclude_healthy_gap_between(acceptance):
    data = step(acceptance, "recurrence_after_recovery")
    incidents = data["observation"]["incidents"]
    new_inc = next(i for i in incidents if i["code"] == data["detail"]["new_incident"])
    slots = [s for r in new_inc["ranges"] for s in r["slots"]]
    # the recurrence is 13:00-14:00, nothing before 13:00 belongs to it
    assert all(s.startswith("2026-11-10T13:") for s in slots)


def test_missing_product_consumer_waits(client):
    client.post("/api/acceptance/reset", json={})
    from app.models import ConsumerDependency

    with _db() as db:
        consumer = _consumer(db, "energy_assessment")
        db.add(
            ConsumerDependency(
                consumer_id=consumer.id,
                product_code="energy.daily",
                product_field="nonexistent",
                building_code=None,
                valid_from=datetime(2026, 1, 1),
            )
        )
        db.commit()
        impact = compute_consumer_impact(
            db, consumer, DAY, DAY + timedelta(hours=6), DAY + timedelta(hours=6), persist=False
        )
        assert impact["status"] == "waiting"
        assert any(r["kind"] == "missing_product" for r in impact["reasons"])


def test_new_gap_in_another_window_also_blocks_close(client):
    """The close guard must look at every window the incident covers, not only the
    window the recovery check happened to run on."""
    client.post("/api/acceptance/run", json={})
    incidents = client.get("/api/incidents").json()
    elec = next(i for i in incidents if i["source_code"] == "electricity" and i["status"] != "closed")
    # repair everything currently open is out of scope; instead verify the guard reacts
    # to a gap created after the recovery check in a *different* completed window.
    client.post("/api/sim/clock", json={"at": "2026-11-10T21:00:00", "run": True})
    check = client.post(
        f"/api/incidents/{elec['id']}/recovery-check",
        json={"window_start": "2026-11-10T12:00:00", "cutoff": "2026-11-10T18:00:00"},
    )
    assert check.status_code == 200
    client.post(
        "/api/sim/action",
        json={
            "action": "drop",
            "meter": "M-ELE-B2-01",
            "from": "2026-11-10T13:00:00",
            "to": "2026-11-10T13:30:00",
        },
    )
    resp = client.post(f"/api/incidents/{elec['id']}/close", json={}).json()
    assert resp["ok"] is False
    assert resp["status"] in ("open", "reopened")
