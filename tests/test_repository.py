import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, time, timedelta, tzinfo
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.errors import ConflictError, NotFoundError
from app.main import create_app
from app.models import (
    LOCAL_TIMEZONE,
    AppointmentCreate,
    PatientCreate,
    ScheduleCreate,
    VisitComplete,
    local_today,
)
from app.repository import ClinicRepository, timestamp


def test_concurrent_bookings_for_one_slot(repository: ClinicRepository) -> None:
    barrier = Barrier(2)

    def book(patient_id: int) -> str:
        barrier.wait(timeout=5)
        try:
            repository.book(AppointmentCreate(slot_id=1, patient_id=patient_id))
        except ConflictError:
            return "conflict"
        return "booked"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(book, (1, 2)))
    assert sorted(results) == ["booked", "conflict"]
    assert len(repository.list_appointments()) == 1


def test_concurrent_overlapping_bookings_for_one_patient(repository: ClinicRepository) -> None:
    barrier = Barrier(2)

    def book(slot_id: int) -> str:
        barrier.wait(timeout=5)
        try:
            repository.book(AppointmentCreate(slot_id=slot_id, patient_id=1))
        except ConflictError:
            return "conflict"
        return "booked"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(book, (1, 2)))
    assert sorted(results) == ["booked", "conflict"]
    assert len(repository.list_appointments()) == 1


def test_cancellation_releases_patient_time(repository: ClinicRepository) -> None:
    first = repository.book(AppointmentCreate(slot_id=1, patient_id=1))
    repository.cancel(first.id)
    second = repository.book(AppointmentCreate(slot_id=2, patient_id=1))
    assert second.slot_id == 2
    assert repository.get_slot(1).is_available


def test_restart_preserves_all_data(repository: ClinicRepository, past_appointment: int) -> None:
    patient = repository.create_patient(
        PatientCreate(full_name="Новый пациент", birth_date="2001-01-01")
    )
    booking = repository.book(AppointmentCreate(slot_id=1, patient_id=patient.id))
    completed = repository.complete(
        past_appointment, VisitComplete(note="Не терять после перезапуска")
    )
    with TestClient(
        create_app(repository.database_path, demo=True), base_url="http://127.0.0.1"
    ) as client:
        assert client.get(f"/api/appointments/{booking.id}").json()["patient_id"] == patient.id
        assert client.get(f"/api/appointments/{completed.id}").json()["note"] == completed.note
        assert len(client.get("/api/clinics").json()) == 2
        assert len(client.get("/api/patients").json()) == 3


def test_database_enforces_foreign_keys_and_unique_slot(repository: ClinicRepository) -> None:
    repository.book(AppointmentCreate(slot_id=1, patient_id=1))
    with pytest.raises(sqlite3.IntegrityError), repository._connect() as connection:
        connection.execute(
            "INSERT INTO appointments (slot_id, patient_id, created_at) VALUES (1, 2, ?)",
            (timestamp(datetime.now(UTC)),),
        )
    with pytest.raises(sqlite3.IntegrityError), repository._connect() as connection:
        connection.execute(
            "INSERT INTO appointments (slot_id, patient_id, created_at) VALUES (4, 999, ?)",
            (timestamp(datetime.now(UTC)),),
        )
    assert len(repository.list_appointments()) == 1


def test_schedule_day_uses_moscow_time(repository: ClinicRepository) -> None:
    day = local_today() + timedelta(days=4)
    before_midnight = datetime.combine(day, time(23, 30), LOCAL_TIMEZONE)
    after_midnight = before_midnight + timedelta(minutes=30)
    with repository._connect() as connection:
        connection.executemany(
            "INSERT INTO slots (doctor_id, starts_at, ends_at) VALUES (1, ?, ?)",
            [
                (timestamp(start), timestamp(start + timedelta(minutes=30)))
                for start in (before_midnight, after_midnight)
            ],
        )
    assert [slot.starts_at for slot in repository.list_slots(1, day)] == [before_midnight]
    assert [slot.starts_at for slot in repository.list_slots(1, day + timedelta(days=1))] == [
        after_midnight
    ]


def test_app_factory_does_not_create_files(tmp_path: Path) -> None:
    path = tmp_path / "not-created" / "clinic.sqlite3"
    create_app(path, demo=True)
    assert not path.parent.exists()


def schedule_data(**overrides: object) -> ScheduleCreate:
    payload: dict[str, object] = {
        "clinic_id": 1,
        "doctor_id": 1,
        "date": local_today() + timedelta(days=5),
        "start_time": "09:00",
        "end_time": "11:00",
    }
    payload.update(overrides)
    return ScheduleCreate.model_validate(payload)


def frozen_datetime(moment: datetime) -> type[datetime]:
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            if tz is None:
                return moment.replace(tzinfo=None)
            return moment.astimezone(tz)

    return FrozenDateTime


def test_create_schedule_creates_half_hour_slots(repository: ClinicRepository) -> None:
    day = local_today() + timedelta(days=5)
    result = repository.create_schedule(schedule_data(date=day))
    assert result.skipped_past == 0
    assert [
        slot.starts_at.astimezone(LOCAL_TIMEZONE).strftime("%H:%M") for slot in result.slots
    ] == [
        "09:00",
        "09:30",
        "10:00",
        "10:30",
    ]
    assert all(slot.ends_at - slot.starts_at == timedelta(minutes=30) for slot in result.slots)
    assert {slot.id for slot in repository.list_slots(1, day)} == {slot.id for slot in result.slots}


def test_create_schedule_with_break(repository: ClinicRepository) -> None:
    day = local_today() + timedelta(days=5)
    result = repository.create_schedule(
        schedule_data(
            date=day,
            start_time="09:00",
            end_time="11:00",
            break_start="09:30",
            break_end="10:00",
        )
    )
    assert [
        slot.starts_at.astimezone(LOCAL_TIMEZONE).strftime("%H:%M") for slot in result.slots
    ] == [
        "09:00",
        "10:00",
        "10:30",
    ]


def test_create_schedule_rejects_existing_slots(repository: ClinicRepository) -> None:
    day = local_today() + timedelta(days=5)
    repository.create_schedule(schedule_data(date=day, start_time="09:00", end_time="10:00"))
    with pytest.raises(ConflictError) as error:
        repository.create_schedule(schedule_data(date=day, start_time="09:30", end_time="10:30"))
    assert "уже есть слоты" in str(error.value)
    assert [
        slot.starts_at.astimezone(LOCAL_TIMEZONE).strftime("%H:%M")
        for slot in error.value.conflicts
    ] == ["09:30"]
    assert len(repository.list_slots(1, day)) == 2


def test_create_schedule_allows_adjacent_interval(repository: ClinicRepository) -> None:
    day = local_today() + timedelta(days=5)
    first = repository.create_schedule(
        schedule_data(date=day, start_time="09:00", end_time="10:00")
    )
    second = repository.create_schedule(
        schedule_data(date=day, start_time="10:00", end_time="11:00")
    )
    assert len(first.slots) == 2
    assert len(second.slots) == 2
    assert len(repository.list_slots(1, day)) == 4


def test_create_schedule_rejects_break_overlap(repository: ClinicRepository) -> None:
    day = local_today() + timedelta(days=5)
    repository.create_schedule(schedule_data(date=day, start_time="09:30", end_time="10:00"))
    with pytest.raises(ConflictError) as error:
        repository.create_schedule(
            schedule_data(
                date=day,
                start_time="09:00",
                end_time="11:00",
                break_start="09:30",
                break_end="10:00",
            )
        )
    assert "Перерыв пересекается" in str(error.value)
    assert [
        slot.starts_at.astimezone(LOCAL_TIMEZONE).strftime("%H:%M")
        for slot in error.value.conflicts
    ] == ["09:30"]


def test_create_schedule_rejects_doctor_from_other_clinic(repository: ClinicRepository) -> None:
    with pytest.raises(NotFoundError, match="в выбранной поликлинике"):
        repository.create_schedule(schedule_data(doctor_id=3))


def test_create_schedule_preserves_existing_appointment(repository: ClinicRepository) -> None:
    appointment = repository.book(AppointmentCreate(slot_id=1, patient_id=1))
    slot = repository.get_slot(1)
    local_start = slot.starts_at.astimezone(LOCAL_TIMEZONE)
    with pytest.raises(ConflictError):
        repository.create_schedule(
            schedule_data(
                date=local_start.date(),
                start_time=local_start.strftime("%H:%M"),
                end_time=(local_start + timedelta(minutes=30)).strftime("%H:%M"),
            )
        )
    assert repository.get_appointment(appointment.id).status == appointment.status
    assert repository.get_slot(1).appointment_id == appointment.id


def test_create_schedule_handles_arbitrary_existing_slot_duration(
    repository: ClinicRepository,
) -> None:
    day = local_today() + timedelta(days=5)
    start = datetime.combine(day, time(9, 15), LOCAL_TIMEZONE)
    with repository._connect() as connection:
        connection.execute(
            "INSERT INTO slots (doctor_id, starts_at, ends_at) VALUES (1, ?, ?)",
            (timestamp(start), timestamp(start + timedelta(minutes=30))),
        )
    with pytest.raises(ConflictError) as error:
        repository.create_schedule(schedule_data(date=day, start_time="09:00", end_time="10:00"))
    assert [
        slot.starts_at.astimezone(LOCAL_TIMEZONE).strftime("%H:%M")
        for slot in error.value.conflicts
    ] == ["09:15"]


def test_concurrent_schedule_creation(repository: ClinicRepository) -> None:
    day = local_today() + timedelta(days=5)
    data = schedule_data(date=day)
    barrier = Barrier(2)

    def create(_: int) -> str:
        barrier.wait(timeout=5)
        try:
            repository.create_schedule(data)
        except ConflictError:
            return "conflict"
        return "created"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, (1, 2)))
    assert sorted(results) == ["conflict", "created"]
    assert len(repository.list_slots(1, day)) == 4


def test_create_schedule_skips_past_slots_today(repository: ClinicRepository) -> None:
    day = local_today()
    fixed_now = datetime.combine(day, time(10, 15), LOCAL_TIMEZONE)
    with patch("app.repository.datetime", frozen_datetime(fixed_now)):
        result = repository.create_schedule(
            schedule_data(date=day, start_time="09:00", end_time="12:00")
        )
    assert result.skipped_past == 3
    assert [
        slot.starts_at.astimezone(LOCAL_TIMEZONE).strftime("%H:%M") for slot in result.slots
    ] == [
        "10:30",
        "11:00",
        "11:30",
    ]


def test_create_schedule_rejects_when_all_slots_past(repository: ClinicRepository) -> None:
    day = local_today()
    fixed_now = datetime.combine(day, time(12, 0), LOCAL_TIMEZONE)
    with (
        patch("app.repository.datetime", frozen_datetime(fixed_now)),
        pytest.raises(ConflictError, match="не осталось будущих"),
    ):
        repository.create_schedule(schedule_data(date=day, start_time="09:00", end_time="11:00"))
    assert repository.list_slots(1, day) == []
