from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from app.errors import ConflictError
from app.models import LOCAL_TIMEZONE, AppointmentCreate, ScheduleCreate, local_today
from app.repository import ClinicRepository


def schedule_values(**overrides: str) -> dict[str, str]:
    return {
        "clinic_id": "1",
        "doctor_id": "1",
        "day": (local_today() + timedelta(days=7)).isoformat(),
        "start": "09:00",
        "end": "11:00",
        **overrides,
    }


def test_form_lists_only_doctors_of_selected_clinic(client: TestClient) -> None:
    response = client.get("/schedule/manage?clinic_id=2")
    assert response.status_code == 200
    assert "Управление расписанием" in response.text
    assert "Белов Игорь Олегович" in response.text
    assert "Соколова Анна Сергеевна" not in response.text


@pytest.mark.parametrize(
    ("break_fields", "expected_starts"),
    [
        ({}, ["09:00", "09:30", "10:00", "10:30"]),
        ({"break_start": "09:30", "break_end": "10:30"}, ["09:00", "10:30"]),
    ],
)
def test_new_slots_appear_and_can_be_booked(
    client: TestClient,
    break_fields: dict[str, str],
    expected_starts: list[str],
) -> None:
    values = schedule_values(**break_fields)
    response = client.post("/schedule/manage", data=values, follow_redirects=False)
    assert response.status_code == 303
    assert f"date={values['day']}" in response.headers["location"]
    published = client.get(response.headers["location"])
    assert "Слоты добавлены в расписание" in published.text
    assert published.text.count("Записать") == len(expected_starts)

    slots = client.get(f"/api/slots?clinic_id=1&date={values['day']}").json()
    assert [slot["starts_at"][11:16] for slot in slots] == [
        f"{int(start[:2]) - 3:02d}{start[2:]}" for start in expected_starts
    ]
    booking = client.post("/api/appointments", json={"slot_id": slots[0]["id"], "patient_id": 1})
    assert booking.status_code == 201


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"clinic_id": "999"}, "Выберите поликлинику из списка"),
        ({"doctor_id": "3"}, "Выберите врача из списка этой поликлиники"),
        ({"day": "2026-02-30"}, "Укажите существующую дату"),
        ({"day": "2000-01-01"}, "Нельзя добавить расписание на прошедшую дату"),
        ({"start": "09:15"}, "Укажите время с шагом 30 минут"),
        ({"start": "12:00"}, "Окончание рабочего интервала должно быть позже"),
        ({"break_start": "10:00"}, "Укажите обе границы перерыва"),
        ({"break_start": "10:00", "break_end": "10:15"}, "Укажите время с шагом 30 минут"),
        ({"break_start": "09:00", "break_end": "10:00"}, "внутри рабочего интервала"),
    ],
)
def test_invalid_intervals_explain_the_error_without_writing(
    client: TestClient,
    repository: ClinicRepository,
    fields: dict[str, str],
    message: str,
) -> None:
    response = client.post("/schedule/manage", data=schedule_values(**fields))
    assert response.status_code == 422
    assert message in response.text
    if fields.get("clinic_id") != "999":
        assert 'value="11:00"' in response.text
    with repository._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM doctor_work_intervals").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM slots").fetchone()[0] == 6


def test_conflict_preserves_booked_slots_after_schema_upgrade(
    repository: ClinicRepository, client: TestClient
) -> None:
    booking = repository.book(AppointmentCreate(patient_id=1, slot_id=1))
    slot_day = repository.get_slot(1).starts_at.astimezone(LOCAL_TIMEZONE).date()
    with repository._connect() as connection:
        connection.execute("DROP TABLE doctor_work_intervals")
        connection.execute("PRAGMA user_version = 1")
    repository.initialize()
    with repository._connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2

    before = repository.list_slots(1, slot_day)
    response = client.post(
        "/schedule/manage", data=schedule_values(day=slot_day.isoformat(), start="09:30")
    )
    assert response.status_code == 409
    assert "Интервал пересекается с расписанием врача" in response.text
    assert 'value="09:30"' in response.text
    assert repository.list_slots(1, slot_day) == before
    assert repository.get_appointment(booking.id) == booking


def test_overlaps_with_break_are_rejected_but_adjacent_intervals_are_allowed(
    repository: ClinicRepository,
) -> None:
    original = ScheduleCreate.model_validate(
        schedule_values(break_start="09:30", break_end="10:30")
    )
    repository.create_schedule(original)
    with pytest.raises(ConflictError, match="пересекается"):
        repository.create_schedule(
            ScheduleCreate.model_validate(schedule_values(start="09:30", end="10:30"))
        )
    repository.create_schedule(
        ScheduleCreate.model_validate(schedule_values(start="11:00", end="11:30"))
    )
    assert len(repository.list_slots(1, original.day)) == 3


def test_concurrent_interval_creation_commits_once(repository: ClinicRepository) -> None:
    data = ScheduleCreate.model_validate(schedule_values())
    barrier = Barrier(2)

    def create() -> bool:
        barrier.wait(timeout=5)
        try:
            repository.create_schedule(data)
        except ConflictError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = [pool.submit(create) for _ in range(2)]
        assert sorted(attempt.result() for attempt in attempts) == [False, True]
    assert len(repository.list_slots(1, data.day)) == 4
