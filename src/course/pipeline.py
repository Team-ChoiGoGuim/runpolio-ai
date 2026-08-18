"""AI-CRS 코스 생성 파이프라인 오케스트레이터.

확정 전략:
  ① 두루누비 코스 탐색은 무조건 실행
  ② TMAP 후보 생성도 무조건 실행 (두루누비와 병행)
  ③ 두루누비 코스가 있으면: 두루누비 1개 + TMAP 상위 2개 = 총 3개
     두루누비 코스가 없으면: TMAP 상위 3개 = 총 3개
  ④ 둘 다 실패 → 조건 완화 안내 (AI-CRS-009)

경로 생성의 목표는 거리(distance_km)다. duration_min 은 사용자가 설정한
목표 시간으로 결과에 그대로 실려 나가며, 생성 조건으로는 쓰이지 않는다.
"""
from __future__ import annotations

import logging
import uuid
import yaml
from pathlib import Path
from typing import Optional

from src.common.geo import total_distance_m
from src.course import durunubi, generator, goal, selector
from src.course.schemas import (
    Course, CourseRequest, CourseResponse, CourseSource, GoalSummary,
)

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "configs" / "course.yaml"

_FALLBACK_REASON = (
    "조건에 맞는 경로를 찾지 못했습니다. "
    "거리를 줄이거나 테마 조건을 완화해 다시 시도해 주세요."
)


def _load_config() -> dict:
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _make_course(
    source, theme, dist_m, path, req: CourseRequest,
    goal_summary: Optional[GoalSummary], source_course_id: Optional[str] = None,
) -> Optional[Course]:
    """코스 하나를 만든다. 거리 오차가 ±400m를 넘으면 반환 대상이 아니므로 None."""
    error_m, accuracy, estimated_min, time_error = (0, None, 0.0, 0.0)
    if goal_summary is not None:
        error_m, accuracy, estimated_min, time_error = goal.course_metrics(
            dist_m, goal_summary
        )
        if accuracy is None:
            return None

    return Course(
        course_id=f"crs_{uuid.uuid4().hex[:8]}",
        source=source,
        theme=theme,
        distance=round(dist_m / 1000.0, 2),
        target_duration_min=req.duration_min,
        path_data=path,
        center_location=req.center,
        source_course_id=source_course_id,
        distance_error_m=error_m,
        distance_accuracy=accuracy,
        estimated_duration_min=estimated_min,
        time_error_min=time_error,
    )


def _time_error_warnings(courses: list[Course], cfg: dict) -> list[str]:
    """시간 오차가 큰 코스를 알린다. 코스를 제거하지는 않는다."""
    limit = cfg.get("time_error_warn_min", 5.0)
    over = [c for c in courses if c.time_error_min > limit]
    if not over:
        return []
    worst = max(c.time_error_min for c in over)
    return [
        f"목표 시간과 {limit:g}분 넘게 차이나는 코스가 {len(over)}개 있습니다"
        f"(최대 {worst:g}분). 시간 오차는 목표 페이스로 실제 생성된 거리를 달렸을 때의 "
        "차이이며, 실제 완주 시간 예측이 아닙니다."
    ]


def _pace_warnings(req: CourseRequest, cfg: dict) -> list[str]:
    """목표 시간이 거리에 비해 비현실적이면 안내 문구를 만든다.

    요청을 거절하지는 않는다. 코스는 거리 기준으로 생성하고, 목표 시간이
    참고값이라는 점만 응답에 실어 보낸다.
    """
    pace = req.implied_pace_min_per_km
    if pace is None:
        return []
    sanity = cfg.get("pace_sanity", {})
    lo = sanity.get("min_min_per_km", 3.0)
    hi = sanity.get("max_min_per_km", 15.0)
    if lo <= pace <= hi:
        return []
    return [
        "목표 시간은 참고값이며 현재 경로 생성은 거리 기준입니다. "
        f"입력하신 {req.distance_km:g}km / {req.duration_min:g}분은 "
        f"약 {pace:.1f}분/km 페이스로, 일반적인 러닝 범위"
        f"({lo:g}~{hi:g}분/km)를 벗어납니다."
    ]


async def create_courses(
    req: CourseRequest, embedding_client: selector.EmbeddingClient
) -> CourseResponse:
    cfg = _load_config()
    total = cfg["generation"]["total_courses"]  # 3
    warnings = _pace_warnings(req, cfg)
    goal_summary = goal.compute_goal_summary(req.distance_km, req.duration_min, cfg)

    # ────────────────────────────────────────
    # ① 두루누비 탐색 (무조건 실행)
    # ────────────────────────────────────────
    durunubi_course = None
    nearby = durunubi.find_nearby_courses(
        center=req.center,
        radius_m=cfg["durunubi"]["search_radius_m"],
        theme=req.theme.value if req.theme else None,
        require_theme_match=cfg["durunubi"]["require_theme_match"],
    )
    for src_course in nearby:
        path = [tuple(p) for p in src_course["path"]]
        segment = durunubi.extract_segment(path, req.center, req.target_m, req.loop)
        if segment is not None:
            durunubi_course = _make_course(
                CourseSource.DURUNUBI, src_course.get("theme", []),
                total_distance_m(segment), segment, req, goal_summary,
                source_course_id=src_course.get("course_id"),
            )
            break  # 가장 가까운 매칭 1개만 사용

    # ────────────────────────────────────────
    # ② TMAP 후보 생성 (무조건 실행, 실패해도 예외를 내지 않음)
    # ────────────────────────────────────────
    gen_cfg = cfg["generation"]
    cache_cfg = cfg.get("candidate_cache", {})
    bands = gen_cfg["accept_bands_m"]
    batch = await generator.generate_candidates(
        center=req.center,
        target_m=req.target_m,
        n_candidates=gen_cfg["n_candidates"],
        refine_top_k=gen_cfg["refine_top_k"],
        max_refine_per_candidate=gen_cfg["max_refine_per_candidate"],
        call_budget=gen_cfg["tmap_call_budget"],
        max_concurrency=gen_cfg["max_concurrency"],
        accept_band_m=bands[0],
        target_accepted=gen_cfg["target_accepted"],
        loop=req.loop,
        timeout_sec=cfg["tmap"]["timeout_sec"],
        cache_ttl_sec=cache_cfg.get("ttl_sec", 300.0),
        cache_max_entries=cache_cfg.get("max_entries", 128),
    )

    # ────────────────────────────────────────
    # ③ 결과 조합: 두루누비 있으면 1+2, 없으면 0+3
    # ────────────────────────────────────────
    courses: list[Course] = []

    if durunubi_course:
        courses.append(durunubi_course)
        tmap_needed = total - 1  # 2개
    else:
        tmap_needed = total      # 3개

    if batch.candidates and tmap_needed > 0:
        # ±200m → ±300m → ±400m 순으로 넓히되, 이 단계는 새 TMAP 호출 없이
        # 이미 생성한 후보 중에서만 고른다.
        pool, band = generator.select_within_bands(
            batch.candidates, req.target_m, bands, tmap_needed,
        )
        batch.stats.selected_band_m = band
        logger.info(
            "코스 생성 요약 | %s",
            dict(batch.stats.as_log_dict(), durunubi_matched=bool(durunubi_course),
                 band_pool=len(pool), returned_tmap=min(len(pool), tmap_needed)),
        )
        top = selector.select_top_k(
            req, pool, embedding_client, top_k=tmap_needed,
        )
        for c in top:
            course = _make_course(
                CourseSource.AI_GENERATED,
                [req.theme.value] if req.theme else [],
                c.distance_m, c.path, req, goal_summary,
            )
            if course is not None:  # ±400m 초과는 반환하지 않는다
                courses.append(course)

    # 시간 오차는 코스를 걸러내는 조건이 아니라 안내 기준이다.
    warnings = warnings + _time_error_warnings(courses, cfg)

    # ④ 실패 처리 — 예외가 아니라 계약에 맞는 빈 응답으로 돌려준다.
    if not courses:
        return CourseResponse(
            courses=[], warnings=warnings, fallback_reason=_FALLBACK_REASON,
            goal_summary=goal_summary,
        )

    return CourseResponse(
        courses=courses, warnings=warnings, goal_summary=goal_summary,
    )
