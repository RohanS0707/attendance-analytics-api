"""HTTP-level regression tests for the Attendance API."""
from fastapi.testclient import TestClient
import mongomock
import pytest
from types import SimpleNamespace

from app import main


@pytest.fixture
def client(monkeypatch):
    mongo = mongomock.MongoClient(tz_aware=True)
    monkeypatch.setattr(main, "db", mongo.attendance_test)
    mongo.attendance_test.employees.create_index("emp_code", unique=True)
    mongo.attendance_test.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    monkeypatch.setattr(main, "ensure_indexes", lambda: True)
    monkeypatch.setattr(main, "client", SimpleNamespace(admin=SimpleNamespace(
        command=lambda *_args, **_kwargs: {"ok": 1}
    )))
    with TestClient(main.app) as test_client:
        yield test_client
    mongo.close()


def employee_payload(**overrides):
    return {
        "emp_code": "EMP1001", "name": "Asha Rao", "email": "asha@example.com",
        "department": "Engineering", "joined_on": "2024-01-15",
        "shift_start": "09:30", "shift_end": "18:30", **overrides,
    }


def add_employee(client, **overrides):
    response = client.post("/employees", json=employee_payload(**overrides))
    assert response.status_code == 201
    return response.json()


def test_health_returns_ok(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_create_employee_returns_created_record(client):
    response = client.post("/employees", json=employee_payload())
    assert response.status_code == 201
    assert response.json()["emp_code"] == "EMP1001"
    assert response.json()["created_at"] is not None


def test_create_employee_rejects_invalid_code(client):
    response = client.post("/employees", json=employee_payload(emp_code="BAD"))
    assert response.status_code == 422


def test_create_employee_rejects_invalid_calendar_date(client):
    response = client.post("/employees", json=employee_payload(joined_on="2024-02-31"))
    assert response.status_code == 422


def test_create_employee_duplicate_returns_conflict(client):
    add_employee(client)
    response = client.post("/employees", json=employee_payload())
    assert response.status_code == 409


def test_list_employees_filters_and_paginates(client):
    add_employee(client)
    add_employee(client, emp_code="EMP1002", department="Sales")
    response = client.get("/employees", params={"department": "Engineering", "page": 1, "page_size": 1})
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert [row["emp_code"] for row in response.json()["items"]] == ["EMP1001"]


def test_punch_in_missing_employee_returns_not_found(client):
    response = client.post("/attendance/punch-in", json={"emp_code": "EMP9999"})
    assert response.status_code == 404


def test_punch_in_creates_record_and_uses_requested_status(client):
    add_employee(client)
    response = client.post("/attendance/punch-in", json={
        "emp_code": "EMP1001", "punched_at": 1764142200000, "status": "WFH",
    })
    assert response.status_code == 201
    assert response.json()["status"] == "WFH"
    assert response.json()["emp_code"] == "EMP1001"


def test_punch_in_rejects_duplicate_for_same_attendance_date(client):
    add_employee(client)
    payload = {"emp_code": "EMP1001", "punched_at": 1764142200000}
    assert client.post("/attendance/punch-in", json=payload).status_code == 201
    assert client.post("/attendance/punch-in", json=payload).status_code == 409


def test_punch_out_requires_existing_punch_in(client):
    add_employee(client)
    response = client.post("/attendance/punch-out", json={"emp_code": "EMP1001"})
    assert response.status_code == 404


def test_punch_out_calculates_hours(client):
    add_employee(client)
    pin = 1764142200000
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP1001", "punched_at": pin}).status_code == 201
    response = client.post("/attendance/punch-out", json={"emp_code": "EMP1001", "punched_at": pin + 8 * 3600000})
    assert response.status_code == 200
    assert response.json()["work_hours"] == 8.0
    assert response.json()["punch_out"] == pin + 8 * 3600000


def test_attendance_list_applies_status_filter(client):
    add_employee(client)
    client.post("/attendance/punch-in", json={"emp_code": "EMP1001", "punched_at": 1764142200000, "status": "WFH"})
    response = client.get("/attendance", params={"status": "WFH"})
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["status"] == "WFH"


def test_regularize_missing_record_returns_not_found(client):
    add_employee(client)
    response = client.patch("/attendance/EMP1001/2025-01-01", json={
        "status": "ABSENT", "reason": "Approved leave", "regularized_by": "manager",
    })
    assert response.status_code == 404


def test_regularize_status_records_audit_history(client):
    add_employee(client)
    client.post("/attendance/punch-in", json={"emp_code": "EMP1001", "punched_at": 1764142200000})
    response = client.patch("/attendance/EMP1001/2025-11-26", json={
        "status": "ABSENT", "reason": "Approved leave", "regularized_by": "manager",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ABSENT"
    assert body["punch_in"] is None
    assert body["history"][0]["by"] == "manager"
