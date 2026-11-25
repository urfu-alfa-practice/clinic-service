"""Серверные HTML-страницы. Для работы интерфейса JavaScript не нужен."""

from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.errors import ConflictError, NotFoundError
from app.models import (
    LOCAL_TIMEZONE,
    NAME_MAX_LENGTH,
    NOTE_MAX_LENGTH,
    AppointmentCreate,
    AppointmentStatus,
    EntityId,
    PatientCreate,
    ScheduleCreate,
    VisitComplete,
    local_today,
)
from app.repository import ClinicRepository


def field_errors(error: ValidationError) -> dict[str, str]:
    messages: dict[str, str] = {}
    for item in error.errors():
        location = item["loc"]
        field = str(location[0]) if location else "__form__"
        if item["type"] == "string_too_long":
            message = f"Сократите текст до {item['ctx']['max_length']} символов."
        elif item["type"] == "value_error":
            message = str(item["ctx"]["error"])
        elif item["type"] in {"time_parsing", "time_from_datetime_parsing"}:
            message = "Укажите время в формате ЧЧ:ММ."
        elif item["type"] in {"date_parsing", "date_from_datetime_parsing"} or field == "date":
            message = "Укажите существующую дату."
        elif field == "birth_date":
            message = "Укажите существующую дату рождения."
        elif field == "patient_id":
            message = "Выберите пациента из списка."
        elif item["type"] in {"int_parsing", "int_type"}:
            message = "Выберите значение из списка."
        elif item["type"] == "missing":
            message = "Заполните поле."
        else:
            message = "Заполните поле — пробелы не считаются текстом."
        messages[field] = message
    return messages


def create_web_router(repository: ClinicRepository, templates: Jinja2Templates) -> APIRouter:
    router = APIRouter(include_in_schema=False)
    templates.env.filters["local_datetime"] = lambda value: value.astimezone(
        LOCAL_TIMEZONE
    ).strftime("%d.%m.%Y, %H:%M")
    templates.env.filters["local_time"] = lambda value: value.astimezone(LOCAL_TIMEZONE).strftime(
        "%H:%M"
    )
    templates.env.filters["local_day"] = lambda value: (
        value.astimezone(LOCAL_TIMEZONE).date().isoformat()
    )
    templates.env.globals.update(
        name_max_length=NAME_MAX_LENGTH,
        note_max_length=NOTE_MAX_LENGTH,
        appointment_status=AppointmentStatus,
    )

    def render(
        request: Request, name: str, *, status_code: int = 200, **context: object
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request, name=name, context=context, status_code=status_code
        )

    @router.get("/", response_class=HTMLResponse)
    def schedule(
        request: Request,
        clinic_id: EntityId | None = None,
        day: Annotated[date | None, Query(alias="date")] = None,
    ) -> HTMLResponse:
        clinics = repository.list_clinics()
        selected_day = day or local_today() + timedelta(days=1)
        selected_clinic = None
        slots = []
        if clinic_id is not None:
            selected_clinic = repository.get_clinic(clinic_id)
        elif clinics:
            selected_clinic = clinics[0]
        if selected_clinic:
            slots = repository.list_slots(selected_clinic.id, selected_day)
        notice = None
        if request.query_params.get("notice") == "schedule_created":
            count = request.query_params.get("count", "")
            notice = f"Добавлено слотов: {count}." if count.isdigit() else "Расписание создано."
        return render(
            request,
            "schedule.html",
            nav="schedule",
            clinics=clinics,
            clinic=selected_clinic,
            day=selected_day,
            slots=slots,
            notice=notice,
        )

    def schedule_form_page(
        request: Request,
        *,
        values: dict[str, str],
        errors: dict[str, str],
        status_code: int = 200,
    ) -> HTMLResponse:
        clinics = repository.list_clinics()
        clinic = None
        clinic_value = values.get("clinic_id", "")
        if clinic_value:
            try:
                clinic = repository.get_clinic(int(clinic_value))
            except (ValueError, NotFoundError):
                clinic = None
        if clinic is None and clinics:
            clinic = clinics[0]
            values = {**values, "clinic_id": str(clinic.id)}
            if clinic_value:
                errors = {"__form__": "Выберите поликлинику из списка.", **errors}
        return render(
            request,
            "schedule_form.html",
            status_code=status_code,
            nav="schedule",
            clinics=clinics,
            clinic=clinic,
            doctors=repository.list_doctors(clinic.id) if clinic else [],
            values=values,
            errors=errors,
            today=local_today(),
        )

    @router.get("/schedule/new", response_class=HTMLResponse)
    def new_schedule(
        request: Request,
        clinic_id: EntityId | None = None,
        day: Annotated[date | None, Query(alias="date")] = None,
    ) -> HTMLResponse:
        values = {"date": (day or local_today()).isoformat()}
        if clinic_id is not None:
            repository.get_clinic(clinic_id)
            values["clinic_id"] = str(clinic_id)
        return schedule_form_page(request, values=values, errors={})

    @router.post("/schedule", response_model=None)
    def create_schedule(
        request: Request,
        clinic_id: Annotated[str, Form()] = "",
        doctor_id: Annotated[str, Form()] = "",
        date: Annotated[str, Form()] = "",
        starts_at: Annotated[str, Form()] = "",
        ends_at: Annotated[str, Form()] = "",
        break_starts_at: Annotated[str, Form()] = "",
        break_ends_at: Annotated[str, Form()] = "",
    ) -> HTMLResponse | RedirectResponse:
        values = {
            "clinic_id": clinic_id,
            "doctor_id": doctor_id,
            "date": date,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "break_starts_at": break_starts_at,
            "break_ends_at": break_ends_at,
        }
        try:
            data = ScheduleCreate.model_validate(values)
        except ValidationError as error:
            return schedule_form_page(
                request, values=values, errors=field_errors(error), status_code=422
            )
        try:
            created = repository.create_schedule(data)
        except ConflictError as error:
            return schedule_form_page(
                request, values=values, errors={"__form__": str(error)}, status_code=409
            )
        except NotFoundError as error:
            return schedule_form_page(
                request, values=values, errors={"__form__": str(error)}, status_code=404
            )
        return RedirectResponse(
            f"/?clinic_id={data.clinic_id}&date={data.date.isoformat()}"
            f"&notice=schedule_created&count={len(created)}",
            status_code=303,
        )

    @router.get("/patients", response_class=HTMLResponse)
    def patients(request: Request) -> HTMLResponse:
        return render(request, "patients.html", nav="patients", patients=repository.list_patients())

    @router.get("/patients/new", response_class=HTMLResponse)
    def new_patient(request: Request) -> HTMLResponse:
        return render(
            request, "patient_form.html", nav="patients", values={}, errors={}, today=local_today()
        )

    @router.post("/patients", response_model=None)
    def create_patient(
        request: Request,
        full_name: Annotated[str, Form()] = "",
        birth_date: Annotated[str, Form()] = "",
        phone: Annotated[str, Form()] = "",
    ) -> HTMLResponse | RedirectResponse:
        values = {"full_name": full_name, "birth_date": birth_date, "phone": phone}
        try:
            data = PatientCreate.model_validate(values)
        except ValidationError as error:
            return render(
                request,
                "patient_form.html",
                status_code=422,
                nav="patients",
                values=values,
                errors=field_errors(error),
                today=local_today(),
            )
        patient = repository.create_patient(data)
        return RedirectResponse(f"/patients/{patient.id}?notice=created", status_code=303)

    @router.get("/patients/{patient_id}", response_class=HTMLResponse)
    def patient_detail(request: Request, patient_id: EntityId) -> HTMLResponse:
        patient = repository.get_patient(patient_id)
        return render(
            request,
            "patient_detail.html",
            nav="patients",
            patient=patient,
            appointments=repository.list_appointments(patient_id=patient_id),
            lab_results=repository.list_lab_results(patient_id),
            notice="Пациент добавлен." if request.query_params.get("notice") == "created" else None,
        )

    @router.get("/appointments", response_class=HTMLResponse)
    def appointments(request: Request) -> HTMLResponse:
        return render(
            request,
            "appointments.html",
            nav="appointments",
            appointments=repository.list_appointments(),
        )

    @router.get("/appointments/new", response_class=HTMLResponse)
    def new_appointment(request: Request, slot_id: EntityId) -> HTMLResponse:
        slot = repository.get_slot(slot_id)
        return render(
            request,
            "booking.html",
            nav="schedule",
            slot=slot,
            patients=repository.list_patients(),
            values={},
            error=None,
        )

    @router.post("/appointments", response_model=None)
    def book(
        request: Request,
        slot_id: Annotated[EntityId, Form()],
        patient_id: Annotated[str, Form()] = "",
    ) -> HTMLResponse | RedirectResponse:
        slot = repository.get_slot(slot_id)
        error_message = None
        status_code = 422
        try:
            data = AppointmentCreate(slot_id=slot_id, patient_id=patient_id)
            appointment = repository.book(data)
        except ValidationError as error:
            error_message = field_errors(error).get("patient_id", "Проверьте данные записи.")
        except (ConflictError, NotFoundError) as error:
            error_message = str(error)
            status_code = 409 if isinstance(error, ConflictError) else 404
        else:
            return RedirectResponse(
                f"/appointments/{appointment.id}?notice=booked", status_code=303
            )
        return render(
            request,
            "booking.html",
            status_code=status_code,
            nav="schedule",
            slot=repository.get_slot(slot.id),
            patients=repository.list_patients(),
            values={"patient_id": patient_id},
            error=error_message,
        )

    def appointment_page(
        request: Request,
        appointment_id: int,
        *,
        status_code: int = 200,
        error: str | None = None,
        note: str = "",
    ) -> HTMLResponse:
        appointment = repository.get_appointment(appointment_id)
        notices = {
            "booked": "Пациент записан на приём.",
            "cancelled": "Запись отменена. Время снова доступно для записи, если приём ещё не начался.",
            "completed": "Приём завершён. Заметка сохранена в медицинской карте.",
        }
        return render(
            request,
            "appointment_detail.html",
            status_code=status_code,
            nav="appointments",
            appointment=appointment,
            can_complete=appointment.starts_at <= datetime.now(UTC),
            notice=notices.get(request.query_params.get("notice", "")) if error is None else None,
            error=error,
            note=note,
        )

    @router.get("/appointments/{appointment_id}", response_class=HTMLResponse)
    def appointment_detail(request: Request, appointment_id: EntityId) -> HTMLResponse:
        return appointment_page(request, appointment_id)

    @router.post("/appointments/{appointment_id}/cancel", response_model=None)
    def cancel(request: Request, appointment_id: EntityId) -> HTMLResponse | RedirectResponse:
        try:
            repository.cancel(appointment_id)
        except ConflictError as error:
            return appointment_page(request, appointment_id, status_code=409, error=str(error))
        return RedirectResponse(f"/appointments/{appointment_id}?notice=cancelled", status_code=303)

    @router.post("/appointments/{appointment_id}/complete", response_model=None)
    def complete(
        request: Request, appointment_id: EntityId, note: Annotated[str, Form()] = ""
    ) -> HTMLResponse | RedirectResponse:
        repository.get_appointment(appointment_id)
        try:
            data = VisitComplete(note=note)
            repository.complete(appointment_id, data)
        except ValidationError as error:
            return appointment_page(
                request,
                appointment_id,
                status_code=422,
                error=field_errors(error)["note"],
                note=note,
            )
        except ConflictError as error:
            return appointment_page(
                request, appointment_id, status_code=409, error=str(error), note=note
            )
        return RedirectResponse(f"/appointments/{appointment_id}?notice=completed", status_code=303)

    return router
