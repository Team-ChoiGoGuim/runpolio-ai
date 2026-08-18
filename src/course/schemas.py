"""AI-CRS 입출력 스키마."""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

Point = tuple[float, float]


class CourseSource(str, Enum):
    AI_GENERATED = "AI_GENERATED"
    DURUNUBI = "DURUNUBI"


class Theme(str, Enum):
    BEACH = "해변"
    RIVER = "강변"
    PARK = "공원"
    LAKE = "호수"
    CITY = "도심"


class DistanceAccuracy(str, Enum):
    """생성된 거리가 목표에서 얼마나 벗어났는지 표시하는 등급."""

    EXACT = "EXACT"                  # ±200m 이내
    RELAXED_300M = "RELAXED_300M"    # ±201~300m
    RELAXED_400M = "RELAXED_400M"    # ±301~400m


@dataclass
class GoalSummary:
    """사용자가 입력한 목표(거리·시간)를 환산한 요약.

    전부 입력값에서 파생된 값이며 경로 생성 조건이 아니다.
    """

    target_distance_km: float
    target_duration_min: float
    target_pace_sec_per_km: int
    target_pace_display: str
    target_speed_kmh: float
    target_intensity: str
    """목표 운동 강도(상/중/하). **코스 난이도가 아니라** 사용자가 설정한 목표의 강도다."""


@dataclass
class CourseRequest:
    """추천 요청.

    distance_km 과 duration_min 은 모두 필수다. 다만 경로 생성의 목표는
    distance_km 이며, duration_min 은 사용자가 설정한 목표 시간으로
    결과에 그대로 반환된다(생성 조건으로 쓰이지 않는다).
    """

    center: Point
    distance_km: float
    duration_min: float
    theme: Optional[Theme] = None
    loop: bool = True

    @property
    def target_m(self) -> float:
        return self.distance_km * 1000.0

    @property
    def implied_pace_min_per_km(self) -> Optional[float]:
        """목표 시간과 거리로부터 역산한 페이스(분/km)."""
        if self.distance_km <= 0:
            return None
        return self.duration_min / self.distance_km


@dataclass
class Course:
    course_id: str
    """요청마다 새로 만드는 임시 식별자. 영구 저장·재조회 키로 쓸 수 없다."""

    source: CourseSource
    theme: list[str]
    distance: float
    target_duration_min: float
    """사용자가 입력한 목표 시간(분). 시스템이 계산한 예상 시간이 아니다."""

    path_data: list[Point]
    center_location: Point
    source_course_id: Optional[str] = None
    """원천 데이터의 영속 ID. DURUNUBI 는 두루누비 crsIdx, AI_GENERATED 는 None."""

    distance_error_m: int = 0
    """|실제 거리 − 목표 거리| (m)."""

    distance_accuracy: Optional[DistanceAccuracy] = None
    """거리 오차 등급. ±400m 를 넘는 코스는 애초에 반환되지 않는다."""

    estimated_duration_min: float = 0.0
    """목표 페이스로 이 코스 거리를 달렸을 때의 시간(분). 실제 완주 시간 예측이 아니다."""

    time_error_min: float = 0.0
    """|예상 시간 − 목표 시간| (분). 표시·경고용이며 후보 생성 조건이 아니다."""


@dataclass
class CourseResponse:
    courses: list[Course] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fallback_reason: Optional[str] = None
    goal_summary: Optional[GoalSummary] = None
