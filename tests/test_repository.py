import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from app.errors import ConflictError, NotFoundError
from app.main import create_app
from app.models import (
    LOCAL_TIMEZONE,
    AppointmentCreate,
    PatientCreate,
    ScheduleCreate,
    Slot,
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


def schedule_payload(day: date, **overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "clinic_id": 1,
        "doctor_id": 1,
        "date": day.isoformat(),
        "start_time": "09:00",
        "end_time": "11:00",
    }
    data.update(overrides)
    return data


def add_schedule(repository: ClinicRepository, day: date, **overrides: object) -> list[Slot]:
    return repository.add_schedule(
        ScheduleCreate.model_validate(schedule_payload(day, **overrides))
    )


def test_add_schedule_creates_half_hour_slots(repository: ClinicRepository) -> None:
    day = local_today() + timedelta(days=3)
    slots = add_schedule(repository, day)
    expected = [
        datetime.combine(day, time(hour, minute), LOCAL_TIMEZONE).astimezone(UTC)
        for hour, minute in ((9, 0), (9, 30), (10, 0), (10, 30))
    ]
    assert [slot.starts_at for slot in slots] == expected
    assert all(slot.ends_at == slot.starts_at + timedelta(minutes=30) for slot in slots)
    assert all(slot.doctor_id == 1 and slot.is_available for slot in slots)


def test_add_schedule_excludes_break(repository: ClinicRepository) -> None:
    day = local_today() + timedelta(days=4)
    slots = add_schedule(repository, day, break_start="09:30", break_end="10:00")
    expected = [
        datetime.combine(day, time(hour, minute), LOCAL_TIMEZONE).astimezone(UTC)
        for hour, minute in ((9, 0), (10, 0), (10, 30))
    ]
    assert [slot.starts_at for slot in slots] == expected


def test_add_schedule_keeps_existing_slots_and_appointments(
    repository: ClinicRepository,
) -> None:
    day = repository.get_slot(1).starts_at.astimezone(LOCAL_TIMEZONE).date()
    booking = repository.book(AppointmentCreate(slot_id=1, patient_id=1))
    before = repository.list_slots(1, day)
    slots = add_schedule(repository, day, start_time="11:00", end_time="12:00")
    assert len(slots) == 2
    assert len(repository.list_slots(1, day)) == len(before) + 2
    assert repository.get_appointment(booking.id) == booking
    assert repository.get_slot(1).appointment_id == booking.id


def test_add_schedule_rejects_overlap(repository: ClinicRepository) -> None:
    day = repository.get_slot(4).starts_at.astimezone(LOCAL_TIMEZONE).date()
    before = repository.list_slots(1, day)
    with pytest.raises(ConflictError, match="пересекается"):
        add_schedule(repository, day, start_time="10:30", end_time="11:30")
    assert repository.list_slots(1, day) == before


def test_add_schedule_rejects_doctor_from_other_clinic(repository: ClinicRepository) -> None:
    day = local_today() + timedelta(days=5)
    with pytest.raises(NotFoundError, match="Врач не найден"):
        add_schedule(repository, day, clinic_id=2, doctor_id=1)
    assert repository.list_slots(1, day) == []


def test_concurrent_add_schedule_creates_only_one_interval(repository: ClinicRepository) -> None:
    day = local_today() + timedelta(days=6)
    barrier = Barrier(2)

    def add() -> str:
        barrier.wait(timeout=5)
        try:
            add_schedule(repository, day)
        except ConflictError:
            return "conflict"
        return "created"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(add) for _ in range(2)]
        results = [future.result() for future in futures]
    assert sorted(results) == ["conflict", "created"]
    assert len(repository.list_slots(1, day)) == 4
