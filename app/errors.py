"""Ошибки предметной области без зависимости от HTTP и HTML."""

from app.models import Slot


class NotFoundError(LookupError):
    """Запрошенная сущность не существует."""


class ConflictError(ValueError):
    """Операция противоречит текущему состоянию данных."""

    def __init__(self, message: str, *, conflicts: list[Slot] | None = None) -> None:
        super().__init__(message)
        self.conflicts = conflicts or []
