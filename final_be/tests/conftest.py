"""Shared pytest fixtures: a fresh SQLite DB per test, wired the same way
main.py's startup event wires it (create tables + seed), via TestClient's
context-manager lifespan support."""
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.setenv("ADMIN_INST_ID", "admin1")
    monkeypatch.setenv("ADMIN_PASSWORD", "Pass@123")
    # Reload modules that cache DATABASE_URL/engine at import time, so each
    # test gets a genuinely fresh DB rather than reusing a previous test's.
    # IMPORTANT: must delete the bare package names too (e.g. "db", not
    # just "db.database") — leaving a stale parent package object in
    # sys.modules while its submodule gets reimported fresh causes the
    # parent's cached attribute to keep pointing at the OLD submodule in
    # some import paths, which silently resurrected the previous test's
    # engine/Base/table-registry (this caused real, hard-to-diagnose
    # cross-test leakage — e.g. rate-limit counters "surviving" into a
    # supposedly fresh test — until this was fixed).
    prefixes = ("db.", "models.", "routers.", "core.", "schemas.")
    bare = {"db", "models", "routers", "core", "schemas", "main", "seed"}
    for mod in list(sys.modules):
        if mod.startswith(prefixes) or mod in bare:
            del sys.modules[mod]
    import main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as c:
        yield c


@pytest.fixture()
def admin_headers(client):
    r = client.post("/api/auth/login", json={"credential": "admin1", "password": "Pass@123", "role": "admin"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def student_headers(client, admin_headers):
    r = client.post("/api/auth/register", json={
        "full_name": "Test Student", "inst_id": "STU100", "email": "stu100@tmu.ac.in",
        "password": "Pass@1234", "role": "student", "branch": "CSE", "section": "A", "semester": "3rd",
    })
    assert r.status_code == 201, r.text
    r = client.patch("/api/users/STU100/status", json={"status": "active"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    r = client.post("/api/auth/login", json={"credential": "STU100", "password": "Pass@1234", "role": "student"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def faculty_headers(client, admin_headers):
    r = client.post("/api/users", json={
        "full_name": "Dr Test", "inst_id": "FAC100", "email": "fac100@tmu.ac.in",
        "password": "Pass@1234", "role": "faculty", "branch": "CSE", "status": "active",
    }, headers=admin_headers)
    assert r.status_code == 201, r.text
    r = client.post("/api/auth/login", json={"credential": "FAC100", "password": "Pass@1234", "role": "faculty"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}
