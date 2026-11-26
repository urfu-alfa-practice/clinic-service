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
    ScheduleForm,
    VisitComplete,
    local_today,
    parse_entity_id,
)
from app.repository import ClinicRepository


def field_errors(error: ValidationError) -> dict[str, str]:
    messages = {}
    for item in error.errors():
        field = str(item["loc"][0]) if item["loc"] else ""
        if item["type"] == "string_too_long":
            message = f"Сократите текст до {item['ctx']['max_length']} символов."
        elif item["type"] == "value_error":
            message = str(item["ctx"]["error"])
        elif field == "birth_date":
            message = "Укажите существующую дату рождения."
        elif field == "patient_id":
            message = "Выберите пациента из списка."
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

    def schedule_management_page(
        request: Request,
        values: dict[str, str],
        *,
        status_code: int = 200,
        errors: dict[str, str] | None = None,
        error: str | None = None,
    ) -> HTMLResponse:
        clinics = repository.list_clinics()
        selected_clinic = None
        raw_clinic_id = values.get("clinic_id", "")
        selected_clinic_id = parse_entity_id(raw_clinic_id)
        if selected_clinic_id is not None:
            selected_clinic = next(
                (clinic for clinic in clinics if clinic.id == selected_clinic_id), None
            )
        doctors = repository.list_doctors(selected_clinic.id) if selected_clinic else []
        return render(
            request,
            "schedule_manage.html",
            status_code=status_code,
            nav="schedule_management",
            clinics=clinics,
            doctors=doctors,
            values=values,
            errors=errors or {},
            error=error,
            today=local_today(),
        )

    @router.get("/schedule/manage", response_class=HTMLResponse)
    def schedule_management(
        request: Request,
        clinic_id: str | None = None,
        doctor_id: str | None = None,
        date: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        break_start: str | None = None,
        break_end: str | None = None,
    ) -> HTMLResponse:
        clinics = repository.list_clinics()
        selected_id = clinic_id if clinic_id is not None else (clinics[0].id if clinics else "")
        values = {
            "clinic_id": str(selected_id),
            "doctor_id": "",
            "date": (local_today() + timedelta(days=1)).isoformat(),
            "start_time": "",
            "end_time": "",
            "break_start": "",
            "break_end": "",
        }
        provided = {
            "doctor_id": doctor_id,
            "date": date,
            "start_time": start_time,
            "end_time": end_time,
            "break_start": break_start,
            "break_end": break_end,
        }
        for field, value in provided.items():
            if value is not None:
                values[field] = value
        return schedule_management_page(request, values)

    @router.post("/schedule/manage", response_model=None)
    def create_schedule(
        request: Request,
        clinic_id: Annotated[str, Form()] = "",
        doctor_id: Annotated[str, Form()] = "",
        date: Annotated[str, Form()] = "",
        start_time: Annotated[str, Form()] = "",
        end_time: Annotated[str, Form()] = "",
        break_start: Annotated[str, Form()] = "",
        break_end: Annotated[str, Form()] = "",
    ) -> HTMLResponse | RedirectResponse:
        values = {
            "clinic_id": clinic_id,
            "doctor_id": doctor_id,
            "date": date,
            "start_time": start_time,
            "end_time": end_time,
            "break_start": break_start,
            "break_end": break_end,
        }
        try:
            data = ScheduleForm.model_validate(values)
        except ValidationError as validation_error:
            errors = field_errors(validation_error)
            general_error = errors.pop("", None)
            return schedule_management_page(
                request,
                values,
                status_code=422,
                errors=errors,
                error=general_error,
            )

        try:
            repository.get_clinic(data.clinic_id_value)
        except NotFoundError as error:
            values["clinic_id"] = ""
            values["doctor_id"] = ""
            return schedule_management_page(
                request,
                values,
                status_code=404,
                errors={"clinic_id": str(error)},
            )
        try:
            doctor = repository.get_doctor(data.doctor_id_value)
        except NotFoundError as error:
            values["doctor_id"] = ""
            return schedule_management_page(
                request,
                values,
                status_code=404,
                errors={"doctor_id": str(error)},
            )
        if doctor.clinic_id != data.clinic_id_value:
            values["doctor_id"] = ""
            return schedule_management_page(
                request,
                values,
                status_code=422,
                errors={"doctor_id": "Врач не закреплён за выбранной поликлиникой."},
            )
        try:
            intervals = data.generate_slots(now=datetime.now(UTC))
            created = repository.create_schedule(
                data.clinic_id_value, data.doctor_id_value, intervals
            )
        except ConflictError as error:
            return schedule_management_page(request, values, status_code=409, error=str(error))
        except NotFoundError as error:
            return schedule_management_page(request, values, status_code=404, error=str(error))
        return RedirectResponse(
            f"/?clinic_id={data.clinic_id_value}&date={data.schedule_date}&added={created}",
            status_code=303,
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
        added = request.query_params.get("added", "")
        added_count = parse_entity_id(added)
        notice = f"Расписание добавлено: {added_count} слотов" if added_count is not None else None
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
