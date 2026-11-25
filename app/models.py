"""Общие модели и правила ввода для API, HTML-форм и хранилища."""

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

LOCAL_TIMEZONE = timezone(timedelta(hours=3), name="МСК")
NAME_MAX_LENGTH = 120
NOTE_MAX_LENGTH = 4000
SLOT_MINUTES = 30
SLOT_DURATION = timedelta(minutes=SLOT_MINUTES)
EntityId = Annotated[int, Field(ge=1, le=2**63 - 1)]


def local_today() -> date:
    return datetime.now(LOCAL_TIMEZONE).date()


def is_slot_boundary(value: time) -> bool:
    """Граница слота должна попадать на 00 или 30 минут без секунд."""
    return value.second == 0 and value.microsecond == 0 and value.minute % SLOT_MINUTES == 0


def schedule_slot_times(
    starts_at: time,
    ends_at: time,
    break_starts_at: time | None = None,
    break_ends_at: time | None = None,
) -> list[tuple[time, time]]:
    """Разбивает рабочий интервал на получасовые слоты, исключая перерыв.

    Время рассматривается как настенное время одной даты. Для вычислений берётся
    служебная минимальная дата, поэтому интервал не может выйти за границы datetime.
    """
    base = date.min
    start = datetime.combine(base, starts_at)
    end = datetime.combine(base, ends_at)
    break_start = datetime.combine(base, break_starts_at) if break_starts_at is not None else None
    break_end = datetime.combine(base, break_ends_at) if break_ends_at is not None else None
    slots: list[tuple[time, time]] = []
    current = start
    while current + SLOT_DURATION <= end:
        slot_end = current + SLOT_DURATION
        if break_start is None or not (break_start < slot_end and break_end > current):
            slots.append((current.time(), slot_end.time()))
        current = slot_end
    return slots


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
    """Рабочий интервал врача, который нужно разбить на получасовые слоты."""

    clinic_id: EntityId
    doctor_id: EntityId
    date: date
    starts_at: time
    ends_at: time
    break_starts_at: time | None = None
    break_ends_at: time | None = None

    @model_validator(mode="before")
    @classmethod
    def empty_break_is_none(cls, data: object) -> object:
        if isinstance(data, dict):
            data = dict(data)
            for field in ("break_starts_at", "break_ends_at"):
                value = data.get(field)
                if isinstance(value, str) and not value.strip():
                    data[field] = None
        return data

    @field_validator("starts_at", "ends_at", "break_starts_at", "break_ends_at")
    @classmethod
    def validate_slot_step(cls, value: time | None) -> time | None:
        if value is None:
            return value
        if value.tzinfo is not None:
            raise ValueError("Укажите местное время без часового пояса.")
        if not is_slot_boundary(value):
            raise ValueError("Время должно быть кратно 30 минутам, например 09:00 или 09:30.")
        return value

    @model_validator(mode="after")
    def validate_interval(self) -> "ScheduleCreate":
        if self.starts_at >= self.ends_at:
            raise ValueError("Окончание рабочего интервала должно быть позже начала.")
        if (self.break_starts_at is None) != (self.break_ends_at is None):
            raise ValueError("Заполните оба поля перерыва или оставьте их пустыми.")
        if (
            self.break_starts_at is not None
            and self.break_ends_at is not None
            and not (self.starts_at < self.break_starts_at < self.break_ends_at < self.ends_at)
        ):
            raise ValueError("Перерыв должен находиться внутри рабочего интервала.")
        if not schedule_slot_times(
            self.starts_at,
            self.ends_at,
            self.break_starts_at,
            self.break_ends_at,
        ):
            raise ValueError(
                "Интервал должен содержать хотя бы один получасовой слот вне перерыва."
            )
        # Московское время на нижней границе date.min не переводится в UTC.
        try:
            datetime.combine(self.date, self.starts_at, LOCAL_TIMEZONE).astimezone(UTC)
            datetime.combine(self.date, self.ends_at, LOCAL_TIMEZONE).astimezone(UTC)
        except OverflowError as error:
            raise ValueError("Дата и время выходят за поддерживаемый диапазон.") from error
        return self


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
