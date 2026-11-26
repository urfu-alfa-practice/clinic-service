"""SQLite и атомарные операции записи. Одно короткое соединение на операцию."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from app.errors import ConflictError, NotFoundError
from app.models import (
    LOCAL_TIMEZONE,
    Appointment,
    AppointmentCreate,
    AppointmentStatus,
    Clinic,
    Doctor,
    LabResult,
    Patient,
    PatientCreate,
    Slot,
    VisitComplete,
    local_today,
)

SLOT_SELECT = """
    SELECT s.id, s.doctor_id, d.full_name AS doctor_name, d.specialty,
           d.clinic_id, c.name AS clinic_name, s.starts_at, s.ends_at,
           a.id AS appointment_id
    FROM slots s
    JOIN doctors d ON d.id = s.doctor_id
    JOIN clinics c ON c.id = d.clinic_id
    LEFT JOIN appointments a ON a.slot_id = s.id AND a.status != 'cancelled'
"""

APPOINTMENT_SELECT = """
    SELECT a.*, p.full_name AS patient_name, d.full_name AS doctor_name,
           d.specialty, d.clinic_id, c.name AS clinic_name, s.starts_at, s.ends_at
    FROM appointments a
    JOIN patients p ON p.id = a.patient_id
    JOIN slots s ON s.id = a.slot_id
    JOIN doctors d ON d.id = s.doctor_id
    JOIN clinics c ON c.id = d.clinic_id
"""

SCHEMA = """
    CREATE TABLE IF NOT EXISTS clinics (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE CHECK (length(trim(name)) BETWEEN 1 AND 120),
        address TEXT NOT NULL CHECK (length(trim(address)) BETWEEN 1 AND 240)
    );
    CREATE TABLE IF NOT EXISTS doctors (
        id INTEGER PRIMARY KEY,
        clinic_id INTEGER NOT NULL REFERENCES clinics(id),
        full_name TEXT NOT NULL CHECK (length(trim(full_name)) BETWEEN 1 AND 120),
        specialty TEXT NOT NULL CHECK (length(trim(specialty)) BETWEEN 1 AND 120)
    );
    CREATE TABLE IF NOT EXISTS patients (
        id INTEGER PRIMARY KEY,
        full_name TEXT NOT NULL CHECK (length(trim(full_name)) BETWEEN 1 AND 120),
        birth_date TEXT NOT NULL,
        phone TEXT NOT NULL DEFAULT '' CHECK (length(phone) <= 30),
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS slots (
        id INTEGER PRIMARY KEY,
        doctor_id INTEGER NOT NULL REFERENCES doctors(id),
        starts_at TEXT NOT NULL,
        ends_at TEXT NOT NULL CHECK (ends_at > starts_at),
        UNIQUE (doctor_id, starts_at)
    );
    CREATE TABLE IF NOT EXISTS appointments (
        id INTEGER PRIMARY KEY,
        slot_id INTEGER NOT NULL REFERENCES slots(id),
        patient_id INTEGER NOT NULL REFERENCES patients(id),
        status TEXT NOT NULL DEFAULT 'scheduled'
            CHECK (status IN ('scheduled', 'completed', 'cancelled')),
        note TEXT NOT NULL DEFAULT '' CHECK (length(note) <= 4000),
        created_at TEXT NOT NULL,
        completed_at TEXT,
        CHECK (
            (status = 'completed' AND length(trim(note)) >= 1 AND completed_at IS NOT NULL)
            OR (status != 'completed' AND note = '' AND completed_at IS NULL)
        )
    );
    CREATE UNIQUE INDEX IF NOT EXISTS one_active_appointment_per_slot
        ON appointments (slot_id) WHERE status != 'cancelled';
    CREATE INDEX IF NOT EXISTS appointments_by_patient ON appointments (patient_id);
    CREATE INDEX IF NOT EXISTS doctors_by_clinic ON doctors (clinic_id);
    CREATE TABLE IF NOT EXISTS lab_results (
        id INTEGER PRIMARY KEY,
        patient_id INTEGER NOT NULL REFERENCES patients(id),
        study_name TEXT NOT NULL CHECK (length(trim(study_name)) BETWEEN 1 AND 120),
        result TEXT NOT NULL CHECK (length(trim(result)) BETWEEN 1 AND 4000),
        received_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS lab_results_by_patient ON lab_results (patient_id);
    PRAGMA user_version = 1;
"""


def timestamp(value: datetime) -> str:
    """Единый формат UTC нужен в том числе для сравнения дат в SQLite."""
    return value.astimezone(UTC).isoformat(timespec="seconds")


class ClinicRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            if connection.execute("PRAGMA user_version").fetchone()[0] > 1:
                raise RuntimeError("Эта база создана более новой версией приложения.")
            connection.executescript(SCHEMA)

    def list_clinics(self) -> list[Clinic]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM clinics ORDER BY id").fetchall()
        return [Clinic.model_validate(dict(row)) for row in rows]

    def get_clinic(self, clinic_id: int) -> Clinic:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM clinics WHERE id = ?", (clinic_id,)).fetchone()
        if row is None:
            raise NotFoundError("Поликлиника не найдена.")
        return Clinic.model_validate(dict(row))

    def list_doctors(self, clinic_id: int) -> list[Doctor]:
        self.get_clinic(clinic_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM doctors WHERE clinic_id = ? ORDER BY full_name, id", (clinic_id,)
            ).fetchall()
        return [Doctor.model_validate(dict(row)) for row in rows]

    def get_doctor(self, doctor_id: int) -> Doctor:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM doctors WHERE id = ?", (doctor_id,)).fetchone()
        if row is None:
            raise NotFoundError("Врач не найден.")
        return Doctor.model_validate(dict(row))

    def list_slots(self, clinic_id: int, day: date) -> list[Slot]:
        self.get_clinic(clinic_id)
        start = datetime.combine(day, time.min, LOCAL_TIMEZONE)
        end = start + timedelta(days=1) if day < date.max else datetime.max.replace(tzinfo=UTC)
        # Московская полночь 01.01.0001 выходит за нижнюю границу datetime в UTC.
        start_utc = start.astimezone(UTC) if day > date.min else datetime.min.replace(tzinfo=UTC)
        with self._connect() as connection:
            rows = connection.execute(
                SLOT_SELECT
                + " WHERE d.clinic_id = ? AND s.starts_at >= ? AND s.starts_at < ?"
                + " ORDER BY s.starts_at, d.full_name, s.id",
                (clinic_id, timestamp(start_utc), timestamp(end)),
            ).fetchall()
        return [Slot.model_validate(dict(row)) for row in rows]

    def create_schedule(
        self,
        clinic_id: int,
        doctor_id: int,
        intervals: list[tuple[datetime, datetime]],
        *,
        now: datetime | None = None,
    ) -> int:
        if not intervals:
            raise ConflictError(
                "После исключения перерыва не остаётся времени для создания слотов."
            )
        current = now if now is not None else datetime.now(UTC)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if (
                connection.execute("SELECT 1 FROM clinics WHERE id = ?", (clinic_id,)).fetchone()
                is None
            ):
                raise NotFoundError("Поликлиника не найдена.")
            doctor = connection.execute(
                "SELECT clinic_id FROM doctors WHERE id = ?", (doctor_id,)
            ).fetchone()
            if doctor is None:
                raise NotFoundError("Врач не найден.")
            if doctor["clinic_id"] != clinic_id:
                raise ConflictError("Врач не закреплён за выбранной поликлиникой.")
            if any(start <= current for start, _ in intervals):
                raise ConflictError(
                    "Нельзя создать расписание: время хотя бы одного слота уже началось или прошло."
                )
            for start, end in intervals:
                overlap = connection.execute(
                    """
                    SELECT starts_at, ends_at FROM slots
                    WHERE doctor_id = ? AND starts_at < ? AND ends_at > ? LIMIT 1
                    """,
                    (doctor_id, timestamp(end), timestamp(start)),
                ).fetchone()
                if overlap is not None:
                    existing_start = datetime.fromisoformat(overlap["starts_at"]).astimezone(
                        LOCAL_TIMEZONE
                    )
                    existing_end = datetime.fromisoformat(overlap["ends_at"]).astimezone(
                        LOCAL_TIMEZONE
                    )
                    raise ConflictError(
                        "Новый слот пересекается с существующим временем врача "
                        f"{existing_start:%d.%m.%Y} {existing_start:%H:%M}–{existing_end:%H:%M}."
                    )
            try:
                connection.executemany(
                    "INSERT INTO slots (doctor_id, starts_at, ends_at) VALUES (?, ?, ?)",
                    [(doctor_id, timestamp(start), timestamp(end)) for start, end in intervals],
                )
            except sqlite3.Error as error:
                raise ConflictError(
                    "Не удалось сохранить расписание из-за конфликта времени. "
                    "Проверьте слоты и повторите."
                ) from error
        return len(intervals)

    def get_slot(self, slot_id: int) -> Slot:
        with self._connect() as connection:
            row = connection.execute(SLOT_SELECT + " WHERE s.id = ?", (slot_id,)).fetchone()
        if row is None:
            raise NotFoundError("Время приёма не найдено.")
        return Slot.model_validate(dict(row))

    def list_patients(self) -> list[Patient]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM patients ORDER BY full_name, id").fetchall()
        return [Patient.model_validate(dict(row)) for row in rows]

    def get_patient(self, patient_id: int) -> Patient:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM patients WHERE id = ?", (patient_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("Пациент не найден.")
        return Patient.model_validate(dict(row))

    def create_patient(self, data: PatientCreate) -> Patient:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO patients (full_name, birth_date, phone, created_at) VALUES (?, ?, ?, ?)",
                (
                    data.full_name,
                    data.birth_date.isoformat(),
                    data.phone,
                    timestamp(datetime.now(UTC)),
                ),
            )
            patient_id = cursor.lastrowid
            assert patient_id is not None
        return self.get_patient(patient_id)

    def list_appointments(self, *, patient_id: int | None = None) -> list[Appointment]:
        query = APPOINTMENT_SELECT
        parameters: tuple[int, ...] = ()
        if patient_id is not None:
            query += " WHERE a.patient_id = ?"
            parameters = (patient_id,)
        query += " ORDER BY s.starts_at DESC, a.id DESC"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [Appointment.model_validate(dict(row)) for row in rows]

    def get_appointment(self, appointment_id: int) -> Appointment:
        with self._connect() as connection:
            row = connection.execute(
                APPOINTMENT_SELECT + " WHERE a.id = ?", (appointment_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("Запись на приём не найдена.")
        return Appointment.model_validate(dict(row))

    def book(self, data: AppointmentCreate) -> Appointment:
        with self._connect() as connection:
            # Все проверки и вставка под одной блокировкой: два запроса не займут одно время.
            connection.execute("BEGIN IMMEDIATE")
            if (
                connection.execute(
                    "SELECT 1 FROM patients WHERE id = ?", (data.patient_id,)
                ).fetchone()
                is None
            ):
                raise NotFoundError("Пациент не найден.")
            row = connection.execute(SLOT_SELECT + " WHERE s.id = ?", (data.slot_id,)).fetchone()
            if row is None:
                raise NotFoundError("Время приёма не найдено.")
            slot = Slot.model_validate(dict(row))
            if slot.starts_at <= datetime.now(UTC):
                raise ConflictError("Нельзя записаться на приём в прошлом или после его начала.")
            if slot.appointment_id is not None:
                raise ConflictError("Это время уже занято. Выберите другой приём.")
            overlap = connection.execute(
                """
                SELECT 1 FROM appointments a JOIN slots s ON s.id = a.slot_id
                WHERE a.patient_id = ? AND a.status != 'cancelled'
                    AND s.starts_at < ? AND s.ends_at > ? LIMIT 1
                """,
                (data.patient_id, timestamp(slot.ends_at), timestamp(slot.starts_at)),
            ).fetchone()
            if overlap:
                raise ConflictError("У пациента уже есть запись на пересекающееся время.")
            cursor = connection.execute(
                "INSERT INTO appointments (slot_id, patient_id, created_at) VALUES (?, ?, ?)",
                (data.slot_id, data.patient_id, timestamp(datetime.now(UTC))),
            )
            appointment_id = cursor.lastrowid
            assert appointment_id is not None
        return self.get_appointment(appointment_id)

    def cancel(self, appointment_id: int) -> Appointment:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM appointments WHERE id = ?", (appointment_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("Запись на приём не найдена.")
            if row["status"] == AppointmentStatus.COMPLETED:
                raise ConflictError("Завершённый приём нельзя отменить.")
            connection.execute(
                "UPDATE appointments SET status = 'cancelled' WHERE id = ?", (appointment_id,)
            )
        return self.get_appointment(appointment_id)

    def complete(self, appointment_id: int, data: VisitComplete) -> Appointment:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                APPOINTMENT_SELECT + " WHERE a.id = ?", (appointment_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("Запись на приём не найдена.")
            appointment = Appointment.model_validate(dict(row))
            if appointment.status != AppointmentStatus.SCHEDULED:
                raise ConflictError("Завершить можно только запланированный приём.")
            if appointment.starts_at > datetime.now(UTC):
                raise ConflictError("Завершить приём можно только после его начала.")
            connection.execute(
                "UPDATE appointments SET status = 'completed', note = ?, completed_at = ?"
                " WHERE id = ?",
                (data.note, timestamp(datetime.now(UTC)), appointment_id),
            )
        return self.get_appointment(appointment_id)

    def list_lab_results(self, patient_id: int) -> list[LabResult]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM lab_results WHERE patient_id = ? ORDER BY received_at DESC, id DESC",
                (patient_id,),
            ).fetchall()
        return [LabResult.model_validate(dict(row)) for row in rows]

    def seed_demo(self) -> bool:
        from app.demo import populate_demo

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # Не дополняем даже частично заполненную базу: пользовательские данные важнее примеров.
            for table in ("clinics", "doctors", "patients", "slots", "appointments", "lab_results"):
                if connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                    return False
            populate_demo(connection, today=local_today())
        return True
