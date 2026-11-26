from datetime import time, timedelta

import pytest
from pydantic import ValidationError

from app.models import ScheduleCreate, local_today

TOMORROW = local_today() + timedelta(days=1)


def schedule(**overrides: object) -> ScheduleCreate:
    payload: dict[str, object] = {
        "clinic_id": 1,
        "doctor_id": 1,
        "date": TOMORROW,
        "start_time": "09:00",
        "end_time": "11:00",
    }
    payload.update(overrides)
    return ScheduleCreate.model_validate(payload)


def test_valid_interval_without_break() -> None:
    data = schedule()
    assert data.start_time == time(9, 0)
    assert data.end_time == time(11, 0)
    assert data.break_start is None
    assert data.break_end is None


def test_valid_interval_with_break() -> None:
    data = schedule(start_time="09:00", end_time="12:00", break_start="10:00", break_end="10:30")
    assert data.break_start == time(10, 0)
    assert data.break_end == time(10, 30)


def test_empty_break_strings_mean_no_break() -> None:
    data = schedule(break_start="", break_end="")
    assert data.break_start is None
    assert data.break_end is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("start_time", "09:15"),
        ("end_time", "11:15"),
        ("break_start", "10:15"),
        ("break_end", "10:45"),
    ],
)
def test_time_must_be_on_half_hour_grid(field: str, value: str) -> None:
    payload: dict[str, object] = {"break_start": "10:00", "break_end": "10:30"}
    payload[field] = value
    with pytest.raises(ValidationError, match="кратно 30 минутам"):
        schedule(**payload)


def test_end_must_be_after_start() -> None:
    with pytest.raises(ValidationError, match="позже его начала"):
        schedule(start_time="11:00", end_time="09:00")


def test_end_equal_to_start_is_rejected() -> None:
    with pytest.raises(ValidationError, match="позже его начала"):
        schedule(start_time="09:00", end_time="09:00")


@pytest.mark.parametrize(
    "break_start,break_end",
    [
        ("09:00", "09:30"),
        ("10:30", "11:00"),
        ("08:30", "09:00"),
        ("10:30", "11:30"),
    ],
)
def test_break_must_be_strictly_inside_interval(break_start: str, break_end: str) -> None:
    with pytest.raises(ValidationError, match="внутри рабочего интервала"):
        schedule(break_start=break_start, break_end=break_end)


def test_break_end_must_be_after_start() -> None:
    with pytest.raises(ValidationError, match="Окончание перерыва"):
        schedule(break_start="10:00", break_end="10:00")


@pytest.mark.parametrize(
    "overrides",
    [
        {"break_start": "10:00"},
        {"break_end": "10:30"},
    ],
)
def test_break_requires_both_fields(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="оба поля"):
        schedule(**overrides)


def test_past_date_is_rejected() -> None:
    with pytest.raises(ValidationError, match="не может быть в прошлом"):
        schedule(date=local_today() - timedelta(days=1))


def test_timezone_aware_time_is_rejected() -> None:
    with pytest.raises(ValidationError, match="без часового пояса"):
        schedule(start_time="09:00+03:00")


def test_extra_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        schedule(unknown="value")
