from datetime import datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from app.errors import ConflictError
from app.models import LOCAL_TIMEZONE, ScheduleForm


def valid_form(**changes: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "clinic_id": "1",
        "doctor_id": "2",
        "date": "2099-01-02",
        "start_time": "09:00",
        "end_time": "12:00",
        "break_start": "10:00",
        "break_end": "10:30",
    }
    values.update(changes)
    return values


def validate_errors(values: dict[str, Any]) -> dict[tuple[Any, ...], list[str]]:
    with pytest.raises(ValidationError) as caught:
        ScheduleForm.model_validate(values)
    messages: dict[tuple[Any, ...], list[str]] = {}
    for error in caught.value.errors():
        messages.setdefault(error["loc"], []).append(error["msg"])
    return messages


@pytest.mark.parametrize("field", ["clinic_id", "doctor_id", "date", "start_time", "end_time"])
@pytest.mark.parametrize("value", ["", "   "])
def test_schedule_form_requires_fields(field: str, value: str) -> None:
    errors = validate_errors(valid_form(**{field: value}))
    assert (field,) in errors
    assert "обязатель" in errors[(field,)][0].lower()


@pytest.mark.parametrize("field", ["clinic_id", "doctor_id"])
@pytest.mark.parametrize("value", ["abc", "1.5", "0", "-1", "9223372036854775808", "9" * 5000])
def test_schedule_form_rejects_invalid_identifiers(field: str, value: str) -> None:
    errors = validate_errors(valid_form(**{field: value}))
    assert (field,) in errors
    assert "целое число" in errors[(field,)][0]


@pytest.mark.parametrize("value", ["1", "9223372036854775807"])
def test_schedule_form_accepts_identifier_boundaries(value: str) -> None:
    data = ScheduleForm.model_validate(valid_form(clinic_id=value, doctor_id=value))
    assert data.clinic_id_value == int(value)
    assert data.doctor_id_value == int(value)


@pytest.mark.parametrize("value", ["02.01.2099", "20990102", "2099-1-02", "2099-02-30"])
def test_schedule_form_rejects_invalid_dates(value: str) -> None:
    errors = validate_errors(valid_form(date=value))
    assert ("date",) in errors
    assert "дат" in errors[("date",)][0].lower()


@pytest.mark.parametrize("field", ["start_time", "end_time", "break_start", "break_end"])
@pytest.mark.parametrize("value", ["09:10", "09:00:01", "09:00:00.000001"])
def test_schedule_form_rejects_time_outside_half_hour_grid(field: str, value: str) -> None:
    errors = validate_errors(valid_form(**{field: value}))
    assert (field,) in errors
    assert "время" in errors[(field,)][0].lower()


@pytest.mark.parametrize(
    "start_time,end_time",
    [("09:00", "09:00"), ("10:00", "09:30"), ("23:30", "00:00")],
)
def test_schedule_form_requires_positive_single_day_work_interval(
    start_time: str, end_time: str
) -> None:
    errors = validate_errors(valid_form(start_time=start_time, end_time=end_time))
    assert () in errors
    assert "рабоч" in errors[()][0].lower()


@pytest.mark.parametrize(
    "changes",
    [
        {"break_start": "10:00", "break_end": ""},
        {"break_start": "", "break_end": "10:30"},
        {"break_start": "10:30", "break_end": "10:00"},
        {"break_start": "10:00", "break_end": "10:00"},
        {"break_start": "08:30", "break_end": "10:00"},
        {"break_start": "11:30", "break_end": "12:30"},
    ],
)
def test_schedule_form_validates_break_pair_and_boundaries(changes: dict[str, str]) -> None:
    errors = validate_errors(valid_form(**changes))
    assert () in errors
    assert "перерыв" in errors[()][0].lower()


def test_schedule_form_accepts_absent_break() -> None:
    data = ScheduleForm.model_validate(valid_form(break_start="", break_end=""))
    assert data.break_start == ""
    assert data.break_end == ""


def moscow(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=LOCAL_TIMEZONE)


def generated_starts(**changes: Any) -> list[str]:
    data = ScheduleForm.model_validate(valid_form(**changes))
    return [
        start.strftime("%H:%M") for start, _ in data.generate_slots(now=moscow("2099-01-02T08:00"))
    ]


def test_generates_half_hour_slots_without_break() -> None:
    assert generated_starts(end_time="11:00", break_start="", break_end="") == [
        "09:00",
        "09:30",
        "10:00",
        "10:30",
    ]


def test_generates_slots_excluding_break() -> None:
    assert generated_starts() == ["09:00", "09:30", "10:30", "11:00", "11:30"]


@pytest.mark.parametrize(
    "break_start,break_end,expected",
    [
        ("09:00", "09:30", ["09:30", "10:00", "10:30", "11:00", "11:30"]),
        ("11:30", "12:00", ["09:00", "09:30", "10:00", "10:30", "11:00"]),
    ],
)
def test_allows_break_at_work_interval_edges(
    break_start: str, break_end: str, expected: list[str]
) -> None:
    assert generated_starts(break_start=break_start, break_end=break_end) == expected


def test_rejects_break_covering_whole_work_interval() -> None:
    errors = validate_errors(valid_form(break_start="09:00", break_end="12:00"))
    assert () in errors
    assert "не остаётся" in errors[()][0]


def test_generated_slots_have_thirty_minute_duration() -> None:
    data = ScheduleForm.model_validate(valid_form())
    slots = data.generate_slots(now=moscow("2099-01-02T08:00"))
    assert all(end - start == timedelta(minutes=30) for start, end in slots)
    assert all(start.tzinfo == LOCAL_TIMEZONE for start, _ in slots)


@pytest.mark.parametrize(
    "now",
    ["2099-01-03T00:00", "2099-01-02T09:00", "2099-01-02T09:45", "2099-01-02T10:00"],
)
def test_rejects_past_date_and_any_already_started_slot(now: str) -> None:
    data = ScheduleForm.model_validate(valid_form())
    before = len(data._local_slots())
    with pytest.raises(ConflictError, match="начал"):
        data.generate_slots(now=moscow(now))
    assert len(data._local_slots()) == before
