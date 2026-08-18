"""TMAP 실패 처리 테스트 — 어떤 실패도 예외로 새어나가지 않아야 한다."""
import asyncio
import json
import logging

import httpx
import pytest

from src.course import generator
from src.course.generator import TmapError

CENTER = (37.5283, 126.9346)
TARGET_M = 5000.0
APP_KEY = "super_secret_appkey_value_1234567890"


@pytest.fixture(autouse=True)
def _tmap_key(monkeypatch):
    monkeypatch.setenv("TMAP_APP_KEY", APP_KEY)


@pytest.fixture(autouse=True)
def _no_cache_leak():
    generator.clear_candidate_cache()
    yield
    generator.clear_candidate_cache()


def _line_feature(n=51, step=0.0009, shift=0.0):
    """목표 거리에 근접하는 직선 경로를 담은 TMAP 유사 응답.

    위도 0.0009도 ≈ 100m 이므로 50구간 ≈ 5,005m 로 TARGET_M(5,000m)의
    수용 밴드(±200m) 안에 들어온다. shift 로 경도를 밀어 서로 다른 경로를 만든다.
    """
    coords = [[126.9346 + shift, 37.5283 + i * step] for i in range(n)]
    return {"features": [{"geometry": {"type": "LineString", "coordinates": coords}}]}


def varied_handler(status=200):
    """호출마다 조금씩 다른 경로를 돌려주는 핸들러 (중복 제거를 피하기 위함)."""
    state = {"n": 0}

    def handler(req):
        state["n"] += 1
        return _json_response(_line_feature(shift=state["n"] * 0.002))
    return handler


def _json_response(payload, status=200):
    return httpx.Response(
        status, content=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
    )


def _run(handler, **kwargs):
    kwargs.setdefault("use_cache", False)
    transport = httpx.MockTransport(handler)
    return asyncio.run(generator.generate_candidates(
        CENTER, TARGET_M, transport=transport, **kwargs
    ))


def _route(handler):
    """_tmap_route 단건 호출."""
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await generator._tmap_route(c, CENTER, CENTER, passes=[])
    return asyncio.run(go())


# ── 정상 응답 ───────────────────────────────────────────────────────────
def test_valid_response_returns_path():
    res = _route(lambda req: _json_response(_line_feature()))
    assert res.error is None
    assert len(res.path) == 51
    assert res.path[0] == (37.5283, 126.9346)  # [lat, lng] 로 뒤집혀 저장된다


def test_generate_candidates_success():
    """±200m 안의 서로 다른 경로 3개를 얻는 즉시 멈추므로 4개를 요청해도 3개다."""
    batch = _run(varied_handler(), n_candidates=4)
    assert len(batch.candidates) == 3
    assert batch.errors == {}
    assert bool(batch) is True
    assert batch.stats.tmap_calls == 3
    assert batch.stats.early_stopped is True


def test_identical_routes_are_deduplicated():
    """같은 경로가 여러 번 나와도 '서로 다른 경로'로 세지 않는다."""
    batch = _run(lambda req: _json_response(_line_feature()), n_candidates=6)
    assert len(batch.candidates) == 1
    assert batch.stats.accepted_in_band == 1


# ── 200 + 비JSON 응답 ───────────────────────────────────────────────────
def test_200_with_html_body_is_invalid_content_type():
    def handler(req):
        return httpx.Response(200, text="<html>error page</html>",
                              headers={"content-type": "text/html"})
    res = _route(handler)
    assert res.path is None
    assert res.error is TmapError.INVALID_CONTENT_TYPE


def test_200_json_content_type_but_broken_body_is_invalid_json():
    """예전에 JSONDecodeError 가 그대로 전파되던 경로."""
    def handler(req):
        return httpx.Response(200, text="not json at all",
                              headers={"content-type": "application/json"})
    res = _route(handler)
    assert res.path is None
    assert res.error is TmapError.INVALID_JSON


def test_200_json_array_body_is_invalid_json():
    res = _route(lambda req: _json_response([1, 2, 3]))
    assert res.error is TmapError.INVALID_JSON


def test_empty_features_is_empty_route():
    res = _route(lambda req: _json_response({"features": []}))
    assert res.error is TmapError.EMPTY_ROUTE


def test_204_no_content_is_empty_route():
    """TMAP은 해상 좌표 등 경로 불가 지점에 204 + 빈 본문을 준다."""
    def handler(req):
        return httpx.Response(204, headers={"content-type": "application/json"})
    res = _route(handler)
    assert res.path is None
    assert res.error is TmapError.EMPTY_ROUTE


def test_200_with_blank_body_is_empty_route():
    def handler(req):
        return httpx.Response(200, text="   ",
                              headers={"content-type": "application/json"})
    assert _route(handler).error is TmapError.EMPTY_ROUTE


# ── HTTP 오류 ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 503])
def test_http_errors_are_classified(status):
    def handler(req):
        return httpx.Response(status, text="error",
                              headers={"content-type": "application/json"})
    res = _route(handler)
    assert res.path is None
    assert res.error is TmapError.HTTP_ERROR


# ── 타임아웃 / 네트워크 ─────────────────────────────────────────────────
def test_timeout_is_classified():
    def handler(req):
        raise httpx.ReadTimeout("timed out", request=req)
    assert _route(handler).error is TmapError.TIMEOUT


def test_connect_timeout_is_classified():
    def handler(req):
        raise httpx.ConnectTimeout("connect timed out", request=req)
    assert _route(handler).error is TmapError.TIMEOUT


def test_network_error_is_classified():
    def handler(req):
        raise httpx.ConnectError("connection refused", request=req)
    assert _route(handler).error is TmapError.NETWORK_ERROR


# ── 부분 실패 / 전체 실패 ───────────────────────────────────────────────
def test_partial_failure_still_returns_other_candidates():
    """후보 하나가 실패해도 나머지 생성은 계속된다."""
    state = {"n": 0}

    def handler(req):
        state["n"] += 1
        if state["n"] % 2 == 1:
            return httpx.Response(500, text="boom",
                                  headers={"content-type": "application/json"})
        return _json_response(_line_feature())

    batch = _run(handler, n_candidates=6, max_refine_per_candidate=0)
    assert batch.candidates, "일부 성공한 후보가 있어야 한다"
    assert batch.errors[TmapError.HTTP_ERROR.value] > 0


def test_all_candidates_fail_returns_empty_batch_without_raising():
    def handler(req):
        return httpx.Response(200, text="<html>nope</html>",
                              headers={"content-type": "text/html"})
    batch = _run(handler, n_candidates=5, max_refine_per_candidate=0)
    assert batch.candidates == []
    assert bool(batch) is False
    assert batch.errors[TmapError.INVALID_CONTENT_TYPE.value] == 5


def test_out_of_band_routes_are_kept_as_candidates():
    """밴드를 못 맞춘 경로도 후보 풀에는 남는다(±300·±400 단계에서 쓰인다)."""
    def handler(req):
        return _json_response(_line_feature(n=3, step=0.0001))  # 아주 짧은 경로
    batch = _run(handler, n_candidates=2, max_refine_per_candidate=0)
    assert batch.candidates, "밴드 밖이어도 후보로 보존되어야 한다"
    assert batch.stats.accepted_in_band == 0


# ── 로그 안전성 ─────────────────────────────────────────────────────────
def test_appkey_is_never_logged(caplog):
    def handler(req):
        body = f'{{"error": "invalid", "appKey": "{APP_KEY}"}}'
        return httpx.Response(200, text=body + " <<<broken",
                              headers={"content-type": "application/json"})
    with caplog.at_level(logging.WARNING, logger=generator.__name__):
        _route(handler)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert logged, "실패는 로그로 남아야 한다"
    assert APP_KEY not in logged
    assert TmapError.INVALID_JSON.value in logged


def test_long_body_is_truncated_in_log(caplog):
    def handler(req):
        return httpx.Response(200, text="x" * 5000,
                              headers={"content-type": "application/json"})
    with caplog.at_level(logging.WARNING, logger=generator.__name__):
        _route(handler)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "…" in logged
    assert len(logged) < 1000


def test_safe_snippet_redacts_labeled_secrets():
    out = generator._safe_snippet('appKey: abc123 token = zzz9 "x"')
    assert "abc123" not in out and "zzz9" not in out
