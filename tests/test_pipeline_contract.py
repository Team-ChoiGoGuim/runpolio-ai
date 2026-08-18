"""파이프라인 입출력 계약 테스트 (난이도 제거 후)."""
import asyncio

import pytest

from src.course import generator, pipeline
from src.course.generator import CandidateBatch, RawCandidate, TmapError
from src.course.schemas import CourseRequest, CourseSource, Theme

SEOUL = (37.5663, 126.9779)


class StubEmbedding:
    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]


def _req(**kw):
    base = dict(center=SEOUL, distance_km=5.0, duration_min=30.0)
    base.update(kw)
    return CourseRequest(**base)


def _fake_candidate(dist_m=5000.0):
    path = [(37.5663 + i * 0.001, 126.9779) for i in range(50)]
    return RawCandidate(path, dist_m, 0.0, False)


@pytest.fixture
def no_durunubi(monkeypatch):
    monkeypatch.setattr(pipeline.durunubi, "find_nearby_courses", lambda **kw: [])


def _patch_generator(monkeypatch, batch):
    async def fake(**kwargs):
        return batch
    monkeypatch.setattr(pipeline.generator, "generate_candidates", fake)


# ── 요청 모델 ───────────────────────────────────────────────────────────
def test_request_requires_distance_and_duration():
    with pytest.raises(TypeError):
        CourseRequest(center=SEOUL, distance_km=5.0)  # duration_min 누락
    with pytest.raises(TypeError):
        CourseRequest(center=SEOUL, duration_min=30.0)  # distance_km 누락


def test_request_has_no_difficulty_field():
    assert not hasattr(_req(), "difficulty")
    assert not hasattr(pipeline.CourseResponse(), "difficulty")


def test_implied_pace():
    assert _req(distance_km=5.0, duration_min=30.0).implied_pace_min_per_km == 6.0
    assert _req(distance_km=5.0, duration_min=10.0).implied_pace_min_per_km == 2.0


# ── 응답 모델 ───────────────────────────────────────────────────────────
def test_course_has_no_difficulty_or_elevation(monkeypatch, no_durunubi):
    _patch_generator(monkeypatch, CandidateBatch(candidates=[_fake_candidate()]))
    res = asyncio.run(pipeline.create_courses(_req(), StubEmbedding()))
    c = res.courses[0]
    assert not hasattr(c, "difficulty")
    assert not hasattr(c, "elevation_gain")
    assert not hasattr(c, "expected_duration")


def test_target_duration_is_echoed_verbatim(monkeypatch, no_durunubi):
    _patch_generator(monkeypatch, CandidateBatch(candidates=[_fake_candidate()]))
    res = asyncio.run(pipeline.create_courses(_req(duration_min=42.5), StubEmbedding()))
    assert all(c.target_duration_min == 42.5 for c in res.courses)


def test_ai_generated_has_no_source_course_id(monkeypatch, no_durunubi):
    _patch_generator(monkeypatch, CandidateBatch(candidates=[_fake_candidate()]))
    res = asyncio.run(pipeline.create_courses(_req(), StubEmbedding()))
    assert res.courses[0].source is CourseSource.AI_GENERATED
    assert res.courses[0].source_course_id is None


def test_durunubi_course_exposes_source_course_id(monkeypatch):
    path = [[37.5663 + i * 0.0005, 126.9779] for i in range(200)]
    monkeypatch.setattr(
        pipeline.durunubi, "find_nearby_courses",
        lambda **kw: [{"course_id": "T_CRS_MNG0000004378", "theme": ["도심"], "path": path}],
    )
    _patch_generator(monkeypatch, CandidateBatch())
    res = asyncio.run(pipeline.create_courses(_req(theme=Theme.CITY), StubEmbedding()))
    assert res.courses[0].source is CourseSource.DURUNUBI
    assert res.courses[0].source_course_id == "T_CRS_MNG0000004378"
    assert res.courses[0].course_id.startswith("crs_")
    assert res.courses[0].course_id != res.courses[0].source_course_id


def test_course_id_is_ephemeral(monkeypatch, no_durunubi):
    _patch_generator(monkeypatch, CandidateBatch(candidates=[_fake_candidate()]))
    a = asyncio.run(pipeline.create_courses(_req(), StubEmbedding()))
    b = asyncio.run(pipeline.create_courses(_req(), StubEmbedding()))
    assert a.courses[0].course_id != b.courses[0].course_id


# ── 목표 시간 현실성 경고 ───────────────────────────────────────────────
def test_realistic_pace_produces_no_warning(monkeypatch, no_durunubi):
    _patch_generator(monkeypatch, CandidateBatch(candidates=[_fake_candidate()]))
    res = asyncio.run(pipeline.create_courses(_req(duration_min=30.0), StubEmbedding()))
    assert res.warnings == []


def test_unrealistic_pace_warns_but_still_generates(monkeypatch, no_durunubi):
    """5km를 10분 — 거절하지 않고 거리 기준으로 생성하되 안내를 붙인다."""
    _patch_generator(monkeypatch, CandidateBatch(candidates=[_fake_candidate()]))
    res = asyncio.run(pipeline.create_courses(
        _req(distance_km=5.0, duration_min=10.0), StubEmbedding()))
    assert res.courses, "경고가 있어도 코스는 생성되어야 한다"
    assert len(res.warnings) == 1
    assert "목표 시간은 참고값" in res.warnings[0]
    assert "거리 기준" in res.warnings[0]


def test_absurdly_slow_pace_also_warns(monkeypatch, no_durunubi):
    _patch_generator(monkeypatch, CandidateBatch(candidates=[_fake_candidate()]))
    res = asyncio.run(pipeline.create_courses(
        _req(distance_km=5.0, duration_min=600.0), StubEmbedding()))
    assert len(res.warnings) == 1


def test_warning_is_kept_on_total_failure(monkeypatch, no_durunubi):
    _patch_generator(monkeypatch, CandidateBatch())
    res = asyncio.run(pipeline.create_courses(
        _req(duration_min=10.0), StubEmbedding()))
    assert res.courses == []
    assert res.fallback_reason is not None
    assert res.warnings, "실패 응답에도 경고는 유지되어야 한다"


# ── 실패 계약 ───────────────────────────────────────────────────────────
def test_total_tmap_failure_returns_fallback_not_exception(monkeypatch, no_durunubi):
    """모든 후보 실패 → 예외 전파(500) 대신 빈 courses + fallback_reason."""
    batch = CandidateBatch()
    batch.errors[TmapError.INVALID_JSON.value] = 10
    _patch_generator(monkeypatch, batch)
    res = asyncio.run(pipeline.create_courses(_req(), StubEmbedding()))
    assert res.courses == []
    assert "조건에 맞는 경로를 찾지 못했습니다" in res.fallback_reason


def test_fallback_reason_is_none_when_any_course_exists(monkeypatch, no_durunubi):
    _patch_generator(monkeypatch, CandidateBatch(candidates=[_fake_candidate()]))
    res = asyncio.run(pipeline.create_courses(_req(), StubEmbedding()))
    assert res.fallback_reason is None


def test_max_three_courses(monkeypatch, no_durunubi):
    _patch_generator(
        monkeypatch, CandidateBatch(candidates=[_fake_candidate() for _ in range(10)]))
    res = asyncio.run(pipeline.create_courses(_req(), StubEmbedding()))
    assert len(res.courses) <= 3


def test_durunubi_plus_two_ai(monkeypatch):
    path = [[37.5663 + i * 0.0005, 126.9779] for i in range(200)]
    monkeypatch.setattr(
        pipeline.durunubi, "find_nearby_courses",
        lambda **kw: [{"course_id": "T_CRS_X", "theme": ["도심"], "path": path}],
    )
    _patch_generator(
        monkeypatch, CandidateBatch(candidates=[_fake_candidate() for _ in range(5)]))
    res = asyncio.run(pipeline.create_courses(_req(theme=Theme.CITY), StubEmbedding()))
    sources = [c.source for c in res.courses]
    assert sources[0] is CourseSource.DURUNUBI
    assert sources.count(CourseSource.AI_GENERATED) == 2
    assert len(res.courses) == 3
