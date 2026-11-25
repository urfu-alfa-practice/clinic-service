from datetime import datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.errors import ConflictError, NotFoundError
from app.models import (
    LOCAL_TIMEZONE,
    AppointmentCreate,
    ScheduleCreate,
    local_today,
)
from app.repository import ClinicRepository, timestamp

TOMORROW = local_today() + timedelta(days=1)
FREE_DAY = local_today() + timedelta(days=10)


def schedule_data(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "clinic_id": 1,
        "doctor_id": 1,
        "date": TOMORROW,
        "start_time": "09:00",
        "end_time": "11:00",
        "break_start": "",
        "break_end": "",
    }
    values.update(overrides)
    return values


def create_schedule(**overrides: object) -> ScheduleCreate:
    return ScheduleCreate.model_validate(schedule_data(**overrides))


def test_interval_is_split_into_half_hour_slots() -> None:
    data = create_schedule()
    ranges = data.slot_ranges()
    assert [start.strftime("%H:%M") for start, _ in ranges] == [
        "09:00",
        "09:30",
        "10:00",
        "10:30",
    ]
    assert all(end - start == timedelta(minutes=30) for start, end in ranges)


def test_break_excludes_overlapping_slots() -> None:
    data = create_schedule(end_time="12:00", break_start="10:00", break_end="10:30")
    assert [start.strftime("%H:%M") for start, _ in data.slot_ranges()] == [
        "09:00",
        "09:30",
        "10:30",
        "11:00",
        "11:30",
    ]


def test_break_at_the_start_is_allowed() -> None:
    data = create_schedule(break_start="09:00", break_end="09:30")
    assert [start.strftime("%H:%M") for start, _ in data.slot_ranges()] == [
        "09:30",
        "10:00",
        "10:30",
    ]


def test_break_at_the_end_is_allowed() -> None:
    data = create_schedule(break_start="10:30", break_end="11:00")
    assert [start.strftime("%H:%M") for start, _ in data.slot_ranges()] == [
        "09:00",
        "09:30",
        "10:00",
    ]


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"start_time": "09:10"}, "кратны 30 минутам"),
        ({"end_time": "11:10"}, "кратны 30 минутам"),
        ({"break_start": "10:15", "break_end": "10:45"}, "перерыва"),
        ({"end_time": "09:00"}, "позже начала"),
        ({"start_time": "11:00", "end_time": "10:00"}, "позже начала"),
        ({"break_start": "10:00", "break_end": "12:30"}, "внутри"),
        ({"break_start": "10:00"}, "обе границы"),
        ({"break_end": "10:30"}, "обе границы"),
        ({"break_start": "09:00", "break_end": "11:00"}, "не остаётся времени"),
        ({"date": local_today() - timedelta(days=1)}, "в прошлом"),
    ],
)
def test_invalid_schedule_is_rejected(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        create_schedule(**overrides)


def test_past_time_today_is_rejected() -> None:
    with pytest.raises(ValidationError, match="в будущем"):
        create_schedule(date=local_today(), start_time="00:00", end_time="01:00")


def test_repository_creates_slots_and_they_can_be_booked(
    repository: ClinicRepository,
) -> None:
    created = repository.create_slots(
        create_schedule(date=FREE_DAY, start_time="09:00", end_time="11:00")
    )
    assert created == 4
    slots = repository.list_slots(1, FREE_DAY)
    assert [slot.starts_at.astimezone(LOCAL_TIMEZONE).strftime("%H:%M") for slot in slots] == [
        "09:00",
        "09:30",
        "10:00",
        "10:30",
    ]
    assert all(slot.ends_at - slot.starts_at == timedelta(minutes=30) for slot in slots)
    appointment = repository.book(AppointmentCreate(slot_id=slots[0].id, patient_id=1))
    assert appointment.slot_id == slots[0].id


def test_repository_skips_break(repository: ClinicRepository) -> None:
    created = repository.create_slots(
        create_schedule(
            date=FREE_DAY,
            start_time="09:00",
            end_time="12:00",
            break_start="10:00",
            break_end="10:30",
        )
    )
    assert created == 5
    starts = [
        slot.starts_at.astimezone(LOCAL_TIMEZONE).strftime("%H:%M")
        for slot in repository.list_slots(1, FREE_DAY)
    ]
    assert starts == ["09:00", "09:30", "10:30", "11:00", "11:30"]


def test_overlap_rejects_whole_interval_without_changes(
    repository: ClinicRepository,
) -> None:
    start = datetime.combine(FREE_DAY, time(9, 0), LOCAL_TIMEZONE)
    with repository._connect() as connection:
        connection.execute(
            "INSERT INTO slots (doctor_id, starts_at, ends_at) VALUES (1, ?, ?)",
            (timestamp(start), timestamp(start + timedelta(minutes=30))),
        )
    before = repository.list_slots(1, FREE_DAY)
    with pytest.raises(ConflictError, match="09:00–09:30"):
        repository.create_slots(
            create_schedule(date=FREE_DAY, start_time="08:30", end_time="10:00")
        )
    after = repository.list_slots(1, FREE_DAY)
    assert [slot.id for slot in after] == [slot.id for slot in before]


def test_exact_existing_slot_is_rejected(repository: ClinicRepository) -> None:
    start = datetime.combine(FREE_DAY, time(9, 0), LOCAL_TIMEZONE)
    with repository._connect() as connection:
        connection.execute(
            "INSERT INTO slots (doctor_id, starts_at, ends_at) VALUES (1, ?, ?)",
            (timestamp(start), timestamp(start + timedelta(minutes=30))),
        )
    with pytest.raises(ConflictError, match="09:00–09:30"):
        repository.create_slots(
            create_schedule(date=FREE_DAY, start_time="09:00", end_time="10:00")
        )
    assert len(repository.list_slots(1, FREE_DAY)) == 1


def test_existing_appointment_is_preserved_on_overlap(repository: ClinicRepository) -> None:
    appointment = repository.book(AppointmentCreate(slot_id=1, patient_id=1))
    day = repository.get_slot(1).starts_at.astimezone(LOCAL_TIMEZONE).date()
    with pytest.raises(ConflictError):
        repository.create_slots(create_schedule(date=day, start_time="09:00", end_time="11:00"))
    assert repository.get_slot(1).appointment_id == appointment.id
    assert repository.get_appointment(appointment.id).status == appointment.status


def test_doctor_must_belong_to_selected_clinic(repository: ClinicRepository) -> None:
    with pytest.raises(ConflictError, match="не относится"):
        repository.create_slots(create_schedule(clinic_id=2, doctor_id=1, date=FREE_DAY))


def test_unknown_clinic_and_doctor(repository: ClinicRepository) -> None:
    with pytest.raises(NotFoundError, match="Поликлиника"):
        repository.create_slots(create_schedule(clinic_id=999, date=FREE_DAY))
    with pytest.raises(NotFoundError, match="Врач"):
        repository.create_slots(create_schedule(doctor_id=999, date=FREE_DAY))


def test_created_slots_survive_repository_restart(repository: ClinicRepository) -> None:
    repository.create_slots(create_schedule(date=FREE_DAY))
    restarted = ClinicRepository(repository.database_path)
    restarted.initialize()
    assert len(restarted.list_slots(1, FREE_DAY)) == 4


def test_manage_page_shows_only_doctors_of_selected_clinic(client: TestClient) -> None:
    response = client.get("/schedule/manage")
    assert response.status_code == 200
    assert "Управление расписанием" in response.text
    assert 'name="doctor_id"' not in response.text

    response = client.get("/schedule/manage", params={"clinic_id": 1})
    assert response.status_code == 200
    assert "Соколова Анна Сергеевна" in response.text
    assert "Орлов Иван Петрович" in response.text
    assert "Белов Игорь Олегович" not in response.text
    assert 'name="doctor_id"' in response.text


def test_schedule_page_links_to_management(client: TestClient) -> None:
    assert 'href="/schedule/manage"' in client.get("/").text


def test_successful_creation_redirects_to_schedule(
    client: TestClient, repository: ClinicRepository
) -> None:
    response = client.post(
        "/schedule/manage",
        data={
            "clinic_id": "1",
            "doctor_id": "1",
            "date": FREE_DAY.isoformat(),
            "start_time": "09:00",
            "end_time": "11:00",
            "break_start": "",
            "break_end": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert "clinic_id=1" in location
    assert f"date={FREE_DAY.isoformat()}" in location
    assert "notice=slots-created" in location
    page = client.get(location)
    assert "Слоты добавлены в расписание." in page.text
    assert "09:00" in page.text
    assert len(repository.list_slots(1, FREE_DAY)) == 4


def test_created_slot_can_be_booked_through_html(
    client: TestClient, repository: ClinicRepository
) -> None:
    client.post(
        "/schedule/manage",
        data={
            "clinic_id": "1",
            "doctor_id": "1",
            "date": FREE_DAY.isoformat(),
            "start_time": "09:00",
            "end_time": "10:00",
            "break_start": "",
            "break_end": "",
        },
    )
    slot = repository.list_slots(1, FREE_DAY)[0]
    response = client.post(
        "/appointments",
        data={"slot_id": str(slot.id), "patient_id": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert repository.get_slot(slot.id).appointment_id is not None


def test_validation_error_keeps_values(client: TestClient, repository: ClinicRepository) -> None:
    response = client.post(
        "/schedule/manage",
        data={
            "clinic_id": "1",
            "doctor_id": "1",
            "date": FREE_DAY.isoformat(),
            "start_time": "09:10",
            "end_time": "11:00",
            "break_start": "10:00",
            "break_end": "10:30",
        },
    )
    assert response.status_code == 422
    assert 'value="09:10"' in response.text
    assert 'value="11:00"' in response.text
    assert 'value="10:00"' in response.text
    assert 'value="10:30"' in response.text
    assert '<option value="1" selected>' in response.text
    assert "кратны 30 минутам" in response.text
    assert repository.list_slots(1, FREE_DAY) == []


def test_conflict_error_keeps_values_and_does_not_create_slots(
    client: TestClient, repository: ClinicRepository
) -> None:
    day = repository.get_slot(1).starts_at.astimezone(LOCAL_TIMEZONE).date()
    before = len(repository.list_slots(1, day))
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
    )
    assert response.status_code == 409
    assert "10:00–10:30" in response.text
    assert 'value="09:00"' in response.text
    assert len(repository.list_slots(1, day)) == before


def test_missing_fields_are_explained(client: TestClient) -> None:
    response = client.post("/schedule/manage", data={})
    assert response.status_code == 422
    assert "Заполните поле" in response.text
    assert 'aria-invalid="true"' in response.text
