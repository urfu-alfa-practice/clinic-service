"""Сборка приложения. Импорт не создаёт файлов и не запускает сервер."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api import create_api_router
from app.errors import ConflictError, NotFoundError
from app.repository import ClinicRepository
from app.web import create_web_router

APP_DIR = Path(__file__).resolve().parent
DEFAULT_DATABASE_PATH = APP_DIR.parent / "data" / "clinic.sqlite3"


def create_app(database_path: Path | None = None, *, demo: bool = False) -> FastAPI:
    repository = ClinicRepository(database_path or DEFAULT_DATABASE_PATH)
    templates = Jinja2Templates(directory=APP_DIR / "templates")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        repository.initialize()
        if demo:
            repository.seed_demo()
        yield

    app = FastAPI(
        title="Поликлиника · Учебная информационная система",
        description="Расписание, запись на приём и медицинские карты. Все данные вымышлены.",
        version="1.0.0",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def check_origin(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        origin = request.headers.get("origin")
        if (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and origin is not None
            and origin != str(request.base_url).rstrip("/")
        ):
            return JSONResponse({"detail": "Запрос с другого сайта запрещён."}, status_code=403)
        return await call_next(request)

    def error_response(request: Request, status_code: int, message: str) -> Response:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": message}, status_code=status_code)
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={"message": message, "status_code": status_code, "nav": ""},
            status_code=status_code,
        )

    @app.exception_handler(NotFoundError)
    async def not_found(request: Request, error: NotFoundError) -> Response:
        return error_response(request, 404, str(error))

    @app.exception_handler(ConflictError)
    async def conflict(request: Request, error: ConflictError) -> Response:
        if request.url.path.startswith("/api/"):
            payload: dict[str, object] = {"detail": str(error)}
            if error.conflicts:
                payload["conflicts"] = [slot.model_dump(mode="json") for slot in error.conflicts]
            return JSONResponse(payload, status_code=409)
        return error_response(request, 409, str(error))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> Response:
        if request.url.path.startswith("/api/"):
            return await request_validation_exception_handler(request, error)
        return error_response(request, 422, "Проверьте адрес, дату и номера записей в запросе.")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> Response:
        if error.status_code == 404:
            return error_response(request, 404, "Страница не найдена.")
        return JSONResponse(
            {"detail": error.detail}, status_code=error.status_code, headers=error.headers
        )

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
    app.include_router(create_api_router(repository))
    app.include_router(create_web_router(repository, templates))
    return app
