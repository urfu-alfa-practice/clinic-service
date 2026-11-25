import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, time, timedelta
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


def local_times(slots: list[Slot]) -> list[tuple[str, str]]:
    return [
        (
            slot.starts_at.astimezone(LOCAL_TIMEZONE).strftime("%H:%M"),
            slot.ends_at.astimezone(LOCAL_TIMEZONE).strftime("%H:%M"),
        )
        for slot in slots
    ]


def test_create_schedule_splits_interval_into_slots(repository: ClinicRepository) -> None:
    day = local_today() + timedelta(days=10)
    slots = repository.create_schedule(
        ScheduleCreate(clinic_id=1, doctor_id=1, date=day, starts_at="09:00", ends_at="11:00")
    )
    assert local_times(slots) == [
        ("09:00", "09:30"),
        ("09:30", "10:00"),
        ("10:00", "10:30"),
        ("10:30", "11:00"),
    ]
    assert [slot.id for slot in repository.list_slots(1, day)] == [slot.id for slot in slots]
    assert (
        repository.book(AppointmentCreate(slot_id=slots[0].id, patient_id=1)).slot_id == slots[0].id
    )


def test_create_schedule_skips_break(repository: ClinicRepository) -> None:
    day = local_today() + timedelta(days=10)
    slots = repository.create_schedule(
        ScheduleCreate(
            clinic_id=1,
            doctor_id=1,
            date=day,
            starts_at="09:00",
            ends_at="11:00",
            break_starts_at="10:00",
            break_ends_at="10:30",
        )
    )
    assert local_times(slots) == [
        ("09:00", "09:30"),
        ("09:30", "10:00"),
        ("10:30", "11:00"),
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {"starts_at": "09:15", "ends_at": "11:00"},
        {"starts_at": "09:00", "ends_at": "10:45"},
        {"starts_at": "09:00", "ends_at": "11:00", "break_starts_at": "10:15"},
        {"starts_at": "09:00", "ends_at": "09:00"},
        {"starts_at": "10:00", "ends_at": "09:00"},
        {
            "starts_at": "09:00",
            "ends_at": "11:00",
            "break_starts_at": "10:00",
            "break_ends_at": "10:15",
        },
        {
            "starts_at": "09:00",
            "ends_at": "11:00",
            "break_starts_at": "08:30",
            "break_ends_at": "09:00",
        },
        {
            "starts_at": "09:00",
            "ends_at": "11:00",
            "break_starts_at": "10:00",
            "break_ends_at": "11:00",
        },
        {
            "starts_at": "09:00",
            "ends_at": "09:30",
            "break_starts_at": "09:00",
            "break_ends_at": "09:30",
        },
    ],
)
def test_invalid_schedule_is_rejected(
    repository: ClinicRepository, payload: dict[str, str]
) -> None:
    day = local_today() + timedelta(days=10)
    with pytest.raises(ValidationError):
        ScheduleCreate(clinic_id=1, doctor_id=1, date=day, **payload)
    assert repository.list_slots(1, day) == []


def test_schedule_rejects_overlap_without_partial_changes(
    repository: ClinicRepository,
) -> None:
    day = local_today() + timedelta(days=10)
    existing = repository.create_schedule(
        ScheduleCreate(clinic_id=1, doctor_id=1, date=day, starts_at="10:00", ends_at="10:30")
    )
    with pytest.raises(ConflictError, match="пересекающийся"):
        repository.create_schedule(
            ScheduleCreate(clinic_id=1, doctor_id=1, date=day, starts_at="09:00", ends_at="11:00")
        )
    assert [slot.id for slot in repository.list_slots(1, day)] == [existing[0].id]


def test_schedule_allows_adjacent_interval(repository: ClinicRepository) -> None:
    day = local_today() + timedelta(days=10)
    repository.create_schedule(
        ScheduleCreate(clinic_id=1, doctor_id=1, date=day, starts_at="09:00", ends_at="09:30")
    )
    slots = repository.create_schedule(
        ScheduleCreate(clinic_id=1, doctor_id=1, date=day, starts_at="09:30", ends_at="10:30")
    )
    assert local_times(slots) == [("09:30", "10:00"), ("10:00", "10:30")]
    assert len(repository.list_slots(1, day)) == 3


def test_schedule_requires_doctor_from_selected_clinic(
    repository: ClinicRepository,
) -> None:
    day = local_today() + timedelta(days=10)
    with pytest.raises(NotFoundError, match="не работает"):
        repository.create_schedule(
            ScheduleCreate(clinic_id=1, doctor_id=3, date=day, starts_at="09:00", ends_at="10:00")
        )
    assert repository.list_slots(1, day) == []


def test_schedule_can_be_created_for_past_date(repository: ClinicRepository) -> None:
    day = local_today() - timedelta(days=3)
    slots = repository.create_schedule(
        ScheduleCreate(clinic_id=1, doctor_id=1, date=day, starts_at="09:00", ends_at="09:30")
    )
    assert local_times(slots) == [("09:00", "09:30")]
    assert repository.get_slot(slots[0].id).is_available is False


def test_schedule_preserves_existing_appointments(repository: ClinicRepository) -> None:
    appointment = repository.book(AppointmentCreate(slot_id=1, patient_id=1))
    day = local_today() + timedelta(days=10)
    repository.create_schedule(
        ScheduleCreate(clinic_id=1, doctor_id=1, date=day, starts_at="09:00", ends_at="10:00")
    )
    assert repository.get_appointment(appointment.id) == appointment
    assert repository.get_slot(1).appointment_id == appointment.id
