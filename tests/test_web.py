from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.models import LOCAL_TIMEZONE, ScheduleCreate, local_today
from app.repository import ClinicRepository
from app.web import field_errors


def test_schedule_and_booking_flow(client: TestClient, repository: ClinicRepository) -> None:
    day = repository.get_slot(1).starts_at.astimezone(LOCAL_TIMEZONE).date().isoformat()
    response = client.get("/", params={"clinic_id": 1, "date": day})
    assert response.status_code == 200
    assert "Расписание приёмов" in response.text
    assert "/appointments/new?slot_id=1" in response.text
    assert client.get("/appointments/new?slot_id=1").status_code == 200
    response = client.post(
        "/appointments", data={"slot_id": 1, "patient_id": 1}, follow_redirects=False
    )
    assert response.status_code == 303
    detail = client.get(response.headers["location"])
    assert "Пациент записан на приём." in detail.text
    assert "Иванов Иван" in detail.text
    appointment_id = repository.list_appointments()[0].id
    assert f"/appointments/{appointment_id}" in client.get("/appointments").text
    assert "Иванов Иван" in client.get("/patients").text
    assert "Запланирован" in client.get("/patients/1").text
    response = client.post(f"/appointments/{appointment_id}/cancel", follow_redirects=False)
    assert response.status_code == 303
    assert "Запись отменена." in client.get(response.headers["location"]).text
    assert repository.get_slot(1).is_available


def test_patient_form_errors_preserve_input(client: TestClient) -> None:
    response = client.post(
        "/patients",
        data={"full_name": "Сидорова Анна", "birth_date": "2000-01-01", "phone": "ошибка"},
    )
    assert response.status_code == 422
    assert 'value="Сидорова Анна"' in response.text
    assert 'value="2000-01-01"' in response.text
    assert 'value="ошибка"' in response.text
    assert 'aria-invalid="true"' in response.text
    assert "от 7 до 15 цифр" in response.text


def test_patient_creation_and_html_escaping(client: TestClient) -> None:
    payload = '<script>alert("x")</script>'
    response = client.post(
        "/patients", data={"full_name": payload, "birth_date": "2000-01-01"}, follow_redirects=False
    )
    assert response.status_code == 303
    detail = client.get(response.headers["location"])
    assert payload not in detail.text
    assert "&lt;script&gt;" in detail.text
    assert payload not in client.get("/patients").text
    assert any(patient["full_name"] == payload for patient in client.get("/api/patients").json())


def test_booking_error_keeps_selected_patient(client: TestClient) -> None:
    client.post("/appointments", data={"slot_id": 1, "patient_id": 1})
    response = client.post("/appointments", data={"slot_id": 2, "patient_id": 1})
    assert response.status_code == 409
    assert "пересекающееся время" in response.text
    assert '<option value="1" selected>' in response.text
    assert client.post("/appointments", data={"slot_id": 2, "patient_id": ""}).status_code == 422


def test_complete_and_escape_note(client: TestClient, past_appointment: int) -> None:
    path = f"/appointments/{past_appointment}"
    assert 'name="note"' in client.get(path).text
    assert client.post(path + "/complete", data={"note": " "}).status_code == 422
    note = "<b>Учебная заметка</b>\nВторая строка"
    response = client.post(path + "/complete", data={"note": note}, follow_redirects=False)
    assert response.status_code == 303
    for target in (path, "/patients/1"):
        text = client.get(target).text
        assert "&lt;b&gt;Учебная заметка&lt;/b&gt;" in text
        assert note not in text
    assert client.post(path + "/cancel").status_code == 409


@pytest.mark.parametrize(
    "path,status",
    [
        ("/patients/new", 200),
        ("/patients/999", 404),
        ("/appointments/999", 404),
        ("/appointments/new?slot_id=999", 404),
        ("/unknown", 404),
        ("/?clinic_id=999", 404),
        ("/?date=bad", 422),
        ("/patients/9223372036854775808", 422),
    ],
)
def test_pages(client: TestClient, path: str, status: int) -> None:
    response = client.get(path)
    assert response.status_code == status
    assert '<html lang="ru">' in response.text


def test_cross_site_posts_rejected(client: TestClient) -> None:
    for path, kwargs in (
        ("/patients", {"data": {"full_name": "Иван", "birth_date": "2000-01-01"}}),
        ("/api/appointments", {"json": {"slot_id": 1, "patient_id": 1}}),
    ):
        response = client.post(path, headers={"Origin": "https://other.example"}, **kwargs)
        assert response.status_code == 403
    assert len(client.get("/api/patients").json()) == 2
    assert client.get("/api/appointments").json() == []


def test_same_origin_post_and_untrusted_host(client: TestClient) -> None:
    response = client.post(
        "/api/appointments",
        json={"slot_id": 1, "patient_id": 1},
        headers={"Origin": "http://127.0.0.1"},
    )
    assert response.status_code == 201
    assert client.get("/", headers={"Host": "other.example"}).status_code == 400


def test_styles_are_local(client: TestClient) -> None:
    response = client.get("/static/styles.css")
    assert response.status_code == 200
    assert "--accent:" in response.text
    text = client.get("/").text
    assert "<script" not in text


def test_field_errors_for_schedule_fields() -> None:
    payload: dict[str, object] = {
        "clinic_id": "",
        "doctor_id": 1,
        "date": (local_today() + timedelta(days=2)).isoformat(),
        "start_time": "",
        "end_time": "11:00",
    }
    with pytest.raises(ValidationError) as error:
        ScheduleCreate.model_validate(payload)
    messages = field_errors(error.value)
    assert messages["clinic_id"] == "Выберите поликлинику из списка."
    assert messages["start_time"] == "Укажите время ЧЧ:ММ с шагом 30 минут."


def test_field_errors_for_schedule_root() -> None:
    payload: dict[str, object] = {
        "clinic_id": 1,
        "doctor_id": 1,
        "date": (local_today() - timedelta(days=1)).isoformat(),
        "start_time": "09:00",
        "end_time": "11:00",
    }
    with pytest.raises(ValidationError) as error:
        ScheduleCreate.model_validate(payload)
    messages = field_errors(error.value)
    assert "только на будущее" in messages["form"]


def management_payload(day: date, **overrides: str) -> dict[str, str]:
    data = {
        "clinic_id": "1",
        "doctor_id": "1",
        "date": day.isoformat(),
        "start_time": "09:00",
        "end_time": "11:00",
        "break_start": "",
        "break_end": "",
    }
    data.update(overrides)
    return data


def test_schedule_management_page(client: TestClient) -> None:
    response = client.get("/schedule/manage")
    assert response.status_code == 200
    assert "Управление расписанием" in response.text
    for name in (
        "clinic_id",
        "doctor_id",
        "date",
        "start_time",
        "end_time",
        "break_start",
        "break_end",
    ):
        assert f'name="{name}"' in response.text
    assert "<optgroup" in response.text
    assert 'href="/schedule/manage"' in response.text


def test_schedule_management_success(client: TestClient, repository: ClinicRepository) -> None:
    day = local_today() + timedelta(days=3)
    response = client.post("/schedule/manage", data=management_payload(day), follow_redirects=False)
    assert response.status_code == 303
    assert "notice=schedule_created" in response.headers["location"]
    assert "created=4" in response.headers["location"]
    page = client.get(response.headers["location"])
    assert "Создано слотов: 4" in page.text
    assert "09:00" in page.text
    assert len(repository.list_slots(1, day)) == 4


def test_schedule_management_validation_error(client: TestClient) -> None:
    day = local_today() + timedelta(days=4)
    response = client.post("/schedule/manage", data=management_payload(day, start_time="09:10"))
    assert response.status_code == 422
    assert "кратно 30 минутам" in response.text
    assert 'value="09:10"' in response.text


def test_schedule_management_conflict(client: TestClient, repository: ClinicRepository) -> None:
    day = repository.get_slot(4).starts_at.astimezone(LOCAL_TIMEZONE).date()
    response = client.post(
        "/schedule/manage",
        data=management_payload(day, start_time="10:30", end_time="11:30"),
    )
    assert response.status_code == 409
    assert "пересекается" in response.text


def test_schedule_management_wrong_doctor(client: TestClient) -> None:
    day = local_today() + timedelta(days=5)
    response = client.post("/schedule/manage", data=management_payload(day, clinic_id="2"))
    assert response.status_code == 404
    assert "Врач не найден" in response.text


def test_schedule_management_escaping(client: TestClient) -> None:
    payload = '<script>alert("x")</script>'
    day = local_today() + timedelta(days=6)
    response = client.post("/schedule/manage", data=management_payload(day, start_time=payload))
    assert response.status_code == 422
    assert payload not in response.text
    assert "&lt;script&gt;" in response.text
