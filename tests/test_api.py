from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.models import LOCAL_TIMEZONE, local_today
from app.repository import ClinicRepository


def schedule_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "clinic_id": 1,
        "doctor_id": 1,
        "date": (local_today() + timedelta(days=5)).isoformat(),
        "start_time": "09:00",
        "end_time": "11:00",
    }
    payload.update(overrides)
    return payload


def test_schedule_and_patient_card(client: TestClient, repository: ClinicRepository) -> None:
    assert len(client.get("/api/clinics").json()) == 2
    doctors = client.get("/api/doctors", params={"clinic_id": 1}).json()
    assert {doctor["id"] for doctor in doctors} == {1, 2}
    day = repository.get_slot(1).starts_at.astimezone(LOCAL_TIMEZONE).date().isoformat()
    response = client.get("/api/slots", params={"clinic_id": 1, "date": day})
    assert response.status_code == 200
    assert {slot["id"] for slot in response.json()} == {1, 2, 4}
    assert all(slot["is_available"] for slot in response.json())
    card = client.get("/api/patients/1").json()
    assert card["patient"]["full_name"] == "Иванов Иван"
    assert card["appointments"] == []
    assert card["lab_results"] == []


def test_create_patient(client: TestClient) -> None:
    response = client.post(
        "/api/patients",
        json={
            "full_name": "  Сидоров Сергей  ",
            "birth_date": "2000-02-29",
            "phone": " +7 (900) 123-45-67 ",
        },
    )
    assert response.status_code == 201
    patient = response.json()
    assert patient["full_name"] == "Сидоров Сергей"
    assert patient["phone"] == "+7 (900) 123-45-67"
    assert client.get(response.headers["location"]).json()["patient"] == patient


@pytest.mark.parametrize(
    "field,value",
    [
        ("full_name", "   "),
        ("full_name", "x" * 121),
        ("full_name", "\x00Иван"),
        ("birth_date", "2001-02-29"),
        ("birth_date", (local_today() + timedelta(days=1)).isoformat()),
        ("phone", "abcdefg"),
        ("phone", "123"),
        ("phone", "1" * 31),
        ("extra", "ignored?"),
    ],
)
def test_invalid_patient(client: TestClient, field: str, value: str) -> None:
    payload = {"full_name": "Иван", "birth_date": "2000-01-01"}
    payload[field] = value
    response = client.post("/api/patients", json=payload)
    assert response.status_code == 422
    assert len(client.get("/api/patients").json()) == 2


def test_book_cancel_and_rebook(client: TestClient) -> None:
    response = client.post("/api/appointments", json={"slot_id": 1, "patient_id": 1})
    assert response.status_code == 201
    appointment = response.json()
    assert appointment["status"] == "scheduled"
    assert appointment["patient_name"] == "Иванов Иван"
    assert client.get(response.headers["location"]).json() == appointment
    assert client.post("/api/appointments", json={"slot_id": 1, "patient_id": 2}).status_code == 409
    cancelled = client.post(f"/api/appointments/{appointment['id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["created_at"] == appointment["created_at"]
    assert client.post(f"/api/appointments/{appointment['id']}/cancel").json() == cancelled.json()
    rebooked = client.post("/api/appointments", json={"slot_id": 1, "patient_id": 2})
    assert rebooked.status_code == 201
    assert rebooked.json()["id"] != appointment["id"]
    assert len(client.get("/api/appointments").json()) == 2


def test_patient_cannot_have_overlapping_appointments(client: TestClient) -> None:
    assert client.post("/api/appointments", json={"slot_id": 1, "patient_id": 1}).status_code == 201
    for slot_id in (2, 3):
        response = client.post("/api/appointments", json={"slot_id": slot_id, "patient_id": 1})
        assert response.status_code == 409
        assert "пересекающееся" in response.json()["detail"]
    # Соседний приём, начинающийся в момент окончания предыдущего, разрешён.
    assert client.post("/api/appointments", json={"slot_id": 4, "patient_id": 1}).status_code == 201


@pytest.mark.parametrize(
    "payload,status",
    [
        ({"slot_id": 1, "patient_id": 999}, 404),
        ({"slot_id": 999, "patient_id": 1}, 404),
        ({"slot_id": 5, "patient_id": 1}, 409),
        ({"slot_id": -1, "patient_id": 1}, 422),
        ({"slot_id": 2**63, "patient_id": 1}, 422),
        ({"slot_id": 1, "patient_id": 1, "status": "completed"}, 422),
    ],
)
def test_invalid_booking(client: TestClient, payload: dict, status: int) -> None:
    assert client.post("/api/appointments", json=payload).status_code == status
    assert client.get("/api/appointments").json() == []


def test_complete_visit_and_keep_medical_record(client: TestClient, past_appointment: int) -> None:
    path = f"/api/appointments/{past_appointment}"
    before = client.get(path).json()
    response = client.post(path + "/complete", json={"note": "  Итоги приёма.\nРекомендации.  "})
    assert response.status_code == 200
    completed = response.json()
    assert completed["status"] == "completed"
    assert completed["note"] == "Итоги приёма.\nРекомендации."
    assert completed["completed_at"] is not None
    assert completed["created_at"] == before["created_at"]
    card = client.get("/api/patients/1").json()
    assert card["appointments"] == [completed]
    assert client.post(path + "/cancel").status_code == 409
    assert client.post(path + "/complete", json={"note": "Подменённая запись"}).status_code == 409
    assert client.get(path).json() == completed


@pytest.mark.parametrize("note", ["", "  ", "x" * 4001, "\x00Заметка"])
def test_invalid_medical_note(client: TestClient, past_appointment: int, note: str) -> None:
    path = f"/api/appointments/{past_appointment}"
    assert client.post(path + "/complete", json={"note": note}).status_code == 422
    assert client.get(path).json()["status"] == "scheduled"


def test_cannot_complete_future_or_cancelled_appointment(client: TestClient) -> None:
    appointment = client.post("/api/appointments", json={"slot_id": 1, "patient_id": 1}).json()
    path = f"/api/appointments/{appointment['id']}"
    assert client.post(path + "/complete", json={"note": "Рано"}).status_code == 409
    assert client.post(path + "/cancel").status_code == 200
    assert client.post(path + "/complete", json={"note": "Поздно"}).status_code == 409


@pytest.mark.parametrize(
    "path",
    [
        "/api/patients/999",
        "/api/appointments/999",
        "/api/doctors?clinic_id=999",
        "/api/slots?clinic_id=999&date=2026-01-01",
        "/api/unknown",
    ],
)
def test_missing_entities(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 404
    assert "detail" in response.json()


@pytest.mark.parametrize(
    "path",
    [
        "/api/patients/0",
        "/api/patients/9223372036854775808",
        "/api/slots?clinic_id=1&date=invalid",
        "/api/slots?clinic_id=1",
    ],
)
def test_invalid_query(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 422


def test_create_schedule(client: TestClient, repository: ClinicRepository) -> None:
    payload = schedule_payload()
    response = client.post("/api/schedule", json=payload)
    assert response.status_code == 201
    body = response.json()
    assert body["skipped_past"] == 0
    assert len(body["slots"]) == 4
    assert response.headers["location"] == (f"/api/slots?clinic_id=1&date={payload['date']}")
    listed = client.get("/api/slots", params={"clinic_id": 1, "date": payload["date"]}).json()
    assert {slot["id"] for slot in listed} == {slot["id"] for slot in body["slots"]}


def test_create_schedule_conflict_returns_slots(client: TestClient) -> None:
    day = schedule_payload()["date"]
    first = client.post(
        "/api/schedule", json=schedule_payload(start_time="09:00", end_time="10:00")
    )
    assert first.status_code == 201
    response = client.post(
        "/api/schedule", json=schedule_payload(start_time="09:30", end_time="10:30")
    )
    assert response.status_code == 409
    body = response.json()
    assert "уже есть слоты" in body["detail"]
    assert [slot["starts_at"] for slot in body["conflicts"]] == [
        first.json()["slots"][1]["starts_at"]
    ]
    listed = client.get("/api/slots", params={"clinic_id": 1, "date": day}).json()
    assert len(listed) == len(first.json()["slots"])


@pytest.mark.parametrize(
    "payload,status",
    [
        ({"clinic_id": 999}, 404),
        ({"doctor_id": 999}, 404),
        ({"doctor_id": 3}, 404),
        ({"start_time": "09:15"}, 422),
        ({"end_time": "11:15"}, 422),
        ({"break_start": "10:00"}, 422),
        ({"break_end": "10:30"}, 422),
        ({"break_start": "09:00", "break_end": "09:30"}, 422),
        ({"start_time": "11:00", "end_time": "09:00"}, 422),
        ({"date": (local_today() - timedelta(days=1)).isoformat()}, 422),
    ],
)
def test_invalid_schedule(client: TestClient, payload: dict[str, object], status: int) -> None:
    response = client.post("/api/schedule", json=schedule_payload(**payload))
    assert response.status_code == status
    assert "detail" in response.json()
    assert (
        client.get("/api/slots", params={"clinic_id": 1, "date": schedule_payload()["date"]}).json()
        == []
    )


def test_openapi(client: TestClient) -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert "/api/appointments/{appointment_id}/complete" in response.json()["paths"]
    assert "/api/schedule" in response.json()["paths"]


def test_date_without_schedule(client: TestClient) -> None:
    assert (
        client.get(
            "/api/slots", params={"clinic_id": 1, "date": date(2020, 1, 1).isoformat()}
        ).json()
        == []
    )
