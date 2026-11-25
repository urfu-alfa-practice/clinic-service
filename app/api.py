"""JSON API использует те же операции и правила, что и браузерный интерфейс."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query, Response

from app.models import (
    Appointment,
    AppointmentCreate,
    Clinic,
    Doctor,
    EntityId,
    Patient,
    PatientCard,
    PatientCreate,
    ScheduleCreate,
    Slot,
    VisitComplete,
)
from app.repository import ClinicRepository


def create_api_router(repository: ClinicRepository) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/clinics", tags=["Расписание"], summary="Поликлиники")
    def list_clinics() -> list[Clinic]:
        return repository.list_clinics()

    @router.get("/doctors", tags=["Расписание"], summary="Врачи поликлиники")
    def list_doctors(clinic_id: EntityId) -> list[Doctor]:
        return repository.list_doctors(clinic_id)

    @router.get(
        "/slots", tags=["Расписание"], summary="Расписание на дату (по московскому времени)"
    )
    def list_slots(clinic_id: EntityId, day: Annotated[date, Query(alias="date")]) -> list[Slot]:
        return repository.list_slots(clinic_id, day)

    @router.post("/slots", status_code=201, tags=["Расписание"], summary="Создать расписание врача")
    def create_schedule(data: ScheduleCreate, response: Response) -> list[Slot]:
        slots = repository.create_schedule(data)
        response.headers["Location"] = (
            f"/api/slots?clinic_id={data.clinic_id}&date={data.date.isoformat()}"
        )
        return slots

    @router.get("/patients", tags=["Пациенты"], summary="Пациенты")
    def list_patients() -> list[Patient]:
        return repository.list_patients()

    @router.post("/patients", status_code=201, tags=["Пациенты"], summary="Создать пациента")
    def create_patient(data: PatientCreate, response: Response) -> Patient:
        patient = repository.create_patient(data)
        response.headers["Location"] = f"/api/patients/{patient.id}"
        return patient

    @router.get("/patients/{patient_id}", tags=["Пациенты"], summary="Медицинская карта пациента")
    def patient_card(patient_id: EntityId) -> PatientCard:
        return PatientCard(
            patient=repository.get_patient(patient_id),
            appointments=repository.list_appointments(patient_id=patient_id),
            lab_results=repository.list_lab_results(patient_id),
        )

    @router.get("/appointments", tags=["Приёмы"], summary="Все записи на приём")
    def list_appointments() -> list[Appointment]:
        return repository.list_appointments()

    @router.post("/appointments", status_code=201, tags=["Приёмы"], summary="Записать пациента")
    def book(data: AppointmentCreate, response: Response) -> Appointment:
        appointment = repository.book(data)
        response.headers["Location"] = f"/api/appointments/{appointment.id}"
        return appointment

    @router.get("/appointments/{appointment_id}", tags=["Приёмы"], summary="Карточка приёма")
    def get_appointment(appointment_id: EntityId) -> Appointment:
        return repository.get_appointment(appointment_id)

    @router.post(
        "/appointments/{appointment_id}/cancel", tags=["Приёмы"], summary="Отменить запись"
    )
    def cancel(appointment_id: EntityId) -> Appointment:
        return repository.cancel(appointment_id)

    @router.post(
        "/appointments/{appointment_id}/complete",
        tags=["Приёмы"],
        summary="Завершить приём с заметкой",
    )
    def complete(appointment_id: EntityId, data: VisitComplete) -> Appointment:
        return repository.complete(appointment_id, data)

    return router
