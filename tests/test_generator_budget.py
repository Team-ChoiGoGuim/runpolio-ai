"""TMAP 호출 예산·동시성·조기중단·캐시·밴드 선택 테스트."""
import asyncio
import json
import logging

import httpx
import pytest

from src.course import generator
from src.course.generator import RawCandidate, select_within_bands

CENTER = (37.5283, 126.9346)
TARGET_M = 5000.0


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("TMAP_APP_KEY", "test_key")
    generator.clear_candidate_cache()
    yield
    generator.clear_candidate_cache()


def _feature(n=51, step=0.0009, shift=0.0):
    coords = [[126.9346 + shift, 37.5283 + i * step] for i in range(n)]
    return {"features": [{"geometry": {"type": "LineString", "coordinates": coords}}]}


def _resp(payload):
    return httpx.Response(200, content=json.dumps(payload).encode(),
                          headers={"content-type": "application/json"})


class Recorder:
    """호출 수와 동시 실행 수를 기록하는 핸들러 팩토리."""

    def __init__(self, *, in_band=True, distinct=True, n=51, step=0.0009):
        self.calls = 0
        self.concurrent = 0
        self.max_concurrent = 0
        self.in_band = in_band
        self.distinct = distinct
        self.n, self.step = n, step

    async def __call__(self, request):
        self.calls += 1
        idx = self.calls  # await 전에 캡처해야 동시 실행 시 같은 값을 읽지 않는다
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            await asyncio.sleep(0)  # 다른 태스크에 양보해 동시성을 실제로 관측한다
            shift = idx * 0.002 if self.distinct else 0.0
            if self.in_band:
                return _resp(_feature(self.n, self.step, shift))
            return _resp(_feature(3, 0.0001, shift))  # 목표에서 한참 먼 경로
        finally:
            self.concurrent -= 1


def _run(handler, **kw):
    kw.setdefault("use_cache", False)
    return asyncio.run(generator.generate_candidates(
        CENTER, TARGET_M, transport=httpx.MockTransport(handler), **kw
    ))


# ── 호출 예산 ───────────────────────────────────────────────────────────
def test_never_exceeds_call_budget():
    """밴드를 절대 못 맞추는 응답이어도 25회를 넘지 않는다."""
    rec = Recorder(in_band=False)
    batch = _run(rec, n_candidates=10, refine_top_k=5,
                 max_refine_per_candidate=3, call_budget=25)
    assert rec.calls <= 25
    assert batch.stats.tmap_calls == rec.calls
    assert batch.stats.call_budget == 25


def test_full_budget_shape_is_10_plus_15():
    """1차 10회 + 보정 5후보×3회 = 25회."""
    rec = Recorder(in_band=False)
    batch = _run(rec, n_candidates=10, refine_top_k=5,
                 max_refine_per_candidate=3, call_budget=25)
    assert batch.stats.initial_calls == 10
    assert batch.stats.refine_calls == 15
    assert batch.stats.tmap_calls == 25
    assert batch.stats.refine_targets == 5


def test_budget_caps_below_theoretical_max():
    rec = Recorder(in_band=False)
    batch = _run(rec, n_candidates=10, refine_top_k=5,
                 max_refine_per_candidate=3, call_budget=12)
    assert rec.calls <= 12
    assert batch.stats.budget_exhausted is True


def test_initial_stage_is_capped_at_ten():
    rec = Recorder(in_band=False)
    _run(rec, n_candidates=10, refine_top_k=0, max_refine_per_candidate=0)
    assert rec.calls == 10


def test_refine_targets_capped_at_five():
    rec = Recorder(in_band=False)
    batch = _run(rec, n_candidates=10, refine_top_k=5, max_refine_per_candidate=1)
    assert batch.stats.refine_targets == 5
    assert batch.stats.refine_calls == 5


def test_refine_per_candidate_capped():
    rec = Recorder(in_band=False)
    batch = _run(rec, n_candidates=2, refine_top_k=5, max_refine_per_candidate=3)
    # 후보 2개 × 최대 3회
    assert batch.stats.refine_calls <= 6


# ── 조기 중단 ───────────────────────────────────────────────────────────
def test_early_stop_after_three_distinct_in_band():
    """±200m 안에서 서로 다른 경로 3개를 얻으면 남은 호출을 하지 않는다."""
    rec = Recorder(in_band=True, distinct=True)
    batch = _run(rec, n_candidates=10, max_concurrency=1,
                 accept_band_m=200, target_accepted=3)
    assert batch.stats.early_stopped is True
    assert batch.stats.accepted_in_band >= 3
    assert rec.calls == 3, "동시성 1이면 정확히 3회에서 멈춘다"
    assert batch.stats.refine_calls == 0


def test_early_stop_skips_refinement():
    rec = Recorder(in_band=True, distinct=True)
    batch = _run(rec, n_candidates=10, max_concurrency=5,
                 refine_top_k=5, max_refine_per_candidate=3)
    assert batch.stats.early_stopped is True
    assert batch.stats.refine_calls == 0
    assert rec.calls <= 10


def test_identical_routes_do_not_count_toward_early_stop():
    """같은 경로만 반복되면 '서로 다른 경로 3개'가 성립하지 않는다."""
    rec = Recorder(in_band=True, distinct=False)
    batch = _run(rec, n_candidates=10, max_concurrency=1,
                 refine_top_k=0, max_refine_per_candidate=0)
    assert batch.stats.accepted_in_band == 1
    assert batch.stats.early_stopped is False
    assert rec.calls == 10


# ── 동시성 ──────────────────────────────────────────────────────────────
def test_concurrency_never_exceeds_limit():
    rec = Recorder(in_band=False)
    _run(rec, n_candidates=10, max_concurrency=5, max_refine_per_candidate=0)
    assert rec.max_concurrent <= 5


def test_concurrency_limit_of_two_is_respected():
    rec = Recorder(in_band=False)
    _run(rec, n_candidates=10, max_concurrency=2, max_refine_per_candidate=0)
    assert rec.max_concurrent <= 2


# ── TTL 캐시 ────────────────────────────────────────────────────────────
def test_cache_hit_avoids_all_tmap_calls():
    rec = Recorder(in_band=True)
    first = asyncio.run(generator.generate_candidates(
        CENTER, TARGET_M, transport=httpx.MockTransport(rec),
        n_candidates=4, use_cache=True))
    calls_after_first = rec.calls
    assert first.stats.cache_hit is False

    second = asyncio.run(generator.generate_candidates(
        CENTER, TARGET_M, transport=httpx.MockTransport(rec),
        n_candidates=4, use_cache=True))
    assert second.stats.cache_hit is True
    assert second.stats.tmap_calls == 0
    assert rec.calls == calls_after_first, "캐시 적중 시 추가 호출이 없어야 한다"
    assert len(second.candidates) == len(first.candidates)


def test_cache_key_separates_by_distance():
    rec = Recorder(in_band=True)

    def gen(target):
        return asyncio.run(generator.generate_candidates(
            CENTER, target, transport=httpx.MockTransport(rec),
            n_candidates=2, use_cache=True))

    gen(5000.0)
    assert gen(5000.0).stats.cache_hit is True
    assert gen(7000.0).stats.cache_hit is False, "거리가 다르면 별개 항목"


def test_cache_expires_after_ttl():
    rec = Recorder(in_band=True)
    asyncio.run(generator.generate_candidates(
        CENTER, TARGET_M, transport=httpx.MockTransport(rec),
        n_candidates=2, use_cache=True, cache_ttl_sec=300))
    expired = asyncio.run(generator.generate_candidates(
        CENTER, TARGET_M, transport=httpx.MockTransport(rec),
        n_candidates=2, use_cache=True, cache_ttl_sec=-1))
    assert expired.stats.cache_hit is False


def test_cache_respects_max_entries():
    rec = Recorder(in_band=True)
    for target in (4000.0, 5000.0, 6000.0):
        asyncio.run(generator.generate_candidates(
            CENTER, target, transport=httpx.MockTransport(rec),
            n_candidates=1, use_cache=True, cache_max_entries=2))
    assert len(generator._cache) <= 2


# ── 밴드 선택 (추가 호출 없음) ──────────────────────────────────────────
def _cand(dist_m):
    return RawCandidate([(37.5, 127.0), (37.51, 127.0)], dist_m, 0.0, False)


def test_band_selection_prefers_tightest():
    cands = [_cand(4900), _cand(5100), _cand(5150), _cand(5350), _cand(5450)]
    pool, band = select_within_bands(cands, 5000.0, [200, 300, 400], need=3)
    assert band == 200
    assert len(pool) == 3


def test_band_widens_to_300_when_200_insufficient():
    cands = [_cand(4900), _cand(5250), _cand(5280)]
    pool, band = select_within_bands(cands, 5000.0, [200, 300, 400], need=3)
    assert band == 300
    assert len(pool) == 3


def test_band_widens_to_400_when_300_insufficient():
    cands = [_cand(4900), _cand(5350), _cand(5380)]
    pool, band = select_within_bands(cands, 5000.0, [200, 300, 400], need=3)
    assert band == 400
    assert len(pool) == 3


def test_band_selection_returns_widest_pool_when_still_short():
    cands = [_cand(4900), _cand(5390)]
    pool, band = select_within_bands(cands, 5000.0, [200, 300, 400], need=3)
    assert band == 400
    assert len(pool) == 2, "부족해도 모인 후보를 그대로 쓴다"


def test_band_selection_makes_no_http_calls():
    """±300·±400 단계는 순수 선택 연산이라 네트워크를 쓰지 않는다."""
    rec = Recorder(in_band=False)
    batch = _run(rec, n_candidates=4, max_refine_per_candidate=0)
    before = rec.calls
    select_within_bands(batch.candidates, TARGET_M, [200, 300, 400], need=3)
    assert rec.calls == before


# ── 관측 로그 ───────────────────────────────────────────────────────────
def test_stats_are_logged(caplog):
    rec = Recorder(in_band=True)
    with caplog.at_level(logging.INFO, logger=generator.__name__):
        _run(rec, n_candidates=4)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    for key in ("tmap_calls", "cache_hit", "initial_candidates", "pool_size",
                "accepted_in_band", "early_stopped"):
        assert key in logged


def test_cache_hit_is_logged(caplog):
    rec = Recorder(in_band=True)
    asyncio.run(generator.generate_candidates(
        CENTER, TARGET_M, transport=httpx.MockTransport(rec),
        n_candidates=2, use_cache=True))
    with caplog.at_level(logging.INFO, logger=generator.__name__):
        asyncio.run(generator.generate_candidates(
            CENTER, TARGET_M, transport=httpx.MockTransport(rec),
            n_candidates=2, use_cache=True))
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "캐시 적중" in logged
    assert "'cache_hit': True" in logged


def test_stats_as_log_dict_shape():
    rec = Recorder(in_band=True)
    stats = _run(rec, n_candidates=3).stats.as_log_dict()
    assert set(stats) >= {
        "tmap_calls", "budget", "initial_calls", "refine_calls", "cache_hit",
        "initial_candidates", "refine_targets", "pool_size", "accepted_in_band",
        "selected_band_m", "early_stopped", "budget_exhausted", "errors",
    }
