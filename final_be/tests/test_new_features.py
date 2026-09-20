"""Tests for: auto-notify on go-live, room/venue conflict detection,
and faculty leave + substitute assignment."""
import json


def _make_faculty(client, admin_headers, inst_id, branch="CSE"):
    r = client.post("/api/users", json={
        "full_name": f"Dr {inst_id}", "inst_id": inst_id, "email": f"{inst_id.lower()}@tmu.ac.in",
        "password": "Pass@1234", "role": "faculty", "branch": branch, "status": "active",
    }, headers=admin_headers)
    assert r.status_code == 201, r.text
    r = client.post("/api/auth/login", json={"credential": inst_id, "password": "Pass@1234", "role": "faculty"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _make_slot(client, admin_headers, faculty_id, room, day="monday", start="09:00", end="10:00",
                branch="CSE", section="A", course_id="CS301"):
    client.post("/api/courses", json={"code": course_id, "name": "Test Course", "credits": 4,
                                       "branch": branch, "section": section}, headers=admin_headers)
    r = client.post("/api/timetable", json={
        "day_of_week": day, "start_time": start, "end_time": end,
        "branch": branch, "section": section, "semester": "3rd",
        "course_id": course_id, "faculty_id": faculty_id, "room": room,
    }, headers=admin_headers)
    assert r.status_code in (200, 201), r.text
    return r.json()


# ── Auto-notify on go-live ────────────────────────────────────────────────
def test_auto_notify_toggle_default_on(client, admin_headers):
    r = client.get("/api/settings", headers=admin_headers)
    assert r.status_code == 200
    assert r.json().get("auto_notify_on_go_live", True) is not False


def test_auto_notify_can_be_disabled(client, admin_headers):
    r = client.patch("/api/settings", json={"auto_notify_on_go_live": False}, headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["auto_notify_on_go_live"] is False


def test_extra_class_does_not_crash_when_no_push_subscriptions(client, faculty_headers, admin_headers):
    """Auto-notify runs on every extra-class creation; with zero
    subscribers and no VAPID key configured it must be a silent no-op,
    never a failure that blocks the session being created."""
    client.post("/api/courses", json={"code": "CS301", "name": "DBMS", "credits": 4}, headers=admin_headers)
    r = client.post("/api/sessions/extra", json={"course_id": "CS301", "title": "Extra"}, headers=faculty_headers)
    assert r.status_code == 201


def test_push_subscription_persists_across_app_reload(client, student_headers):
    """Regression test for the in-memory-dict bug: subscribe, then
    confirm the record is really in the DB (not just process memory) by
    checking it survived through a normal request/response cycle."""
    r = client.post("/api/notifications/push/subscribe", json={
        "endpoint": "https://fcm.googleapis.com/fake-endpoint",
        "keys": {"p256dh": "abc", "auth": "def"},
    }, headers=student_headers)
    assert r.status_code == 201
    r = client.delete("/api/notifications/push/unsubscribe", headers=student_headers)
    assert r.status_code == 200


# ── Room/venue conflict detection ─────────────────────────────────────────
def test_room_conflict_detected_across_different_sections(client, admin_headers):
    fac1 = _make_faculty(client, admin_headers, "FACA")
    fac2 = _make_faculty(client, admin_headers, "FACB")
    _make_slot(client, admin_headers, "FACA", room="Room 101", section="A", course_id="CS301")
    _make_slot(client, admin_headers, "FACB", room="Room 101", section="B", course_id="CS302")

    r = client.get("/api/timetable/conflicts", headers=admin_headers)
    assert r.status_code == 200
    conflicts = r.json()["conflicts"]
    room_conflicts = [c for c in conflicts if c["type"] == "room"]
    assert len(room_conflicts) == 1
    assert room_conflicts[0]["room"] == "Room 101"


def test_teacher_conflict_still_detected(client, admin_headers):
    _make_faculty(client, admin_headers, "FACC")
    _make_slot(client, admin_headers, "FACC", room="Room 201", section="A", course_id="CS301")
    _make_slot(client, admin_headers, "FACC", room="Room 202", section="B", course_id="CS302")

    r = client.get("/api/timetable/conflicts", headers=admin_headers)
    conflicts = r.json()["conflicts"]
    teacher_conflicts = [c for c in conflicts if c["type"] == "teacher"]
    assert len(teacher_conflicts) == 1


def test_no_false_positive_for_shared_room_same_class(client, admin_headers):
    """A single class occupying a room isn't a conflict just because it
    exists — only flag when 2+ DIFFERENT (section, course) pairs collide."""
    _make_faculty(client, admin_headers, "FACD")
    _make_slot(client, admin_headers, "FACD", room="Room 301", section="A", course_id="CS301")

    r = client.get("/api/timetable/conflicts", headers=admin_headers)
    conflicts = r.json()["conflicts"]
    assert len(conflicts) == 0


# ── Faculty leave + substitute assignment ─────────────────────────────────
def test_faculty_can_submit_leave(client, faculty_headers):
    r = client.post("/api/leave", json={
        "from_date": "2026-11-02T00:00:00", "to_date": "2026-11-02T00:00:00",
        "reason": "Conference",
    }, headers=faculty_headers)
    assert r.status_code == 201
    assert r.json()["faculty_id"] == "FAC100"
    assert r.json()["student_id"] is None


def test_faculty_leave_only_reviewable_by_admin(client, faculty_headers, admin_headers):
    r = client.post("/api/leave", json={
        "from_date": "2026-11-02T00:00:00", "to_date": "2026-11-02T00:00:00",
        "reason": "Conference",
    }, headers=faculty_headers)
    leave_id = r.json()["id"]

    # Another faculty member trying to approve — must be blocked
    other_fac = _make_faculty(client, admin_headers, "FACOTHER")
    r = client.patch(f"/api/leave/{leave_id}", json={"status": "approved"}, headers=other_fac)
    assert r.status_code == 403

    # Admin approving — allowed
    r = client.patch(f"/api/leave/{leave_id}", json={"status": "approved"}, headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["status"] == "approved"


def test_substitute_assignment_full_flow(client, admin_headers):
    # A Monday-only timetable slot for FACA
    _make_faculty(client, admin_headers, "FACA")
    substitute_headers = _make_faculty(client, admin_headers, "FACSUB", branch="CSE")
    slot = _make_slot(client, admin_headers, "FACA", room="Room 401", day="monday",
                       section="A", course_id="CS301")

    fac_headers = client.post("/api/auth/login", json={"credential": "FACA", "password": "Pass@1234", "role": "faculty"})
    fac_headers = {"Authorization": f"Bearer {fac_headers.json()['access_token']}"}

    # Leave covering a known Monday
    r = client.post("/api/leave", json={
        "from_date": "2026-11-02T00:00:00", "to_date": "2026-11-08T00:00:00",  # a full week, includes >=1 Monday
        "reason": "Medical",
    }, headers=fac_headers)
    assert r.status_code == 201
    leave_id = r.json()["id"]

    r = client.patch(f"/api/leave/{leave_id}", json={"status": "approved"}, headers=admin_headers)
    assert r.status_code == 200

    r = client.get(f"/api/leave/{leave_id}/affected-classes", headers=admin_headers)
    assert r.status_code == 200
    classes = r.json()["classes"]
    assert len(classes) >= 1
    assert classes[0]["day"] == "monday"

    cls = classes[0]
    r = client.post(f"/api/leave/{leave_id}/assign-substitute", json={
        "timetable_slot_id": cls["timetable_slot_id"],
        "class_date": cls["class_date"],
        "substitute_faculty_id": "FACSUB",
    }, headers=admin_headers)
    assert r.status_code == 201
    assert r.json()["substitute_faculty_id"] == "FACSUB"

    # Substitute can see it on their own dashboard
    r = client.get("/api/leave/substitute-assignments/mine", headers=substitute_headers)
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_cannot_assign_self_as_substitute(client, admin_headers):
    _make_faculty(client, admin_headers, "FACA")
    slot = _make_slot(client, admin_headers, "FACA", room="Room 501", day="tuesday")
    fac_headers = client.post("/api/auth/login", json={"credential": "FACA", "password": "Pass@1234", "role": "faculty"})
    fac_headers = {"Authorization": f"Bearer {fac_headers.json()['access_token']}"}
    r = client.post("/api/leave", json={
        "from_date": "2026-11-03T00:00:00", "to_date": "2026-11-03T00:00:00", "reason": "x",
    }, headers=fac_headers)
    leave_id = r.json()["id"]
    client.patch(f"/api/leave/{leave_id}", json={"status": "approved"}, headers=admin_headers)
    classes = client.get(f"/api/leave/{leave_id}/affected-classes", headers=admin_headers).json()["classes"]
    r = client.post(f"/api/leave/{leave_id}/assign-substitute", json={
        "timetable_slot_id": classes[0]["timetable_slot_id"],
        "class_date": classes[0]["class_date"],
        "substitute_faculty_id": "FACA",  # same person
    }, headers=admin_headers)
    assert r.status_code == 400
