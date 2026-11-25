"""Общие модели и правила ввода для API, HTML-форм и хранилища."""

from datetime import UTC, date, datetime, time, timedelta, timezone
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

LOCAL_TIMEZONE = timezone(timedelta(hours=3), name="МСК")
NAME_MAX_LENGTH = 120
NOTE_MAX_LENGTH = 4000
SLOT_MINUTES = 30
EntityId = Annotated[int, Field(ge=1, le=2**63 - 1)]


def local_today() -> date:
    return datetime.now(LOCAL_TIMEZONE).date()


class InputModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def reject_null_character(cls, value: object) -> object:
        if isinstance(value, str) and "\x00" in value:
            raise ValueError("Нулевой символ в тексте недопустим.")
        return value


class PatientCreate(InputModel):
    full_name: str = Field(min_length=1, max_length=NAME_MAX_LENGTH)
    birth_date: date
    phone: str = Field(default="", max_length=30)

    @field_validator("birth_date")
    @classmethod
    def validate_birth_date(cls, value: date) -> date:
        if value > local_today():
            raise ValueError("Дата рождения не может быть в будущем.")
        return value

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        if value and (
            any(char not in "+0123456789 ()-" for char in value)
            or not 7 <= sum(char.isdigit() for char in value) <= 15
        ):
            raise ValueError("Укажите от 7 до 15 цифр; допустимы +, пробелы, скобки и дефисы.")
        return value


class Patient(PatientCreate):
    id: EntityId
    created_at: datetime


class Clinic(BaseModel):
    id: EntityId
    name: str
    address: str


class Doctor(BaseModel):
    id: EntityId
    clinic_id: EntityId
    full_name: str
    specialty: str


class ScheduleCreate(InputModel):
    """Рабочий интервал врача. Время указывается по московскому времени."""

    clinic_id: EntityId
    doctor_id: EntityId
    date: date
    start_time: time
    end_time: time
    break_start: time | None = None
    break_end: time | None = None

    @field_validator("start_time", "end_time", "break_start", "break_end")
    @classmethod
    def validate_slot_boundary(cls, value: time | None) -> time | None:
        if value is not None and value.tzinfo is not None:
            raise ValueError("Укажите местное время без часового пояса.")
        if value is not None and (
            value.minute not in (0, SLOT_MINUTES) or value.second or value.microsecond
        ):
            raise ValueError("Время должно быть кратно 30 минутам: например, 09:00 или 09:30.")
        return value

    @model_validator(mode="after")
    def validate_interval(self) -> "ScheduleCreate":
        if self.start_time >= self.end_time:
            raise ValueError("Окончание рабочего интервала должно быть позже начала.")
        if (self.break_start is None) != (self.break_end is None):
            raise ValueError(
                "Укажите и начало, и окончание перерыва — или оставьте оба поля пустыми."
            )
        if self.break_start is not None and self.break_end is not None:
            if self.break_start >= self.break_end:
                raise ValueError("Окончание перерыва должно быть позже его начала.")
            if self.break_start < self.start_time or self.break_end > self.end_time:
                raise ValueError("Перерыв должен полностью находиться внутри рабочего интервала.")
        if not self.slot_times():
            raise ValueError("В интервале нет ни одного полного 30-минутного слота.")
        return self

    def slot_times(self) -> list[tuple[time, time]]:
        """Полные получасовые слоты интервала без времени перерыва."""
        start = self.start_time.hour * 60 + self.start_time.minute
        end = self.end_time.hour * 60 + self.end_time.minute
        break_start = (
            self.break_start.hour * 60 + self.break_start.minute
            if self.break_start is not None
            else None
        )
        break_end = (
            self.break_end.hour * 60 + self.break_end.minute if self.break_end is not None else None
        )
        slots = []
        for minute in range(start, end - SLOT_MINUTES + 1, SLOT_MINUTES):
            slot_end = minute + SLOT_MINUTES
            if (
                break_start is not None
                and break_end is not None
                and minute < break_end
                and slot_end > break_start
            ):
                continue
            slots.append((time(minute // 60, minute % 60), time(slot_end // 60, slot_end % 60)))
        return slots


class Slot(BaseModel):
    id: EntityId
    doctor_id: EntityId
    doctor_name: str
    specialty: str
    clinic_id: EntityId
    clinic_name: str
    starts_at: datetime
    ends_at: datetime
    appointment_id: EntityId | None = None

    @computed_field
    @property
    def is_available(self) -> bool:
        return self.appointment_id is None and self.starts_at > datetime.now(UTC)


class AppointmentStatus(StrEnum):
    SCHEDULED = "scheduled"
    COMPLETED = "completed"
    CANCELLED = "cancelled"

    @property
    def label(self) -> str:
        return {
            self.SCHEDULED: "Запланирован",
            self.COMPLETED: "Завершён",
            self.CANCELLED: "Отменён",
        }[self]


class AppointmentCreate(InputModel):
    slot_id: EntityId
    patient_id: EntityId


class VisitComplete(InputModel):
    note: str = Field(min_length=1, max_length=NOTE_MAX_LENGTH)


class Appointment(BaseModel):
    id: EntityId
    slot_id: EntityId
    patient_id: EntityId
    patient_name: str
    doctor_name: str
    specialty: str
    clinic_id: EntityId
    clinic_name: str
    starts_at: datetime
    ends_at: datetime
    status: AppointmentStatus
    note: str
    created_at: datetime
    completed_at: datetime | None


class LabResult(BaseModel):
    id: EntityId
    patient_id: EntityId
    study_name: str
    result: str
    received_at: datetime


class PatientCard(BaseModel):
    patient: Patient
    appointments: list[Appointment]
    lab_results: list[LabResult]
