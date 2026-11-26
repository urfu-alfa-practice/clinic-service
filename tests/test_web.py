from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.models import LOCAL_TIMEZONE, local_today
from app.repository import ClinicRepository


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


def test_schedule_management_form_and_navigation(client: TestClient) -> None:
    response = client.get("/schedule/manage")

    assert response.status_code == 200
    assert "Управление расписанием" in response.text
    assert 'href="/schedule/manage"' in client.get("/").text
    for field in (
        "clinic_id",
        "doctor_id",
        "date",
        "start_time",
        "end_time",
        "break_start",
        "break_end",
    ):
        assert f'name="{field}"' in response.text
    assert response.text.count("<form") == 1
    assert response.text.count('name="clinic_id"') == 1
    assert 'action="/schedule/manage" method="post"' in response.text
    assert 'formmethod="get" formnovalidate' in response.text
    assert "Показать врачей" in response.text
    assert "filter_clinic_id" not in response.text
    assert "schedule-form" in response.text
    assert "<script" not in response.text
    assert ".schedule-form" in client.get("/static/styles.css").text


def test_schedule_implicit_submit_uses_primary_post_action(client: TestClient) -> None:
    text = client.get("/schedule/manage").text

    primary = text.index('id="schedule-implicit-submit"')
    refresh = text.index('formmethod="get"')
    assert primary < refresh
    assert 'tabindex="-1" aria-hidden="true"' in text


def test_schedule_management_hints_are_contextual_and_accessible(client: TestClient) -> None:
    response = client.get("/schedule/manage")

    assert response.status_code == 200
    assert "<aside" not in response.text
    assert 'class="detail-layout"' not in response.text
    for hint_id in ("doctor-id-hint", "date-hint"):
        assert f'id="{hint_id}"' in response.text
        assert f'aria-describedby="{hint_id}"' in response.text
    for hint_id in ("work-interval-hint", "break-hint"):
        assert f'id="{hint_id}"' in response.text
        assert response.text.count(f'aria-describedby="{hint_id}"') == 2
    assert response.text.index("Рабочий интервал") < response.text.index("work-interval-hint")
    assert response.text.index("Перерыв") < response.text.index("break-hint")
    assert "30 минут" in response.text
    assert "Существующие слоты и записи пациентов не изменяются." in response.text


@pytest.mark.parametrize(
    "clinic_id,expected_doctor,other_clinic_doctor",
    [
        (1, "Соколова Анна Сергеевна", "Белов Игорь Олегович"),
        (2, "Белов Игорь Олегович", "Соколова Анна Сергеевна"),
    ],
)
def test_schedule_management_filters_doctors_on_server(
    client: TestClient, clinic_id: int, expected_doctor: str, other_clinic_doctor: str
) -> None:
    response = client.get("/schedule/manage", params={"clinic_id": clinic_id})

    assert response.status_code == 200
    assert expected_doctor in response.text
    assert other_clinic_doctor not in response.text
    assert f'<option value="{clinic_id}" selected>' in response.text


def test_schedule_management_get_preserves_schedule_values(client: TestClient) -> None:
    response = client.get(
        "/schedule/manage",
        params={
            "clinic_id": "2",
            "doctor_id": "",
            "date": "2027-02-03",
            "start_time": "09:00",
            "end_time": "12:00",
            "break_start": "10:00",
            "break_end": "10:30",
        },
    )

    assert response.status_code == 200
    assert "Белов Игорь Олегович" in response.text
    assert "Соколова Анна Сергеевна" not in response.text
    assert '<option value="2" selected>' in response.text
    for value in ("2027-02-03", "09:00", "12:00", "10:00", "10:30"):
        assert f'value="{value}"' in response.text


def schedule_payload(**changes: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "clinic_id": "1",
        "doctor_id": "1",
        "date": (local_today() + timedelta(days=20)).isoformat(),
        "start_time": "09:00",
        "end_time": "12:00",
        "break_start": "10:00",
        "break_end": "10:30",
    }
    values.update(changes)
    return values


def slot_count(repository: ClinicRepository) -> int:
    with repository._connect() as connection:
        return connection.execute("SELECT COUNT(*) FROM slots").fetchone()[0]


@pytest.mark.parametrize(
    "changes,field,message",
    [
        ({"start_time": "09:10"}, "start_time", "00 или 30"),
        ({"clinic_id": "1.5"}, "clinic_id", "целое число"),
        ({"doctor_id": ""}, "doctor_id", "обязательное"),
        ({"date": "2026-02-30"}, "date", "дат"),
    ],
)
def test_schedule_post_field_errors_preserve_valid_values_and_create_nothing(
    client: TestClient,
    repository: ClinicRepository,
    changes: dict[str, str],
    field: str,
    message: str,
) -> None:
    before = slot_count(repository)

    response = client.post("/schedule/manage", data=schedule_payload(**changes))

    assert response.status_code == 422
    assert f'id="{field}-error"' in response.text
    assert message in response.text
    if "clinic_id" not in changes:
        assert '<option value="1" selected>' in response.text
    assert 'value="10:00"' in response.text
    assert slot_count(repository) == before


def test_schedule_post_cross_field_error_is_above_form_and_creates_nothing(
    client: TestClient, repository: ClinicRepository
) -> None:
    before = slot_count(repository)

    response = client.post(
        "/schedule/manage",
        data=schedule_payload(start_time="12:00", end_time="09:00"),
    )

    assert response.status_code == 422
    assert 'class="alert alert-error spaced" role="alert"' in response.text
    assert "Окончание рабочего интервала" in response.text
    assert 'value="12:00"' in response.text
    assert slot_count(repository) == before


def test_schedule_post_rejects_doctor_from_another_clinic(
    client: TestClient, repository: ClinicRepository
) -> None:
    before = slot_count(repository)

    response = client.post("/schedule/manage", data=schedule_payload(doctor_id="3"))

    assert response.status_code == 422
    assert 'id="doctor_id-error"' in response.text
    assert "не закреплён" in response.text
    assert '<option value="3" selected>' not in response.text
    assert '<option value="1" selected>' in response.text
    assert slot_count(repository) == before


def test_schedule_post_missing_clinic_returns_form_and_resets_both_selects(
    client: TestClient, repository: ClinicRepository
) -> None:
    before = slot_count(repository)

    response = client.post("/schedule/manage", data=schedule_payload(clinic_id="999"))

    assert response.status_code == 404
    assert "<h1>Управление расписанием</h1>" in response.text
    assert 'id="clinic_id-error"' in response.text
    assert "Поликлиника не найдена" in response.text
    assert '<option value="999" selected>' not in response.text
    assert '<option value="1" selected>' not in response.text
    assert 'value="09:00"' in response.text
    assert slot_count(repository) == before


def test_schedule_post_missing_doctor_returns_form_and_resets_only_doctor(
    client: TestClient, repository: ClinicRepository
) -> None:
    before = slot_count(repository)

    response = client.post("/schedule/manage", data=schedule_payload(doctor_id="999"))

    assert response.status_code == 404
    assert "<h1>Управление расписанием</h1>" in response.text
    assert 'id="doctor_id-error"' in response.text
    assert "Врач не найден" in response.text
    assert '<option value="999" selected>' not in response.text
    assert '<option value="1" selected>' in response.text
    assert slot_count(repository) == before


def test_schedule_post_conflict_shows_time_and_preserves_valid_selects(
    client: TestClient, repository: ClinicRepository
) -> None:
    existing = repository.get_slot(1).starts_at.astimezone(LOCAL_TIMEZONE)
    before = slot_count(repository)

    response = client.post(
        "/schedule/manage",
        data=schedule_payload(
            date=existing.date().isoformat(),
            start_time=existing.strftime("%H:%M"),
            end_time=(existing + timedelta(minutes=30)).strftime("%H:%M"),
            break_start="",
            break_end="",
        ),
    )

    assert response.status_code == 409
    assert "пересекается" in response.text
    assert existing.strftime("%H:%M") in response.text
    assert (existing + timedelta(minutes=30)).strftime("%H:%M") in response.text
    assert '<option value="1" selected>' in response.text
    assert slot_count(repository) == before


def test_schedule_post_escapes_invalid_user_input(
    client: TestClient, repository: ClinicRepository
) -> None:
    payload = '<script>alert("schedule")</script>'
    before = slot_count(repository)

    response = client.post("/schedule/manage", data=schedule_payload(start_time=payload))

    assert response.status_code == 422
    assert payload not in response.text
    assert "&lt;script&gt;" in response.text
    assert slot_count(repository) == before


def test_schedule_creation_and_patient_booking_end_to_end(
    client: TestClient, repository: ClinicRepository
) -> None:
    payload = schedule_payload()

    response = client.post("/schedule/manage", data=payload, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == (f"/?clinic_id=1&date={payload['date']}&added=5")
    schedule = client.get(response.headers["location"])
    assert schedule.status_code == 200
    assert "Расписание добавлено: 5 слотов" in schedule.text

    slots = repository.list_slots(1, local_today() + timedelta(days=20))
    assert len(slots) == 5
    assert [slot.starts_at.astimezone(LOCAL_TIMEZONE).strftime("%H:%M") for slot in slots] == [
        "09:00",
        "09:30",
        "10:30",
        "11:00",
        "11:30",
    ]
    target = slots[2]
    booking_link = f"/appointments/new?slot_id={target.id}"
    assert booking_link in schedule.text
    booking = client.get(booking_link)
    assert booking.status_code == 200

    response = client.post(
        "/appointments",
        data={"slot_id": target.id, "patient_id": 2},
        follow_redirects=False,
    )

    assert response.status_code == 303
    appointment = next(item for item in repository.list_appointments() if item.slot_id == target.id)
    assert appointment.status == "scheduled"
    assert appointment.patient_id == 2
    assert appointment.starts_at == target.starts_at
    assert appointment.ends_at == target.ends_at
    assert "Пациент записан на приём." in client.get(response.headers["location"]).text


@pytest.mark.parametrize("field", ["clinic_id", "doctor_id"])
def test_schedule_post_rejects_very_long_identifier_without_technical_error(
    client: TestClient, repository: ClinicRepository, field: str
) -> None:
    before = slot_count(repository)

    response = client.post("/schedule/manage", data=schedule_payload(**{field: "9" * 5000}))

    assert response.status_code == 422
    assert f'id="{field}-error"' in response.text
    assert "целое число от 1" in response.text
    assert "ValueError" not in response.text
    assert "Exceeds the limit" not in response.text
    assert slot_count(repository) == before


@pytest.mark.parametrize("added", ["not-a-number", "9" * 5000])
def test_schedule_ignores_invalid_added_parameter(client: TestClient, added: str) -> None:
    response = client.get("/", params={"added": added})

    assert response.status_code == 200
    assert "Расписание приёмов" in response.text
    assert "Расписание добавлено:" not in response.text
