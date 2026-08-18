"""사용자 목표(거리·시간) 요약과 코스별 오차 계산.

여기서 나오는 값은 전부 **사용자가 입력한 목표를 그대로 환산한 것**이다.
경로 생성 조건이 아니고, 실제 완주 시간 예측도 아니다.

  target_pace_sec_per_km = duration_min × 60 / distance_km
  target_speed_kmh       = distance_km / (duration_min / 60)
  estimated_duration_min = actual_distance_km × target_pace_sec_per_km / 60
  time_error_min         = |estimated_duration_min − target_duration_min|

estimated_duration_min 은 "목표 페이스로 실제 생성된 거리를 달렸을 때 걸리는 시간"이다.
경사도·신호등·개인 기록 같은 독립 데이터가 없으므로 실제 소요 시간과는 다르다.
"""
from __future__ import annotations

from typing import Optional

from src.course.schemas import DistanceAccuracy, GoalSummary

# 거리 정확도 구간 경계(m). generation.accept_bands_m 와 같은 정책을 표시용으로 옮긴 것.
_ACCURACY_BANDS: list[tuple[float, DistanceAccuracy]] = [
    (200.0, DistanceAccuracy.EXACT),
    (300.0, DistanceAccuracy.RELAXED_300M),
    (400.0, DistanceAccuracy.RELAXED_400M),
]


def format_pace(pace_sec_per_km: float) -> str:
    """초/km → 'M:SS/km'."""
    total = int(round(pace_sec_per_km))
    return f"{total // 60}:{total % 60:02d}/km"


def classify_intensity(pace_sec_per_km: float, cfg: dict) -> str:
    """목표 운동 강도. 코스 난이도가 아니라 사용자가 설정한 목표의 강도다."""
    thresholds = cfg.get("target_intensity", {})
    high = thresholds.get("high_max_pace_sec_per_km", 330)
    medium = thresholds.get("medium_max_pace_sec_per_km", 420)
    if pace_sec_per_km <= high:
        return "상"
    if pace_sec_per_km <= medium:
        return "중"
    return "하"


def classify_accuracy(distance_error_m: float) -> Optional[DistanceAccuracy]:
    """거리 오차 → 정확도 표시. ±400m를 넘으면 None(반환 대상이 아님)."""
    for limit, label in _ACCURACY_BANDS:
        if distance_error_m <= limit:
            return label
    return None


def compute_goal_summary(
    distance_km: float, duration_min: float, cfg: dict,
) -> Optional[GoalSummary]:
    """목표 요약을 계산한다. 거리·시간이 양수가 아니면 None."""
    if distance_km <= 0 or duration_min <= 0:
        return None
    pace_sec = duration_min * 60.0 / distance_km
    speed_kmh = distance_km / (duration_min / 60.0)
    return GoalSummary(
        target_distance_km=round(distance_km, 2),
        target_duration_min=round(duration_min, 2),
        target_pace_sec_per_km=int(round(pace_sec)),
        target_pace_display=format_pace(pace_sec),
        target_speed_kmh=round(speed_kmh, 2),
        target_intensity=classify_intensity(pace_sec, cfg),
    )


def course_metrics(
    actual_distance_m: float, goal: GoalSummary,
) -> tuple[int, Optional[DistanceAccuracy], float, float]:
    """코스 하나의 거리 오차·정확도·예상 시간·시간 오차.

    반환: (distance_error_m, distance_accuracy, estimated_duration_min, time_error_min)
    """
    target_m = goal.target_distance_km * 1000.0
    error_m = abs(actual_distance_m - target_m)

    actual_km = actual_distance_m / 1000.0
    estimated_min = actual_km * goal.target_pace_sec_per_km / 60.0
    time_error = abs(estimated_min - goal.target_duration_min)

    return (
        int(round(error_m)),
        classify_accuracy(error_m),
        round(estimated_min, 2),
        round(time_error, 2),
    )
