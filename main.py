"""AI-CRS 추천 API 서버.

docs/openapi-runpolio-ai.yaml 계약을 그대로 구현한다. 기존 로직(src/course/pipeline.py)은
건드리지 않고, 이 파일은 HTTP 요청을 파싱·검증해 그 함수를 호출하고 결과를
계약에 맞는 JSON으로 직렬화하는 얇은 껍데기다.

실행:
    uvicorn main:app --host 0.0.0.0 --port 8000

CORS 미들웨어는 넣지 않았다 — 이 서버는 브라우저가 아니라 백엔드(Spring)가
서버 대 서버로 호출하는 내부용이라 필요 없다. 브라우저에서 직접 호출해야 하면
그때 추가할 것.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

load_dotenv()

from src.course import durunubi_store
from src.course.embedding import MockEmbeddingClient
from src.course.pipeline import create_courses
from src.course.schemas import Course, CourseRequest, CourseResponse, Theme

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
logger = logging.getLogger("runpolio.main")

app = FastAPI(
    title="Runpolio AI API",
    version="2.1.0",
    description="docs/openapi-runpolio-ai.yaml 참고. 계약 상세는 그쪽이 정본이다.",
)

# 임베딩 모델 제공자가 확정되기 전까지의 임시 대용품.
# 교체 시 이 한 줄만 바꾸면 된다 (배선은 pipeline.create_courses 가 그대로 재사용).
_embedding_client = MockEmbeddingClient()

_ALLOWED_THEMES = [t.value for t in Theme]

# NFR 응답 목표(5초)의 3배 여유. 이 시간을 넘기면 뭔가 비정상이라는 뜻이므로
# 무한정 붙잡지 않고 500으로 끊는다.
_REQUEST_TIMEOUT_SEC = 15.0


class ApiError(Exception):
    """검증·설정 오류를 스펙의 Error 스키마로 변환하기 위한 예외."""

    def __init__(self, status_code: int, error: str, message: str, detail: Optional[dict] = None):
        self.status_code = status_code
        self.error = error
        self.message = message
        self.detail = detail

    def to_body(self) -> dict:
        body: dict[str, Any] = {"error": self.error, "message": self.message}
        body["detail"] = self.detail
        return body


@app.exception_handler(ApiError)
async def _api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.to_body())


@app.exception_handler(Exception)
async def _unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
    # 여기까지 오는 건 진짜 버그다. TMAP 실패는 pipeline 안에서 이미 흡수되어
    # fallback_reason 으로 빠지므로 이 핸들러를 타지 않는다.
    logger.exception("처리되지 않은 예외")
    return JSONResponse(
        status_code=500,
        content={
            "error": "INTERNAL_ERROR",
            "message": "서버 내부 오류가 발생했습니다.",
            "detail": {"reason": type(exc).__name__},
        },
    )


# ── 요청 검증 ────────────────────────────────────────────────────────────
def _is_number(v: Any) -> bool:
    # bool 은 파이썬에서 int 의 서브클래스라 isinstance(True, (int,float)) 가 True 다.
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _bad_request(message: str, detail: Optional[dict] = None) -> ApiError:
    return ApiError(400, "BAD_REQUEST", message, detail)


def _parse_request(body: Any) -> CourseRequest:
    """원시 JSON dict를 CourseRequest로 검증·변환한다.

    docs/openapi-runpolio-ai.yaml 의 BadRequest 예시(missing_center,
    missing_duration, bad_distance, bad_theme, bad_coordinate)와 문구를 맞췄다.
    """
    if not isinstance(body, dict):
        raise _bad_request("요청 본문은 JSON 객체여야 합니다.")

    # ── 필수 최상위 필드 존재 확인 ──
    missing_top = [k for k in ("center", "distance_km", "duration_min") if k not in body or body[k] is None]
    if missing_top:
        names = ", ".join(missing_top)
        raise _bad_request(f"{names} 은(는) 필수 항목입니다.", {"missing": missing_top})

    # ── center ──
    center = body["center"]
    if not isinstance(center, dict):
        raise _bad_request("center 는 객체({lat, lng})여야 합니다.", {"field": "center", "value": center})

    missing_center = [f"center.{k}" for k in ("lat", "lng") if k not in center or center[k] is None]
    if missing_center:
        raise _bad_request(
            f"{', '.join(missing_center)} 은(는) 필수 항목입니다.", {"missing": missing_center}
        )

    lat, lng = center["lat"], center["lng"]
    if not _is_number(lat):
        raise _bad_request("center.lat 은 숫자여야 합니다.", {"field": "center.lat", "value": lat})
    if not _is_number(lng):
        raise _bad_request("center.lng 은 숫자여야 합니다.", {"field": "center.lng", "value": lng})
    if not (-90 <= lat <= 90):
        raise _bad_request("center.lat 은 -90 이상 90 이하여야 합니다.", {"field": "center.lat", "value": lat})
    if not (-180 <= lng <= 180):
        raise _bad_request("center.lng 은 -180 이상 180 이하여야 합니다.", {"field": "center.lng", "value": lng})

    # ── distance_km ──
    distance_km = body["distance_km"]
    if not _is_number(distance_km):
        raise _bad_request("distance_km 은 숫자여야 합니다.", {"field": "distance_km", "value": distance_km})
    if not (0 < distance_km <= 100):
        raise _bad_request(
            "distance_km 은 0보다 크고 100 이하여야 합니다.", {"field": "distance_km", "value": distance_km}
        )

    # ── duration_min ──
    duration_min = body["duration_min"]
    if not _is_number(duration_min):
        raise _bad_request("duration_min 은 숫자여야 합니다.", {"field": "duration_min", "value": duration_min})
    if duration_min <= 0:
        raise _bad_request("duration_min 은 0보다 커야 합니다.", {"field": "duration_min", "value": duration_min})

    # ── theme (선택) ──
    theme_val = body.get("theme")
    theme: Optional[Theme] = None
    if theme_val is not None:
        if theme_val not in _ALLOWED_THEMES:
            raise _bad_request(
                "지원하지 않는 theme 입니다.",
                {"field": "theme", "value": theme_val, "allowed": _ALLOWED_THEMES},
            )
        theme = Theme(theme_val)

    return CourseRequest(
        center=(float(lat), float(lng)),
        distance_km=float(distance_km),
        duration_min=float(duration_min),
        theme=theme,
    )


# ── 응답 직렬화 (dataclass → 계약에 정확히 맞는 JSON) ───────────────────
def _course_to_dict(c: Course) -> dict:
    return {
        "course_id": c.course_id,
        "source": c.source.value,
        "source_course_id": c.source_course_id,
        "theme": list(c.theme),
        "distance": c.distance,
        "target_duration_min": c.target_duration_min,
        "distance_error_m": c.distance_error_m,
        "distance_accuracy": c.distance_accuracy.value if c.distance_accuracy else None,
        "estimated_duration_min": c.estimated_duration_min,
        "time_error_min": c.time_error_min,
        "path_data": [[lat, lng] for lat, lng in c.path_data],
        "center_location": list(c.center_location),
    }


def _response_to_dict(res: CourseResponse) -> dict:
    goal = None
    if res.goal_summary is not None:
        g = res.goal_summary
        goal = {
            "target_distance_km": g.target_distance_km,
            "target_duration_min": g.target_duration_min,
            "target_pace_sec_per_km": g.target_pace_sec_per_km,
            "target_pace_display": g.target_pace_display,
            "target_speed_kmh": g.target_speed_kmh,
            "target_intensity": g.target_intensity,
        }
    return {
        "goal_summary": goal,
        "courses": [_course_to_dict(c) for c in res.courses],
        "warnings": list(res.warnings),
        "fallback_reason": res.fallback_reason,
    }


# ── 엔드포인트 ──────────────────────────────────────────────────────────
@app.post("/api/v1/courses/recommend")
async def recommend_courses(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        raise _bad_request("요청 본문을 JSON으로 파싱할 수 없습니다.")

    req = _parse_request(body)

    # TMAP 키가 아예 없으면 매 후보마다 빈 appKey로 25번 헛호출하고 실패하는 대신
    # 즉시 설정 오류로 끊는다.
    if not os.environ.get("TMAP_APP_KEY"):
        raise ApiError(
            500, "INTERNAL_ERROR", "서버 설정 오류입니다.",
            {"reason": "TMAP_APP_KEY 환경변수 없음"},
        )

    try:
        result = await asyncio.wait_for(
            create_courses(req, _embedding_client), timeout=_REQUEST_TIMEOUT_SEC
        )
    except asyncio.TimeoutError:
        logger.error("추천 처리 시간 초과 (%.0fs)", _REQUEST_TIMEOUT_SEC)
        raise ApiError(
            500, "INTERNAL_ERROR", "요청 처리 시간이 초과되었습니다.",
            {"reason": "TIMEOUT", "timeout_sec": _REQUEST_TIMEOUT_SEC},
        )

    return JSONResponse(status_code=200, content=_response_to_dict(result))


@app.get("/api/v1/health")
async def health() -> JSONResponse:
    meta = durunubi_store.load_meta()
    course_count = meta.get("course_count", 0) or 0
    index_loaded = course_count > 0
    return JSONResponse(status_code=200, content={
        "status": "ok" if index_loaded else "degraded",
        "index_loaded": index_loaded,
        "data_source": meta.get("source"),
        "last_updated": meta.get("collected_at"),
        "course_count": course_count,
        "tmap_key_configured": bool(os.environ.get("TMAP_APP_KEY")),
    })
