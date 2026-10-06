import logging
from enum import StrEnum

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class ErrorCode(StrEnum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    CONFLICT = "CONFLICT"
    RATE_LIMITED = "RATE_LIMITED"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


_STATUS = {
    ErrorCode.VALIDATION_ERROR: 400,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.RESOURCE_NOT_FOUND: 404,
    ErrorCode.CONFLICT: 409,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.SOURCE_UNAVAILABLE: 503,
    ErrorCode.INTERNAL_ERROR: 500,
}
_CODE_BY_STATUS = {status: code for code, status in _STATUS.items()}


class AppError(Exception):
    def __init__(self, code: ErrorCode, message: str, field: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field

    @property
    def status_code(self) -> int:
        return _STATUS[self.code]


def validation_error(field: str, message: str) -> AppError:
    return AppError(ErrorCode.VALIDATION_ERROR, message, field)


def not_found(message: str) -> AppError:
    return AppError(ErrorCode.RESOURCE_NOT_FOUND, message)


def _body(code: ErrorCode, message: str, field: str | None = None) -> dict:
    return {"error": {"code": code.value, "message": message, "field": field}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(_body(exc.code, exc.message, exc.field), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0]
        field = str(first["loc"][-1]) if first["loc"] else None
        return JSONResponse(
            _body(ErrorCode.VALIDATION_ERROR, f"요청 값이 올바르지 않습니다: {field}", field),
            status_code=400,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _CODE_BY_STATUS.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
        message = "요청한 경로를 찾을 수 없습니다" if exc.status_code == 404 else str(exc.detail)
        return JSONResponse(_body(code, message), status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("처리되지 않은 오류", exc_info=exc)
        return JSONResponse(_body(ErrorCode.INTERNAL_ERROR, "서버 오류가 발생했습니다"), status_code=500)
