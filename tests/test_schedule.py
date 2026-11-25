from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.errors import ConflictError, NotFoundError
from app.main import create_app
from app.models import (
    LOCAL_TIMEZONE,
    AppointmentCreate,
    ScheduleCreate,
    local_today,
)
from app.repository import ClinicRepository, timestamp


def schedule(
    *,
    clinic_id: int = 1,
    doctor_id: int = 1,
    day=None,
    start: str = "09:00",
    end: str = "11:00",
    break_start: str | None = None,
    break_end: str | None = None,
) -> ScheduleCreate:
    return ScheduleCreate(
        clinic_id=clinic_id,
        doctor_id=doctor_id,
        date=day or local_today() + timedelta(days=10),
        start_time=start,
        end_time=end,
        break_start=break_start,
        break_end=break_end,
    )


def local_times(slots: list) -> list[str]:
    return [slot.starts_at.astimezone(LOCAL_TIMEZONE).strftime("%H:%M") for slot in slots]


def test_create_schedule_splits_into_half_hour_slots(repository: ClinicRepository) -> None:
    day = local_today() + timedelta(days=10)
    created = repository.create_schedule(schedule(day=day))

    assert local_times(created) == ["09:00", "09:30", "10:00", "10:30"]
    assert all(slot.ends_at - slot.starts_at == timedelta(minutes=30) for slot in created)
    assert all(slot.is_available for slot in created)
    assert {slot.id for slot in created} <= {slot.id for slot in repository.list_slots(1, day)}


def test_break_does_not_create_slots(repository: ClinicRepository) -> None:
    day = local_today() + timedelta(days=10)
    created = repository.create_schedule(schedule(day=day, break_start="09:30", break_end="10:00"))

    assert local_times(created) == ["09:00", "10:00", "10:30"]


def test_adjacent_interval_is_allowed(repository: ClinicRepository) -> None:
    day = local_today() + timedelta(days=10)
    repository.create_schedule(schedule(day=day, start="09:00", end="11:00"))
    created = repository.create_schedule(schedule(day=day, start="11:00", end="12:00"))

    assert local_times(created) == ["11:00", "11:30"]
    assert len(repository.list_slots(1, day)) == 6


def test_overlapping_interval_is_rejected(repository: ClinicRepository) -> None:
    day = local_today() + timedelta(days=10)
    repository.create_schedule(schedule(day=day, start="09:00", end="11:00"))

    with pytest.raises(ConflictError, match="пересекающееся"):
        repository.create_schedule(schedule(day=day, start="10:00", end="12:00"))
    assert len(repository.list_slots(1, day)) == 4


def test_concurrent_overlapping_intervals_are_serialized(
    repository: ClinicRepository,
) -> None:
    day = local_today() + timedelta(days=10)
    barrier = Barrier(2)

    def create(start: str, end: str) -> str:
        barrier.wait(timeout=5)
        try:
            repository.create_schedule(schedule(day=day, start=start, end=end))
        except ConflictError:
            return "conflict"
        return "created"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, ("09:00", "10:00"), ("11:00", "12:00")))
    assert sorted(results) == ["conflict", "created"]
    assert len(repository.list_slots(1, day)) == 4


def test_interval_cannot_cover_another_intervals_break(
    repository: ClinicRepository,
) -> None:
    day = local_today() + timedelta(days=10)
    repository.create_schedule(
        schedule(day=day, start="09:00", end="11:00", break_start="09:30", break_end="10:00")
    )

    # 09:30–10:00 — перерыв внутри уже добавленного рабочего интервала.
    with pytest.raises(ConflictError, match="пересекающееся"):
        repository.create_schedule(schedule(day=day, start="09:30", end="10:00"))


def test_schedule_creation_preserves_existing_slots_and_appointments(
    repository: ClinicRepository,
) -> None:
    appointment = repository.book(AppointmentCreate(slot_id=1, patient_id=1))
    existing_day = repository.get_slot(1).starts_at.astimezone(LOCAL_TIMEZONE).date()
    before_slots = repository.list_slots(1, existing_day)

    repository.create_schedule(schedule())

    assert repository.list_slots(1, existing_day) == before_slots
    assert repository.get_appointment(appointment.id) == appointment
    assert repository.get_slot(1).appointment_id == appointment.id


@pytest.mark.parametrize(
    "start,end,break_start,break_end",
    [
        ("09:15", "11:00", None, None),
        ("09:00", "11:15", None, None),
        ("09:00", "11:00", "10:15", "10:45"),
        ("09:00", "09:00", None, None),
        ("11:00", "09:00", None, None),
        ("09:00", "11:00", "08:30", "09:30"),
        ("09:00", "11:00", "10:30", "11:30"),
        ("09:00", "11:00", "10:00", "10:00"),
        ("09:00", "11:00", "10:00", None),
        ("09:00", "11:00", None, "10:00"),
        ("09:00", "09:30", "09:00", "09:30"),
    ],
)
def test_invalid_schedule_interval(
    start: str, end: str, break_start: str | None, break_end: str | None
) -> None:
    with pytest.raises(ValidationError):
        schedule(start=start, end=end, break_start=break_start, break_end=break_end)


def test_doctor_must_belong_to_selected_clinic(repository: ClinicRepository) -> None:
    with pytest.raises(ConflictError, match="не работает"):
        repository.create_schedule(schedule(clinic_id=1, doctor_id=3))


def test_missing_clinic_or_doctor(repository: ClinicRepository) -> None:
    with pytest.raises(NotFoundError, match="Поликлиника"):
        repository.create_schedule(schedule(clinic_id=999))
    with pytest.raises(NotFoundError, match="Врач"):
        repository.create_schedule(schedule(doctor_id=999))


def test_api_create_schedule(client: TestClient) -> None:
    day = (local_today() + timedelta(days=10)).isoformat()
    response = client.post(
        "/api/schedules",
        json={
            "clinic_id": 1,
            "doctor_id": 1,
            "date": day,
            "start_time": "09:00",
            "end_time": "11:00",
        },
    )

    assert response.status_code == 201
    assert len(response.json()) == 4
    assert response.headers["Location"] == f"/api/slots?clinic_id=1&date={day}"
    listed = client.get("/api/slots", params={"clinic_id": 1, "date": day}).json()
    assert {slot["id"] for slot in response.json()} <= {slot["id"] for slot in listed}


def test_api_schedule_errors(client: TestClient) -> None:
    day = (local_today() + timedelta(days=10)).isoformat()
    valid = {
        "clinic_id": 1,
        "doctor_id": 1,
        "date": day,
        "start_time": "09:00",
        "end_time": "11:00",
    }
    assert client.post("/api/schedules", json={**valid, "start_time": "09:15"}).status_code == 422
    assert client.post("/api/schedules", json={**valid, "break_start": "10:00"}).status_code == 422
    assert client.post("/api/schedules", json={**valid, "doctor_id": 3}).status_code == 409
    assert client.post("/api/schedules", json={**valid, "clinic_id": 999}).status_code == 404
    assert client.post("/api/schedules", json=valid).status_code == 201
    assert client.post("/api/schedules", json={**valid, "start_time": "10:00"}).status_code == 409


def test_schedule_manage_page_and_creation(
    client: TestClient, repository: ClinicRepository
) -> None:
    day = (local_today() + timedelta(days=10)).isoformat()
    page = client.get("/schedule", params={"clinic_id": 1, "date": day})
    assert page.status_code == 200
    assert "Управление расписанием" in page.text
    assert 'name="start_time"' in page.text
    assert 'name="break_start"' in page.text

    response = client.post(
        "/schedule",
        data={
            "clinic_id": 1,
            "doctor_id": 1,
            "date": day,
            "start_time": "09:00",
            "end_time": "11:00",
            "break_start": "",
            "break_end": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    result = client.get(response.headers["location"])
    assert "Добавлено слотов: 4" in result.text
    assert "09:00" in result.text
    assert "10:30" in result.text
    schedule_page = client.get("/", params={"clinic_id": 1, "date": day})
    assert "09:00" in schedule_page.text
    assert "10:30" in schedule_page.text
    assert len(repository.list_slots(1, local_today() + timedelta(days=10))) == 4


def test_schedule_manage_error_explains_and_preserves_values(client: TestClient) -> None:
    day = (local_today() + timedelta(days=10)).isoformat()
    response = client.post(
        "/schedule",
        data={
            "clinic_id": 1,
            "doctor_id": 1,
            "date": day,
            "start_time": "09:15",
            "end_time": "11:00",
            "break_start": "",
            "break_end": "",
        },
    )
    assert response.status_code == 422
    assert "кратно 30 минутам" in response.text
    assert 'value="09:15"' in response.text
    assert 'aria-invalid="true"' in response.text


def test_schedule_manage_overlap_error(client: TestClient) -> None:
    day = (local_today() + timedelta(days=10)).isoformat()
    data = {
        "clinic_id": 1,
        "doctor_id": 1,
        "date": day,
        "start_time": "09:00",
        "end_time": "11:00",
        "break_start": "",
        "break_end": "",
    }
    assert client.post("/schedule", data=data, follow_redirects=False).status_code == 303
    response = client.post("/schedule", data={**data, "start_time": "10:00"})
    assert response.status_code == 409
    assert "пересекающееся время" in response.text


def test_schedule_manage_doctor_from_other_clinic(client: TestClient) -> None:
    day = (local_today() + timedelta(days=10)).isoformat()
    response = client.post(
        "/schedule",
        data={
            "clinic_id": 1,
            "doctor_id": 3,
            "date": day,
            "start_time": "09:00",
            "end_time": "11:00",
        },
    )
    assert response.status_code == 409
    assert "не работает в этой поликлинике" in response.text


def test_schedule_manage_missing_entities(client: TestClient) -> None:
    assert client.get("/schedule", params={"clinic_id": 999}).status_code == 404
    assert client.get("/schedule", params={"clinic_id": 1, "doctor_id": 999}).status_code == 404


def test_schedule_manage_empty_database(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "empty.sqlite3"), base_url="http://127.0.0.1") as client:
        response = client.get("/schedule")
    assert response.status_code == 200
    assert "Поликлиник пока нет" in response.text


def test_initialize_upgrades_version_1_and_preserves_data(tmp_path: Path) -> None:
    repository = ClinicRepository(tmp_path / "version-1.sqlite3")
    repository.initialize()
    start = datetime.now(UTC) + timedelta(days=5)
    with repository._connect() as connection:
        connection.execute("DROP TABLE schedule_intervals")
        connection.execute("PRAGMA user_version = 1")
        connection.execute("INSERT INTO clinics VALUES (1, 'Поликлиника №01', 'ул. Учебная, д. 1')")
        connection.execute(
            "INSERT INTO doctors VALUES (1, 1, 'Соколова Анна Сергеевна', 'Терапевт')"
        )
        connection.execute(
            "INSERT INTO slots (doctor_id, starts_at, ends_at) VALUES (1, ?, ?)",
            (timestamp(start), timestamp(start + timedelta(minutes=30))),
        )

    repository.initialize()

    with repository._connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM schedule_intervals").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM slots").fetchone()[0] == 1
        assert (
            connection.execute("SELECT name FROM clinics WHERE id = 1").fetchone()[0]
            == "Поликлиника №01"
        )


def test_initialize_rejects_newer_database(tmp_path: Path) -> None:
    repository = ClinicRepository(tmp_path / "future.sqlite3")
    repository.initialize()
    with repository._connect() as connection:
        connection.execute("PRAGMA user_version = 3")
    with pytest.raises(RuntimeError, match="более новой"):
        repository.initialize()
