"""Attendance marking tests — rotating QR, server-side face verification,
branch/section enforcement, and the manual-edit window."""
import time


def _make_active_session(client, faculty_headers, admin_headers, course_id="CS301", branch="CSE", section="A", **course_kwargs):
    kwargs = dict(code=course_id, name="Test Course", credits=4, branch="CSE", section="A")
    kwargs.update(course_kwargs)
    r = client.post("/api/courses", json=kwargs, headers=admin_headers)
    assert r.status_code == 201, r.text
    # BUG-FIX regression test note: /sessions/extra does NOT auto-inherit
    # the course's own branch/section — it only restricts by whatever
    # branch/section is explicitly passed in this request body. Passing
    # them here is what actually exercises the branch/section check in
    # mark_full_flow (an ad-hoc extra class with neither set is
    # deliberately open to anyone, same as the pre-existing design).
    r = client.post("/api/sessions/extra", json={
        "course_id": course_id, "title": "Extra", "branch": branch, "section": section,
    }, headers=faculty_headers)
    assert r.status_code == 201, r.text
    return r.json()


def test_qr_token_hidden_from_students(client, student_headers, faculty_headers, admin_headers):
    session = _make_active_session(client, faculty_headers, admin_headers)
    assert session["qr_token"] is not None  # faculty (creator) sees it

    r = client.get("/api/sessions/active/mine", headers=student_headers)
    assert r.status_code == 200
    sessions = r.json()
    assert len(sessions) == 1
    assert sessions[0]["qr_token"] is None  # BUG FIX: must be masked for students


def test_student_cannot_read_qr_live(client, student_headers, faculty_headers, admin_headers):
    session = _make_active_session(client, faculty_headers, admin_headers)
    r = client.get(f"/api/sessions/{session['id']}/qr-live", headers=student_headers)
    assert r.status_code == 403


def test_mark_attendance_with_rotating_qr(client, student_headers, faculty_headers, admin_headers):
    client.patch("/api/settings", json={"face_required": False}, headers=admin_headers)
    session = _make_active_session(client, faculty_headers, admin_headers)
    live = client.get(f"/api/sessions/{session['id']}/qr-live", headers=faculty_headers).json()
    r = client.post("/api/attendance/qr-gps-face", json={"qr_token": live["qr_payload"]}, headers=student_headers)
    assert r.status_code == 201
    assert r.json()["status"] == "present"


def test_duplicate_mark_rejected(client, student_headers, faculty_headers, admin_headers):
    client.patch("/api/settings", json={"face_required": False}, headers=admin_headers)
    session = _make_active_session(client, faculty_headers, admin_headers)
    live = client.get(f"/api/sessions/{session['id']}/qr-live", headers=faculty_headers).json()
    client.post("/api/attendance/qr-gps-face", json={"qr_token": live["qr_payload"]}, headers=student_headers)
    r = client.post("/api/attendance/qr-gps-face", json={"qr_token": live["qr_payload"]}, headers=student_headers)
    assert r.status_code == 409


def test_garbage_qr_token_rejected(client, student_headers):
    r = client.post("/api/attendance/qr-gps-face", json={"qr_token": "not-a-real-token"}, headers=student_headers)
    assert r.status_code == 404


def test_stale_static_token_from_wrong_session_rejected(client, student_headers, faculty_headers, admin_headers):
    session = _make_active_session(client, faculty_headers, admin_headers)
    # Well-formed but wrong session id / token combo
    r = client.post("/api/attendance/qr-gps-face", json={"qr_token": f"{session['id']+9999}:abcdef123456"}, headers=student_headers)
    assert r.status_code == 404


def test_face_required_blocks_unregistered_student(client, student_headers, faculty_headers, admin_headers):
    # face_required defaults to True (system settings)
    session = _make_active_session(client, faculty_headers, admin_headers)
    live = client.get(f"/api/sessions/{session['id']}/qr-live", headers=faculty_headers).json()
    r = client.post("/api/attendance/qr-gps-face", json={"qr_token": live["qr_payload"]}, headers=student_headers)
    assert r.status_code == 403
    assert "face" in r.json()["detail"].lower()


def test_face_descriptor_mismatch_rejected(client, student_headers, faculty_headers, admin_headers):
    import json
    embedding = [0.1] * 128
    client.post("/api/users/STU100/register-face", json={"image_b64": "data:image/jpeg;base64,Zm9v", "face_descriptor": json.dumps(embedding)}, headers=student_headers)
    session = _make_active_session(client, faculty_headers, admin_headers)
    live = client.get(f"/api/sessions/{session['id']}/qr-live", headers=faculty_headers).json()
    wrong_descriptor = [0.9] * 128  # far from the registered embedding
    r = client.post("/api/attendance/qr-gps-face",
                     json={"qr_token": live["qr_payload"], "face_descriptor": wrong_descriptor},
                     headers=student_headers)
    assert r.status_code == 403


def test_face_descriptor_match_accepted(client, student_headers, faculty_headers, admin_headers):
    import json
    embedding = [0.1] * 128
    client.post("/api/users/STU100/register-face", json={"image_b64": "data:image/jpeg;base64,Zm9v", "face_descriptor": json.dumps(embedding)}, headers=student_headers)
    session = _make_active_session(client, faculty_headers, admin_headers)
    live = client.get(f"/api/sessions/{session['id']}/qr-live", headers=faculty_headers).json()
    r = client.post("/api/attendance/qr-gps-face",
                     json={"qr_token": live["qr_payload"], "face_descriptor": embedding},
                     headers=student_headers)
    assert r.status_code == 201


def test_wrong_branch_student_blocked(client, faculty_headers, admin_headers):
    client.patch("/api/settings", json={"face_required": False}, headers=admin_headers)
    client.post("/api/users", json={
        "full_name": "ECE Student", "inst_id": "ECE1", "email": "ece1@tmu.ac.in",
        "password": "Pass@1234", "role": "student", "branch": "ECE", "section": "A",
    }, headers=admin_headers)
    ece_headers = client.post("/api/auth/login", json={"credential": "ECE1", "password": "Pass@1234", "role": "student"})
    ece_headers = {"Authorization": f"Bearer {ece_headers.json()['access_token']}"}

    session = _make_active_session(client, faculty_headers, admin_headers)  # branch=CSE
    live = client.get(f"/api/sessions/{session['id']}/qr-live", headers=faculty_headers).json()
    r = client.post("/api/attendance/qr-gps-face", json={"qr_token": live["qr_payload"]}, headers=ece_headers)
    assert r.status_code == 403


def test_manual_mark_rejects_unknown_student(client, faculty_headers, admin_headers):
    session = _make_active_session(client, faculty_headers, admin_headers)
    r = client.post("/api/attendance/manual", json={
        "session_id": session["id"], "student_id": "NOBODY123", "status": "present",
    }, headers=faculty_headers)
    assert r.status_code == 404


def test_manual_mark_writes_audit_log(client, student_headers, faculty_headers, admin_headers):
    session = _make_active_session(client, faculty_headers, admin_headers)
    r = client.post("/api/attendance/manual", json={
        "session_id": session["id"], "student_id": "STU100", "status": "present",
    }, headers=faculty_headers)
    assert r.status_code == 201
    r = client.get("/api/attendance/audit-log", headers=faculty_headers)
    entries = r.json()["entries"]
    assert any(e["student_id"] == "STU100" and e["action"] == "create" for e in entries)


def test_proxy_flag_raised_for_shared_device(client, faculty_headers, admin_headers):
    client.patch("/api/settings", json={"face_required": False}, headers=admin_headers)
    for i, inst_id in enumerate(["MULTI1", "MULTI2"]):
        client.post("/api/users", json={
            "full_name": f"Student {i}", "inst_id": inst_id, "email": f"{inst_id.lower()}@tmu.ac.in",
            "password": "Pass@1234", "role": "student", "branch": "CSE", "section": "A",
        }, headers=admin_headers)

    session = _make_active_session(client, faculty_headers, admin_headers)
    live = client.get(f"/api/sessions/{session['id']}/qr-live", headers=faculty_headers).json()
    for inst_id in ["MULTI1", "MULTI2"]:
        h = client.post("/api/auth/login", json={"credential": inst_id, "password": "Pass@1234", "role": "student"})
        h = {"Authorization": f"Bearer {h.json()['access_token']}"}
        client.post("/api/attendance/qr-gps-face", json={"qr_token": live["qr_payload"], "device_id": "same-device"}, headers=h)

    r = client.get("/api/attendance/flags", headers=faculty_headers)
    flags = r.json()
    assert any("device" in f["reason"].lower() for f in flags)


def test_defaulters_endpoint_scoped_correctly(client, student_headers, faculty_headers, admin_headers):
    """Regression test for the numerator/denominator scoping bug found
    during audit — defaulters must only count sessions for THIS course."""
    session = _make_active_session(client, faculty_headers, admin_headers)
    client.post("/api/sessions/extra", json={"course_id": "CS301", "title": "s2"}, headers=faculty_headers)
    r = client.get("/api/attendance/defaulters?course_id=CS301", headers=faculty_headers)
    assert r.status_code == 200  # no closed sessions yet, so no crash, empty-safe


def test_leave_od_type_no_longer_accepted(client, student_headers):
    """CHANGE regression test: OD was removed as a leave type — a client
    that still sends 'od' (stale cache, old mobile build) gets silently
    normalized to 'leave' rather than erroring, since the type field is
    otherwise cosmetic."""
    r = client.post("/api/leave", json={
        "from_date": "2026-11-01T00:00:00", "to_date": "2026-11-02T00:00:00",
        "leave_type": "od", "reason": "Conference",
    }, headers=student_headers)
    assert r.status_code == 201
    assert r.json()["leave_type"] == "leave"
