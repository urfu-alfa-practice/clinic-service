"""Общие модели и правила ввода для API, HTML-форм и хранилища."""

import re
from datetime import UTC, date, datetime, time, timedelta, timezone
from enum import StrEnum
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from app.errors import ConflictError

LOCAL_TIMEZONE = timezone(timedelta(hours=3), name="МСК")
NAME_MAX_LENGTH = 120
NOTE_MAX_LENGTH = 4000
MAX_ENTITY_ID = 2**63 - 1
EntityId = Annotated[int, Field(ge=1, le=MAX_ENTITY_ID)]


def parse_entity_id(value: str) -> int | None:
    if not value.isascii() or not value.isdigit():
        return None
    normalized = value.lstrip("0")
    if len(normalized) > len(str(MAX_ENTITY_ID)):
        return None
    parsed = int(normalized) if normalized else 0
    return parsed if 1 <= parsed <= MAX_ENTITY_ID else None


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


class ScheduleForm(InputModel):
    clinic_id: str = ""
    doctor_id: str = ""
    schedule_date: str = Field(default="", alias="date")
    start_time: str = ""
    end_time: str = ""
    break_start: str = ""
    break_end: str = ""

    @field_validator("clinic_id", "doctor_id", "schedule_date", "start_time", "end_time")
    @classmethod
    def require_value(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Заполните обязательное поле.")
        return value

    @field_validator("clinic_id", "doctor_id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if parse_entity_id(value) is None:
            raise ValueError(f"Укажите целое число от 1 до {MAX_ENTITY_ID}.")
        return value

    @field_validator("schedule_date")
    @classmethod
    def validate_date(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError("Укажите дату в формате ГГГГ-ММ-ДД.")
        try:
            date.fromisoformat(value)
        except ValueError as error:
            raise ValueError("Укажите существующую календарную дату.") from error
        return value

    @field_validator("start_time", "end_time", "break_start", "break_end")
    @classmethod
    def validate_time(cls, value: str) -> str:
        if not value:
            return value
        message = "Укажите время в формате ЧЧ:ММ; минуты должны быть 00 или 30."
        if not re.fullmatch(r"[0-9]{2}:[0-9]{2}", value):
            raise ValueError(message)
        try:
            parsed = time.fromisoformat(value)
        except ValueError as error:
            raise ValueError(message) from error
        if parsed.minute not in {0, 30} or parsed.second or parsed.microsecond:
            raise ValueError(message)
        return value

    @model_validator(mode="after")
    def validate_intervals(self) -> "ScheduleForm":
        if self.start_time_value >= self.end_time_value:
            raise ValueError("Окончание рабочего интервала должно быть позже его начала.")
        if bool(self.break_start) != bool(self.break_end):
            raise ValueError("Заполните обе границы перерыва или оставьте обе пустыми.")
        if self.break_start:
            if self.break_start_value >= self.break_end_value:
                raise ValueError("Окончание перерыва должно быть позже его начала.")
            if (
                self.break_start_value < self.start_time_value
                or self.break_end_value > self.end_time_value
            ):
                raise ValueError("Перерыв должен целиком находиться внутри рабочего интервала.")
        if not self._local_slots():
            raise ValueError("После исключения перерыва не остаётся времени для создания слотов.")
        return self

    @property
    def clinic_id_value(self) -> int:
        value = parse_entity_id(self.clinic_id)
        assert value is not None
        return value

    @property
    def doctor_id_value(self) -> int:
        value = parse_entity_id(self.doctor_id)
        assert value is not None
        return value

    @property
    def date_value(self) -> date:
        return date.fromisoformat(self.schedule_date)

    @property
    def start_time_value(self) -> time:
        return time.fromisoformat(self.start_time)

    @property
    def end_time_value(self) -> time:
        return time.fromisoformat(self.end_time)

    @property
    def break_start_value(self) -> time:
        return time.fromisoformat(self.break_start)

    @property
    def break_end_value(self) -> time:
        return time.fromisoformat(self.break_end)

    def generate_slots(self, *, now: datetime) -> list[tuple[datetime, datetime]]:
        slots = self._local_slots()
        current = now.astimezone(LOCAL_TIMEZONE)
        if any(start <= current for start, _ in slots):
            raise ConflictError(
                "Нельзя создать расписание: время хотя бы одного слота уже началось или прошло."
            )
        return slots

    def _local_slots(self) -> list[tuple[datetime, datetime]]:
        start = datetime.combine(self.date_value, self.start_time_value, LOCAL_TIMEZONE)
        end = datetime.combine(self.date_value, self.end_time_value, LOCAL_TIMEZONE)
        break_start = (
            datetime.combine(self.date_value, self.break_start_value, LOCAL_TIMEZONE)
            if self.break_start
            else None
        )
        break_end = (
            datetime.combine(self.date_value, self.break_end_value, LOCAL_TIMEZONE)
            if self.break_end
            else None
        )
        slots = []
        while start < end:
            slot_end = start + timedelta(minutes=30)
            if not (
                break_start is not None
                and break_end is not None
                and start < break_end
                and slot_end > break_start
            ):
                slots.append((start, slot_end))
            start = slot_end
        return slots


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
