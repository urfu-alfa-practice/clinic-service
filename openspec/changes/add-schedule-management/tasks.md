# Tasks

## 1. Модель и валидация

- [x] 1.1 Добавить `ScheduleCreate` с проверкой кратности 30 минутам, порядка времени, границ перерыва и наличия хотя бы одного слота; проверить тестами `test_invalid_schedule_interval`.
- [x] 1.2 Реализовать генерацию получасовых слотов без времени перерыва; проверить тестами `test_create_schedule_splits_into_half_hour_slots` и `test_break_does_not_create_slots`.

## 2. Хранение и миграция

- [x] 2.1 Добавить таблицу `schedule_intervals` и индекс, обновить `PRAGMA user_version` до 2; проверить тестом `test_initialize_upgrades_version_1_and_preserves_data`.
- [x] 2.2 Реализовать `ClinicRepository.create_schedule` в транзакции `BEGIN IMMEDIATE` с проверкой поликлиники, врача и пересечений; проверить тестами `test_overlapping_interval_is_rejected`, `test_interval_cannot_cover_another_intervals_break` и `test_adjacent_interval_is_allowed`.
- [x] 2.3 Проверить, что существующие слоты и записи не меняются; тест `test_schedule_creation_preserves_existing_slots_and_appointments`.
- [x] 2.4 Проверить отказ от открытия базы более новой версии; тест `test_initialize_rejects_newer_database`.

## 3. HTML-интерфейс

- [x] 3.1 Добавить страницу `/schedule` с формой поликлиники, врача, даты, интервала и перерыва; проверить тестом `test_schedule_manage_page_and_creation`.
- [x] 3.2 Показывать понятные ошибки и сохранять введённые значения; проверить тестом `test_schedule_manage_error_explains_and_preserves_values`.
- [x] 3.3 Показывать конфликт пересечения и врача из другой поликлиники; проверить тестами `test_schedule_manage_overlap_error` и `test_schedule_manage_doctor_from_other_clinic`.
- [x] 3.4 Добавить ссылки на управление из расписания и главного меню, а также таблицу слотов на выбранную дату; проверить вручную и тестом `test_schedule_manage_page_and_creation`.

## 4. API

- [x] 4.1 Добавить `POST /api/schedules` с той же моделью валидации и репозиторием; проверить тестом `test_api_create_schedule`.
- [x] 4.2 Проверить ошибки API 422/409/404; тест `test_api_schedule_errors`.

## 5. Документация и проверки

- [x] 5.1 Обновить `README.md`: правила расписания, API, версия схемы и миграция.
- [x] 5.2 Выполнить `.venv\Scripts\python.exe -m pytest`, `python -m ruff check .` и `python -m ruff format --check .`.
- [x] 5.3 Выполнить `npx @fission-ai/openspec@latest validate add-schedule-management --strict` и убедиться, что спецификация корректна.

## Workflow follow-up

- После проверки и ревью архивировать изменение командой `openspec archive add-schedule-management`.
