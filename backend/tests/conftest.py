from __future__ import annotations

import os
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

TEST_DB = os.getenv(
    "TEST_DATABASE_URL", "postgresql+psycopg://ccdev:ccdev@127.0.0.1:5432/ccdev_test"
)
ADMIN_URL = "postgresql+psycopg://ccdev:ccdev@127.0.0.1:5432/ccdev"


def _ensure_test_database() -> None:
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.execute(
            text("select 1 from pg_database where datname = 'ccdev_test'")
        ).scalar()
        if not exists:
            conn.execute(text("create database ccdev_test"))


@pytest.fixture(scope="session")
def engine():
    _ensure_test_database()
    eng = create_engine(TEST_DB, pool_pre_ping=True)
    with eng.begin() as conn:
        conn.execute(text("drop schema public cascade"))
        conn.execute(text("create schema public"))
    yield eng
    eng.dispose()


@pytest.fixture(scope="session")
def app_client(engine):
    os.environ["DATABASE_URL"] = TEST_DB
    from app import db as db_module

    db_module.engine = engine
    db_module.SessionLocal = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, class_=Session
    )

    from app.main import app

    from app.db import init_db

    init_db()
    with TestClient(app) as client:
        yield client


@pytest.fixture(scope="session")
def client(app_client):
    return app_client


def api(client, method: str, path: str, **kwargs):
    resp = getattr(client, method)(path, **kwargs)
    assert resp.status_code < 500, resp.text
    return resp


@pytest.fixture()
def acceptance(client):
    resp = client.post("/api/acceptance/run", json={})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    return data


def step(data, key: str) -> dict:
    for item in data["steps"]:
        if item["step"] == key:
            return item
    raise KeyError(key)


def window_obs(observation: dict, start_prefix: str = "2026-11-10T00") -> dict:
    for w in observation["windows"]:
        if w["window_start"].startswith(start_prefix):
            return w
    raise KeyError(start_prefix)


def consumer_status(window: dict, code: str) -> dict:
    for c in window["consumers"]:
        if c["consumer"] == code:
            return c
    raise KeyError(code)


def source_status(window: dict, code: str) -> dict:
    for s in window["sources"]:
        if s["source"] == code:
            return s
    raise KeyError(code)
