"""Общие модели и правила ввода для API, HTML-форм и хранилища."""

from datetime import UTC, date, datetime, time, timedelta, timezone
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

LOCAL_TIMEZONE = timezone(timedelta(hours=3), name="МСК")
NAME_MAX_LENGTH = 120
NOTE_MAX_LENGTH = 4000
SCHEDULE_STEP_MINUTES = 30
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


def is_schedule_step(value: time) -> bool:
    return (
        value.minute % SCHEDULE_STEP_MINUTES == 0 and value.second == 0 and value.microsecond == 0
    )


class ScheduleCreate(InputModel):
    clinic_id: EntityId
    doctor_id: EntityId
    date: date
    start_time: time
    end_time: time
    break_start: time | None = None
    break_end: time | None = None

    @field_validator("break_start", "break_end", mode="before")
    @classmethod
    def empty_break_to_none(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value

    @field_validator("date")
    @classmethod
    def validate_date(cls, value: date) -> date:
        if value < local_today():
            raise ValueError("Расписание нельзя создавать в прошлом.")
        return value

    @model_validator(mode="after")
    def validate_interval(self) -> "ScheduleCreate":
        if not is_schedule_step(self.start_time) or not is_schedule_step(self.end_time):
            raise ValueError("Начало и окончание рабочего интервала должны быть кратны 30 минутам.")
        if self.end_time <= self.start_time:
            raise ValueError("Окончание рабочего интервала должно быть позже начала.")
        if (self.break_start is None) != (self.break_end is None):
            raise ValueError("Укажите обе границы перерыва или оставьте обе пустыми.")
        if self.break_start is not None and self.break_end is not None:
            if not is_schedule_step(self.break_start) or not is_schedule_step(self.break_end):
                raise ValueError("Начало и окончание перерыва должны быть кратны 30 минутам.")
            if self.break_end <= self.break_start:
                raise ValueError("Окончание перерыва должно быть позже начала.")
            if self.break_start < self.start_time or self.break_end > self.end_time:
                raise ValueError("Перерыв должен находиться внутри рабочего интервала.")
        if not self.slot_ranges():
            raise ValueError("После перерыва не остаётся времени приёма.")
        starts_at = datetime.combine(self.date, self.start_time, LOCAL_TIMEZONE)
        if starts_at <= datetime.now(UTC):
            raise ValueError("Начало рабочего интервала должно быть в будущем.")
        return self

    def slot_ranges(self) -> list[tuple[datetime, datetime]]:
        start = datetime.combine(self.date, self.start_time, LOCAL_TIMEZONE)
        end = datetime.combine(self.date, self.end_time, LOCAL_TIMEZONE)
        break_start = (
            datetime.combine(self.date, self.break_start, LOCAL_TIMEZONE)
            if self.break_start is not None
            else None
        )
        break_end = (
            datetime.combine(self.date, self.break_end, LOCAL_TIMEZONE)
            if self.break_end is not None
            else None
        )
        step = timedelta(minutes=SCHEDULE_STEP_MINUTES)
        ranges = []
        cursor = start
        while cursor < end:
            slot_end = cursor + step
            if not (
                break_start is not None
                and break_end is not None
                and cursor < break_end
                and slot_end > break_start
            ):
                ranges.append((cursor, slot_end))
            cursor = slot_end
        return ranges


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
