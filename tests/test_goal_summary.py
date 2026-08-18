"""목표 요약(goal_summary)과 코스별 거리·시간 오차 테스트."""
import asyncio

import pytest

from src.course import goal, pipeline
from src.course.generator import CandidateBatch, RawCandidate
from src.course.schemas import CourseRequest, DistanceAccuracy, GoalSummary

CFG = {"target_intensity": {"high_max_pace_sec_per_km": 330,
                            "medium_max_pace_sec_per_km": 420}}
SEOUL = (37.5663, 126.9779)


class StubEmbedding:
    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]


def _req(**kw):
    base = dict(center=SEOUL, distance_km=5.0, duration_min=30.0)
    base.update(kw)
    return CourseRequest(**base)


def _candidate(dist_m):
    path = [(37.5663 + i * 0.001, 126.9779) for i in range(50)]
    return RawCandidate(path, dist_m, 0.0, False)


@pytest.fixture
def no_durunubi(monkeypatch):
    monkeypatch.setattr(pipeline.durunubi, "find_nearby_courses", lambda **kw: [])


def _patch_generator(monkeypatch, batch):
    async def fake(**kwargs):
        return batch
    monkeypatch.setattr(pipeline.generator, "generate_candidates", fake)


def _run(monkeypatch, dist_m_list, **req_kw):
    _patch_generator(
        monkeypatch, CandidateBatch(candidates=[_candidate(d) for d in dist_m_list]))
    return asyncio.run(pipeline.create_courses(_req(**req_kw), StubEmbedding()))


# ── 목표 요약 계산 ──────────────────────────────────────────────────────
@pytest.mark.parametrize("dist_km,dur_min,pace_sec,display,speed,intensity", [
    (5.0, 30.0, 360, "6:00/km", 10.0, "중"),
    (5.0, 25.0, 300, "5:00/km", 12.0, "상"),
    (5.0, 40.0, 480, "8:00/km", 7.5, "하"),
])
def test_goal_summary_values(dist_km, dur_min, pace_sec, display, speed, intensity):
    s = goal.compute_goal_summary(dist_km, dur_min, CFG)
    assert s.target_distance_km == dist_km
    assert s.target_duration_min == dur_min
    assert s.target_pace_sec_per_km == pace_sec
    assert s.target_pace_display == display
    assert s.target_speed_kmh == speed
    assert s.target_intensity == intensity


def test_intensity_boundaries_are_inclusive():
    """상: 330초 이하 / 중: 330 초과 ~ 420 이하 / 하: 420 초과."""
    assert goal.classify_intensity(330, CFG) == "상"
    assert goal.classify_intensity(330.1, CFG) == "중"
    assert goal.classify_intensity(420, CFG) == "중"
    assert goal.classify_intensity(420.1, CFG) == "하"


def test_intensity_thresholds_come_from_config():
    strict = {"target_intensity": {"high_max_pace_sec_per_km": 240,
                                   "medium_max_pace_sec_per_km": 300}}
    assert goal.classify_intensity(300, strict) == "중"
    assert goal.classify_intensity(240, strict) == "상"


def test_pace_display_pads_seconds():
    assert goal.format_pace(365) == "6:05/km"
    assert goal.format_pace(3600) == "60:00/km"


def test_goal_summary_none_for_non_positive_input():
    assert goal.compute_goal_summary(0.0, 30.0, CFG) is None
    assert goal.compute_goal_summary(5.0, 0.0, CFG) is None


# ── 코스별 거리·시간 오차 ───────────────────────────────────────────────
def test_course_metrics_for_5_18km():
    """5km/30분 목표에 실제 5.18km — 거리 오차 180m, 예상 시간은 목표 페이스 환산값."""
    s = goal.compute_goal_summary(5.0, 30.0, CFG)
    error_m, accuracy, est_min, time_err = goal.course_metrics(5180.0, s)
    assert error_m == 180
    assert accuracy is DistanceAccuracy.EXACT
    # 5.18km × 360초/km ÷ 60 = 31.08분
    assert est_min == pytest.approx(31.08, abs=0.01)
    assert time_err == pytest.approx(1.08, abs=0.01)


def test_course_metrics_for_5_35km_is_relaxed_400():
    s = goal.compute_goal_summary(5.0, 30.0, CFG)
    error_m, accuracy, est_min, time_err = goal.course_metrics(5350.0, s)
    assert error_m == 350
    assert accuracy is DistanceAccuracy.RELAXED_400M
    assert est_min == pytest.approx(32.1, abs=0.01)
    assert time_err == pytest.approx(2.1, abs=0.01)


@pytest.mark.parametrize("error_m,expected", [
    (0, DistanceAccuracy.EXACT),
    (200, DistanceAccuracy.EXACT),
    (201, DistanceAccuracy.RELAXED_300M),
    (300, DistanceAccuracy.RELAXED_300M),
    (301, DistanceAccuracy.RELAXED_400M),
    (400, DistanceAccuracy.RELAXED_400M),
    (401, None),
])
def test_accuracy_bands(error_m, expected):
    assert goal.classify_accuracy(error_m) is expected


def test_shorter_than_target_also_counts_as_error():
    s = goal.compute_goal_summary(5.0, 30.0, CFG)
    error_m, accuracy, _, _ = goal.course_metrics(4750.0, s)
    assert error_m == 250
    assert accuracy is DistanceAccuracy.RELAXED_300M


# ── 파이프라인 통합 ─────────────────────────────────────────────────────
def test_response_carries_goal_summary(monkeypatch, no_durunubi):
    res = _run(monkeypatch, [5000.0], distance_km=5.0, duration_min=30.0)
    assert isinstance(res.goal_summary, GoalSummary)
    assert res.goal_summary.target_pace_display == "6:00/km"
    assert res.goal_summary.target_intensity == "중"


def test_goal_summary_present_even_when_generation_fails(monkeypatch, no_durunubi):
    res = _run(monkeypatch, [], distance_km=5.0, duration_min=30.0)
    assert res.courses == []
    assert res.fallback_reason is not None
    assert res.goal_summary is not None, "실패해도 목표 요약은 돌려준다"


def test_course_fields_are_populated(monkeypatch, no_durunubi):
    res = _run(monkeypatch, [5180.0], distance_km=5.0, duration_min=30.0)
    c = res.courses[0]
    assert c.distance == 5.18
    assert c.distance_error_m == 180
    assert c.distance_accuracy is DistanceAccuracy.EXACT
    assert c.estimated_duration_min == pytest.approx(31.08, abs=0.01)
    assert c.time_error_min == pytest.approx(1.08, abs=0.01)


def test_courses_beyond_400m_are_not_returned(monkeypatch, no_durunubi):
    """±400m 초과 후보는 반환하지 않는다."""
    res = _run(monkeypatch, [5450.0], distance_km=5.0, duration_min=30.0)
    assert res.courses == []


def test_mixed_accuracy_courses(monkeypatch, no_durunubi):
    res = _run(monkeypatch, [5100.0, 5280.0, 5350.0], distance_km=5.0, duration_min=30.0)
    assert [c.distance_accuracy for c in res.courses] == [
        DistanceAccuracy.EXACT,
        DistanceAccuracy.RELAXED_300M,
        DistanceAccuracy.RELAXED_400M,
    ]


# ── 시간 오차 경고 ──────────────────────────────────────────────────────
def test_large_time_error_warns_without_removing_course(monkeypatch, no_durunubi):
    """시간 오차가 5분을 넘어도 코스를 삭제하지 않고 경고만 붙인다.

    목표 5km/70분 → 페이스 840초/km(14분/km). 거리 오차 400m면
    시간 오차는 0.4km × 14 = 5.6분으로 임계값(5분)을 넘는다.
    페이스가 느릴수록 같은 거리 오차가 큰 시간 오차로 증폭된다.
    """
    res = _run(monkeypatch, [5400.0], distance_km=5.0, duration_min=70.0)
    c = res.courses[0]
    assert res.courses, "경고가 붙어도 코스는 남아야 한다"
    assert c.time_error_min == pytest.approx(5.6, abs=0.01)
    assert any("목표 시간과" in w and "실제 완주 시간 예측이 아닙니다" in w
               for w in res.warnings)


def test_small_time_error_produces_no_warning(monkeypatch, no_durunubi):
    res = _run(monkeypatch, [5100.0], distance_km=5.0, duration_min=30.0)
    assert res.courses[0].time_error_min < 5
    assert res.warnings == []


def test_time_error_warning_counts_affected_courses(monkeypatch, no_durunubi):
    # 5km/70분 기준 거리 오차 358m 이상이어야 시간 오차가 5분을 넘는다.
    res = _run(monkeypatch, [5400.0, 5380.0], distance_km=5.0, duration_min=70.0)
    warning = next(w for w in res.warnings if "목표 시간과" in w)
    assert "2개" in warning


def test_only_courses_over_threshold_are_counted(monkeypatch, no_durunubi):
    """임계값을 넘는 코스만 경고에 집계된다 (350m → 4.9분이라 제외)."""
    res = _run(monkeypatch, [5400.0, 5350.0], distance_km=5.0, duration_min=70.0)
    warning = next(w for w in res.warnings if "목표 시간과" in w)
    assert "1개" in warning
    assert len(res.courses) == 2, "경고 대상이 아니어도 코스는 모두 남는다"


def test_time_error_is_not_a_generation_filter(monkeypatch, no_durunubi):
    """시간 오차가 커도 후보 수는 줄어들지 않는다."""
    res = _run(monkeypatch, [5400.0, 5380.0, 5360.0], distance_km=5.0, duration_min=70.0)
    assert len(res.courses) == 3
    assert all(c.time_error_min > 5 for c in res.courses)
