"""Auth flow tests — login, registration, rate limiting, and the
self-edit privilege-escalation fix (students can't PATCH their own
branch/section/semester/course)."""


def test_admin_seed_login(client):
    r = client.post("/api/auth/login", json={"credential": "admin1", "password": "Pass@123", "role": "admin"})
    assert r.status_code == 200
    assert r.json()["user"]["role"] == "admin"


def test_wrong_password_rejected(client):
    r = client.post("/api/auth/login", json={"credential": "admin1", "password": "wrong", "role": "admin"})
    assert r.status_code == 401


def test_public_registration_always_creates_student(client):
    """Public /register must ignore any other role requested — faculty/
    admin accounts can only be created by an admin via POST /users."""
    r = client.post("/api/auth/register", json={
        "full_name": "Sneaky", "inst_id": "SNEAKY1", "email": "sneaky@tmu.ac.in",
        "password": "Pass@1234", "role": "faculty",
    })
    assert r.status_code == 201
    assert r.json()["role"] == "student"


def test_pending_student_cannot_login(client):
    client.post("/api/auth/register", json={
        "full_name": "Pending Guy", "inst_id": "PEND1", "email": "pend1@tmu.ac.in",
        "password": "Pass@1234", "role": "student",
    })
    r = client.post("/api/auth/login", json={"credential": "PEND1", "password": "Pass@1234", "role": "student"})
    assert r.status_code == 403


def test_login_rate_limit_kicks_in(client):
    for _ in range(5):
        r = client.post("/api/auth/login", json={"credential": "admin1", "password": "wrong", "role": "admin"})
        assert r.status_code == 401
    # 6th attempt (same IP+credential pair) should be rate-limited
    r = client.post("/api/auth/login", json={"credential": "admin1", "password": "wrong", "role": "admin"})
    assert r.status_code == 429


def test_rate_limit_is_per_credential_not_global(client):
    """BUG FIX regression test: the old rate limiter was keyed by IP
    alone, so failing five logins on one account would lock out every
    other account on the same network too."""
    for _ in range(5):
        client.post("/api/auth/login", json={"credential": "admin1", "password": "wrong", "role": "admin"})
    # A different account, same test client (same "IP"), should be unaffected.
    r = client.post("/api/auth/login", json={"credential": "nobody", "password": "wrong", "role": "student"})
    assert r.status_code == 401  # not 429 — rejected for bad creds, not rate-limited


def test_student_cannot_self_promote_via_me(client, student_headers):
    """BUG FIX regression test: a student used to be able to PATCH their
    own branch/section/semester via /auth/me with no admin involvement."""
    r = client.get("/api/auth/me", headers=student_headers)
    before = r.json()
    r = client.patch("/api/auth/me", json={
        "semester": "8th", "section": "Z", "branch": "ECE", "full_name": "New Name",
    }, headers=student_headers)
    assert r.status_code == 200
    after = r.json()
    assert after["semester"] == before["semester"]
    assert after["section"] == before["section"]
    assert after["branch"] == before["branch"]
    assert after["full_name"] == "New Name"  # contact-info field — allowed


def test_student_cannot_self_promote_via_users_endpoint(client, student_headers):
    r = client.patch("/api/users/STU100", json={"semester": "8th", "section": "Z"}, headers=student_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["semester"] != "8th"
    assert body["section"] != "Z"


def test_admin_can_edit_academic_fields(client, student_headers, admin_headers):
    r = client.patch("/api/users/STU100", json={"semester": "5th", "section": "B"}, headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["semester"] == "5th"
    assert r.json()["section"] == "B"


def test_student_cannot_edit_other_students(client, student_headers, admin_headers):
    client.post("/api/auth/register", json={
        "full_name": "Other Student", "inst_id": "STU200", "email": "stu200@tmu.ac.in",
        "password": "Pass@1234", "role": "student",
    })
    r = client.patch("/api/users/STU200", json={"full_name": "Hacked"}, headers=student_headers)
    assert r.status_code == 403
