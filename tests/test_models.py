from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.models import ScheduleCreate, local_today


def schedule_payload(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "clinic_id": 1,
        "doctor_id": 1,
        "date": (local_today() + timedelta(days=2)).isoformat(),
        "start_time": "09:00",
        "end_time": "11:00",
    }
    data.update(overrides)
    return data


def test_valid_schedule() -> None:
    schedule = ScheduleCreate.model_validate(schedule_payload())
    assert schedule.clinic_id == 1
    assert schedule.day == local_today() + timedelta(days=2)
    assert schedule.start_time.isoformat() == "09:00:00"
    assert schedule.end_time.isoformat() == "11:00:00"
    assert schedule.break_start is None


def test_valid_schedule_with_break() -> None:
    schedule = ScheduleCreate.model_validate(
        schedule_payload(break_start="09:30", break_end="10:00")
    )
    assert schedule.break_start is not None
    assert schedule.break_end is not None
    assert schedule.break_start.isoformat() == "09:30:00"
    assert schedule.break_end.isoformat() == "10:00:00"


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"start_time": "09:10"}, "кратно 30 минутам"),
        ({"end_time": "11:15"}, "кратно 30 минутам"),
        ({"break_start": "09:40", "break_end": "10:00"}, "кратно 30 минутам"),
        ({"break_start": "09:00", "break_end": "10:10"}, "кратно 30 минутам"),
        ({"start_time": "09:00:15"}, "без секунд"),
        ({"start_time": "утро"}, "формате ЧЧ:ММ"),
        ({"start_time": "11:00"}, "позже начала"),
        ({"end_time": "08:00"}, "позже начала"),
        ({"break_start": "09:30"}, "и начало, и окончание"),
        ({"break_end": "10:00"}, "и начало, и окончание"),
        ({"break_start": "10:00", "break_end": "09:30"}, "позже начала"),
        ({"break_start": "12:00", "break_end": "12:30"}, "внутри рабочего интервала"),
        ({"break_start": "08:00", "break_end": "09:00"}, "внутри рабочего интервала"),
        ({"date": (local_today() - timedelta(days=1)).isoformat()}, "только на будущее"),
        (
            {"date": local_today().isoformat(), "start_time": "00:00", "end_time": "01:00"},
            "только на будущее",
        ),
        ({"start_time": ""}, "Input should be a valid time"),
        ({"clinic_id": 0}, "greater than or equal"),
        ({"extra": "ignored"}, "Extra inputs are not permitted"),
    ],
)
def test_invalid_schedule(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError) as error:
        ScheduleCreate.model_validate(schedule_payload(**overrides))
    assert message in str(error.value)
