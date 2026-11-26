import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from app.errors import ConflictError
from app.main import create_app
from app.models import (
    LOCAL_TIMEZONE,
    AppointmentCreate,
    PatientCreate,
    ScheduleForm,
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


def schedule_intervals(start: datetime, count: int = 1) -> list[tuple[datetime, datetime]]:
    return [
        (start + timedelta(minutes=30 * index), start + timedelta(minutes=30 * (index + 1)))
        for index in range(count)
    ]


def all_slot_rows(repository: ClinicRepository) -> list[tuple[object, ...]]:
    with repository._connect() as connection:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT id, doctor_id, starts_at, ends_at FROM slots ORDER BY id"
            )
        ]


def test_create_schedule_preserves_existing_slots_and_appointments(
    repository: ClinicRepository,
) -> None:
    appointment = repository.book(AppointmentCreate(slot_id=1, patient_id=1))
    slots_before = all_slot_rows(repository)
    appointments_before = repository.list_appointments()
    start = repository.get_slot(1).starts_at + timedelta(days=1)

    created = repository.create_schedule(
        1,
        1,
        schedule_intervals(start, 2),
        now=start - timedelta(minutes=1),
    )

    assert created == 2
    assert all_slot_rows(repository)[: len(slots_before)] == slots_before
    assert repository.list_appointments() == appointments_before
    assert repository.get_appointment(appointment.id).patient_id == 1
    assert len(repository.list_slots(1, start.astimezone(LOCAL_TIMEZONE).date())) == 2


def test_create_schedule_allows_adjacent_slots(repository: ClinicRepository) -> None:
    existing_start = repository.get_slot(1).starts_at + timedelta(days=1)
    existing_end = existing_start + timedelta(minutes=30)
    now = existing_start - timedelta(hours=1)
    repository.create_schedule(1, 1, [(existing_start, existing_end)], now=now)
    intervals = [
        (existing_start - timedelta(minutes=30), existing_start),
        (existing_end, existing_end + timedelta(minutes=30)),
    ]

    assert repository.create_schedule(1, 1, intervals, now=now) == 2
    assert len(all_slot_rows(repository)) == 9


def test_create_schedule_allows_same_time_for_different_doctors(
    repository: ClinicRepository,
) -> None:
    start = repository.get_slot(1).starts_at + timedelta(days=1)
    now = start - timedelta(minutes=1)

    repository.create_schedule(1, 1, schedule_intervals(start), now=now)
    repository.create_schedule(1, 2, schedule_intervals(start), now=now)

    with repository._connect() as connection:
        rows = connection.execute(
            "SELECT doctor_id FROM slots WHERE starts_at = ? ORDER BY doctor_id",
            (timestamp(start),),
        ).fetchall()
    assert [row["doctor_id"] for row in rows] == [1, 2]


def test_create_schedule_conflicts_with_cancelled_appointment_slot(
    repository: ClinicRepository,
) -> None:
    appointment = repository.book(AppointmentCreate(slot_id=1, patient_id=1))
    repository.cancel(appointment.id)
    existing = repository.get_slot(1)
    before = all_slot_rows(repository)

    with pytest.raises(ConflictError, match="10:00"):
        repository.create_schedule(
            1,
            1,
            [(existing.starts_at, existing.ends_at)],
            now=existing.starts_at - timedelta(minutes=1),
        )
    assert all_slot_rows(repository) == before


def test_create_schedule_allows_slots_inside_previous_break(
    repository: ClinicRepository,
) -> None:
    day = (repository.get_slot(1).starts_at + timedelta(days=7)).astimezone(LOCAL_TIMEZONE)
    first_start = day.replace(hour=9, minute=0, second=0, microsecond=0)
    original = [
        (first_start, first_start + timedelta(minutes=30)),
        (first_start + timedelta(minutes=30), first_start + timedelta(hours=1)),
        (first_start + timedelta(hours=2), first_start + timedelta(hours=2, minutes=30)),
        (
            first_start + timedelta(hours=2, minutes=30),
            first_start + timedelta(hours=3),
        ),
    ]
    now = first_start - timedelta(hours=1)
    repository.create_schedule(1, 1, original, now=now)

    created = repository.create_schedule(
        1,
        1,
        schedule_intervals(first_start + timedelta(hours=1), 2),
        now=now,
    )

    assert created == 2
    assert len(repository.list_slots(1, first_start.date())) == 6


def test_create_schedule_rejects_doctor_from_another_clinic(
    repository: ClinicRepository,
) -> None:
    start = repository.get_slot(1).starts_at + timedelta(days=1)
    before = all_slot_rows(repository)

    with pytest.raises(ConflictError, match="поликлиник"):
        repository.create_schedule(
            2, 1, schedule_intervals(start), now=start - timedelta(minutes=1)
        )
    assert all_slot_rows(repository) == before


@pytest.mark.parametrize(
    "existing_minutes,new_start_offset,new_end_offset",
    [
        (30, -30, 15),
        (30, 15, 60),
        (30, -30, 90),
        (60, 15, 45),
        (75, 60, 120),
    ],
    ids=["left", "right", "contains", "inside", "non-half-hour-existing"],
)
def test_create_schedule_overlap_matrix_rolls_back_all_new_slots(
    repository: ClinicRepository,
    existing_minutes: int,
    new_start_offset: int,
    new_end_offset: int,
) -> None:
    existing_start = repository.get_slot(1).starts_at + timedelta(days=1)
    now = existing_start - timedelta(hours=2)
    repository.create_schedule(
        1,
        1,
        [(existing_start, existing_start + timedelta(minutes=existing_minutes))],
        now=now,
    )
    before = all_slot_rows(repository)

    with pytest.raises(ConflictError):
        repository.create_schedule(
            1,
            1,
            [
                (
                    existing_start + timedelta(minutes=new_start_offset),
                    existing_start + timedelta(minutes=new_end_offset),
                )
            ],
            now=now,
        )
    assert all_slot_rows(repository) == before


@pytest.mark.parametrize("delay", [timedelta(), timedelta(seconds=1)], ids=["current", "past"])
def test_create_schedule_rechecks_time_after_form_validation(
    repository: ClinicRepository, delay: timedelta
) -> None:
    day = (repository.get_slot(1).starts_at + timedelta(days=10)).astimezone(LOCAL_TIMEZONE)
    data = ScheduleForm.model_validate(
        {
            "clinic_id": "1",
            "doctor_id": "1",
            "date": day.date().isoformat(),
            "start_time": "09:00",
            "end_time": "10:00",
            "break_start": "",
            "break_end": "",
        }
    )
    first_start = day.replace(hour=9, minute=0, second=0, microsecond=0)
    intervals = data.generate_slots(now=first_start - timedelta(minutes=1))
    before = all_slot_rows(repository)

    with pytest.raises(ConflictError, match="начал"):
        repository.create_schedule(1, 1, intervals, now=first_start + delay)
    assert all_slot_rows(repository) == before


def test_create_schedule_conflict_rolls_back_whole_submission(
    repository: ClinicRepository,
) -> None:
    existing = repository.get_slot(1)
    intervals = [
        (existing.starts_at - timedelta(minutes=30), existing.starts_at),
        (existing.starts_at, existing.ends_at),
    ]
    before = all_slot_rows(repository)

    with pytest.raises(ConflictError):
        repository.create_schedule(1, 1, intervals, now=existing.starts_at - timedelta(hours=1))
    assert all_slot_rows(repository) == before


def test_create_schedule_insert_error_rolls_back_previous_inserts_and_hides_sql(
    repository: ClinicRepository,
) -> None:
    start = repository.get_slot(1).starts_at + timedelta(days=1)
    before = all_slot_rows(repository)
    with repository._connect() as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_eighth_slot BEFORE INSERT ON slots
            WHEN (SELECT COUNT(*) FROM slots) = 7
            BEGIN SELECT RAISE(ABORT, 'forced insertion failure'); END
            """
        )

    with pytest.raises(ConflictError) as caught:
        repository.create_schedule(
            1, 1, schedule_intervals(start, 2), now=start - timedelta(minutes=1)
        )

    message = str(caught.value)
    assert "расписан" in message.lower()
    assert "forced insertion failure" not in message
    assert "INSERT" not in message
    assert "SQL" not in message
    assert all_slot_rows(repository) == before


def test_concurrent_identical_schedule_creation_has_one_winner(
    repository: ClinicRepository,
) -> None:
    start = repository.get_slot(1).starts_at + timedelta(days=1)
    intervals = schedule_intervals(start, 2)
    now = start - timedelta(minutes=1)
    barrier = Barrier(2)

    def create() -> str:
        barrier.wait(timeout=5)
        try:
            repository.create_schedule(1, 1, intervals, now=now)
        except ConflictError:
            return "conflict"
        return "created"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create(), range(2)))

    assert sorted(results) == ["conflict", "created"]
    with repository._connect() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM slots WHERE doctor_id = 1 AND starts_at >= ?",
            (timestamp(start),),
        ).fetchone()[0]
    assert count == 2


def test_concurrent_partially_overlapping_schedules_keep_one_valid_set(
    repository: ClinicRepository,
) -> None:
    start = repository.get_slot(1).starts_at + timedelta(days=1)
    now = start - timedelta(minutes=1)
    schedules = [
        schedule_intervals(start, 2),
        schedule_intervals(start + timedelta(minutes=30), 2),
    ]
    barrier = Barrier(2)

    def create(intervals: list[tuple[datetime, datetime]]) -> str:
        barrier.wait(timeout=5)
        try:
            repository.create_schedule(1, 1, intervals, now=now)
        except ConflictError:
            return "conflict"
        return "created"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, schedules))

    assert sorted(results) == ["conflict", "created"]
    with repository._connect() as connection:
        rows = connection.execute(
            """
            SELECT starts_at, ends_at FROM slots
            WHERE doctor_id = 1 AND ends_at > ? ORDER BY starts_at
            """,
            (timestamp(start),),
        ).fetchall()
    assert len(rows) == 2
    assert all(
        first["ends_at"] <= second["starts_at"]
        for first, second in zip(rows, rows[1:], strict=False)
    )
