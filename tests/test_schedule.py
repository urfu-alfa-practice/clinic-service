from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.errors import ConflictError
from app.models import LOCAL_TIMEZONE, AppointmentCreate, WorkIntervalCreate, local_today
from app.repository import ClinicRepository


def interval(**overrides: object) -> WorkIntervalCreate:
    data: dict[str, object] = {
        "clinic_id": 1,
        "doctor_id": 1,
        "date": local_today() + timedelta(days=5),
        "start_time": "09:00",
        "end_time": "11:00",
    }
    data.update(overrides)
    return WorkIntervalCreate.model_validate(data)


def slot_count(repository: ClinicRepository) -> int:
    with repository._connect() as connection:
        return connection.execute("SELECT COUNT(*) FROM slots").fetchone()[0]


@pytest.mark.parametrize(
    "overrides",
    [
        {"start_time": "09:10"},
        {"end_time": "11:15"},
        {"break_start": "10:15", "break_end": "10:45"},
        {"start_time": "11:00", "end_time": "09:00"},
        {"break_start": "08:30", "break_end": "09:00"},
        {"break_start": "09:30"},
        {"break_start": "10:30", "break_end": "10:00"},
        {
            "start_time": "09:00",
            "end_time": "09:30",
            "break_start": "09:00",
            "break_end": "09:30",
        },
    ],
)
def test_invalid_interval(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        interval(**overrides)


def test_slots_without_break() -> None:
    slots = interval().slots()
    assert [start.strftime("%H:%M") for start, _ in slots] == [
        "09:00",
        "09:30",
        "10:00",
        "10:30",
    ]


def test_slots_skip_break() -> None:
    slots = interval(end_time="12:00", break_start="10:00", break_end="10:30").slots()
    assert [start.strftime("%H:%M") for start, _ in slots] == [
        "09:00",
        "09:30",
        "10:30",
        "11:00",
        "11:30",
    ]


def test_slots_with_single_slot_and_boundary_break() -> None:
    assert len(interval(start_time="09:00", end_time="09:30").slots()) == 1
    slots = interval(start_time="09:00", end_time="10:00", break_start="09:00", break_end="09:30")
    assert [start.strftime("%H:%M") for start, _ in slots.slots()] == ["09:30"]


def test_create_schedule_adds_slots(repository: ClinicRepository) -> None:
    day = local_today() + timedelta(days=5)
    created = repository.create_schedule(interval(date=day))
    assert created == 4
    slots = repository.list_slots(1, day)
    assert [slot.starts_at.astimezone(LOCAL_TIMEZONE).strftime("%H:%M") for slot in slots] == [
        "09:00",
        "09:30",
        "10:00",
        "10:30",
    ]


def test_schedule_is_stored_in_utc(repository: ClinicRepository) -> None:
    day = local_today() + timedelta(days=5)
    repository.create_schedule(interval(date=day))
    with repository._connect() as connection:
        row = connection.execute(
            "SELECT starts_at FROM slots WHERE starts_at LIKE ? ORDER BY starts_at LIMIT 1",
            (f"{day.isoformat()}T%",),
        ).fetchone()
    assert row is not None
    assert row["starts_at"] == f"{day.isoformat()}T06:00:00+00:00"


def test_partial_overlap_creates_nothing(repository: ClinicRepository) -> None:
    day = local_today() + timedelta(days=6)
    repository.create_schedule(interval(date=day, end_time="09:30"))
    before = slot_count(repository)
    with pytest.raises(ConflictError, match="расписание"):
        repository.create_schedule(interval(date=day, end_time="10:00"))
    assert slot_count(repository) == before


def test_exact_duplicate_creates_nothing(repository: ClinicRepository) -> None:
    day = local_today() + timedelta(days=6)
    repository.create_schedule(interval(date=day, end_time="09:30"))
    before = slot_count(repository)
    with pytest.raises(ConflictError):
        repository.create_schedule(interval(date=day, end_time="09:30"))
    assert slot_count(repository) == before


def test_adjacent_interval_is_allowed(repository: ClinicRepository) -> None:
    day = local_today() + timedelta(days=6)
    repository.create_schedule(interval(date=day, start_time="09:00", end_time="09:30"))
    repository.create_schedule(interval(date=day, start_time="09:30", end_time="10:00"))
    slots = repository.list_slots(1, day)
    assert [slot.starts_at.astimezone(LOCAL_TIMEZONE).strftime("%H:%M") for slot in slots] == [
        "09:00",
        "09:30",
    ]


def test_concurrent_schedule_creation(repository: ClinicRepository) -> None:
    day = local_today() + timedelta(days=6)
    barrier = Barrier(2)

    def attempt() -> int | None:
        barrier.wait()
        try:
            return repository.create_schedule(interval(date=day))
        except ConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(attempt), pool.submit(attempt)]
        created = sorted(result.result() for result in results if result.result())

    assert created == [4]
    slots = repository.list_slots(1, day)
    assert len(slots) == 4


def test_existing_appointment_is_preserved(repository: ClinicRepository) -> None:
    appointment = repository.book(AppointmentCreate(slot_id=1, patient_id=1))
    day = local_today() + timedelta(days=6)
    repository.create_schedule(interval(date=day))
    assert repository.get_appointment(appointment.id) == appointment
    assert repository.get_slot(1).appointment_id == appointment.id
    with repository._connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_manage_page_has_form(client: TestClient) -> None:
    response = client.get("/schedule/manage")
    assert response.status_code == 200
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


def test_manage_page_lists_selected_clinic_doctors(client: TestClient) -> None:
    text = client.get("/schedule/manage?clinic_id=2").text
    assert "Белов Игорь Олегович" in text
    assert "Соколова Анна Сергеевна" not in text


def test_schedule_page_links_to_management(client: TestClient) -> None:
    assert 'href="/schedule/manage"' in client.get("/").text


def test_create_schedule_via_form(client: TestClient, repository: ClinicRepository) -> None:
    day = local_today() + timedelta(days=5)
    response = client.post(
        "/schedule/manage",
        data={
            "clinic_id": "1",
            "doctor_id": "1",
            "date": day.isoformat(),
            "start_time": "09:00",
            "end_time": "11:00",
            "break_start": "",
            "break_end": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "notice=created" in response.headers["location"]
    assert "created=4" in response.headers["location"]
    page = client.get(f"/?clinic_id=1&date={day.isoformat()}")
    assert "09:00" in page.text
    assert "Свободно: 4" in page.text


def test_form_error_keeps_values(client: TestClient, repository: ClinicRepository) -> None:
    day = local_today() + timedelta(days=5)
    before = slot_count(repository)
    response = client.post(
        "/schedule/manage",
        data={
            "clinic_id": "1",
            "doctor_id": "1",
            "date": day.isoformat(),
            "start_time": "09:10",
            "end_time": "11:00",
            "break_start": "",
            "break_end": "",
        },
    )
    assert response.status_code == 422
    assert "кратны 30 минутам" in response.text
    assert 'value="09:10"' in response.text
    assert slot_count(repository) == before


def test_conflict_shows_message_and_creates_nothing(
    client: TestClient, repository: ClinicRepository
) -> None:
    day = local_today() + timedelta(days=5)
    before = slot_count(repository)
    data = {
        "clinic_id": "1",
        "doctor_id": "1",
        "date": day.isoformat(),
        "start_time": "09:00",
        "end_time": "09:30",
        "break_start": "",
        "break_end": "",
    }
    assert client.post("/schedule/manage", data=data).status_code == 200
    response = client.post("/schedule/manage", data={**data, "end_time": "10:00"})
    assert response.status_code == 409
    assert "уже есть расписание" in response.text
    assert slot_count(repository) == before + 1
    assert len(repository.list_slots(1, day)) == 1


def test_foreign_doctor_is_rejected(client: TestClient, repository: ClinicRepository) -> None:
    before = slot_count(repository)
    response = client.post(
        "/schedule/manage",
        data={
            "clinic_id": "1",
            "doctor_id": "3",
            "date": (local_today() + timedelta(days=5)).isoformat(),
            "start_time": "09:00",
            "end_time": "11:00",
            "break_start": "",
            "break_end": "",
        },
    )
    assert response.status_code == 404
    assert slot_count(repository) == before


def test_repeated_submit_does_not_duplicate(
    client: TestClient, repository: ClinicRepository
) -> None:
    day = local_today() + timedelta(days=5)
    data = {
        "clinic_id": "1",
        "doctor_id": "1",
        "date": day.isoformat(),
        "start_time": "09:00",
        "end_time": "11:00",
        "break_start": "",
        "break_end": "",
    }
    assert client.post("/schedule/manage", data=data).status_code == 200
    response = client.post("/schedule/manage", data=data)
    assert response.status_code == 409
    assert len(repository.list_slots(1, day)) == 4


def test_past_created_slot_is_not_bookable(
    client: TestClient, repository: ClinicRepository
) -> None:
    day = local_today() - timedelta(days=1)
    repository.create_schedule(interval(date=day, start_time="09:00", end_time="09:30"))
    page = client.get(f"/?clinic_id=1&date={day.isoformat()}")
    assert "09:00" in page.text
    assert "Время прошло" in page.text
    assert "Записать" not in page.text
