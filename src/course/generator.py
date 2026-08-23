"""TMAP 보행자 경로 기반 커스텀 코스 후보 생성.

호출 예산 구조 (요청당 최대 25회):

  1차   최대 10개 후보 × 1회        = 최대 10회
  보정  목표 거리에 가까운 5개 × 3회 = 최대 15회
                                     ─────────
                                       최대 25회

  ±200m 안에서 서로 다른 경로를 3개 확보하면 즉시 모든 생성을 중단한다.
  ±300m·±400m 로 넓히는 단계는 새 TMAP 호출 없이 이미 만든 후보에서만 고른다.

TMAP 호출은 어떤 이유로 실패하든 예외를 밖으로 내보내지 않는다. 모든 실패는
구조화된 오류 코드(TmapError)로 변환되어 집계되고, 후보 하나가 실패해도
나머지 후보 생성은 계속된다.
"""
from __future__ import annotations
import asyncio
import json as _json
import logging
import math
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import httpx

from src.common.geo import Point, destination_point, total_distance_m

logger = logging.getLogger(__name__)

TMAP_URL = "https://apis.openapi.sk.com/tmap/routes/pedestrian?version=1"

# 비정상 응답 본문은 진단에 필요한 앞부분만 남긴다.
_BODY_LOG_LIMIT = 200
_SECRET_LABELS = re.compile(
    r"(?i)\b(appkey|api[-_]?key|authorization|token|secret)\b\s*[=:]\s*\S+"
)

# 경로 동일성 판정: 균등 표본 좌표를 약 11m 격자로 반올림해 지문을 만든다.
_SIGNATURE_SAMPLES = 8
_SIGNATURE_PRECISION = 4


class TmapError(str, Enum):
    """내부 관측용 실패 분류. 사용자에게 그대로 노출하지 않는다."""

    TIMEOUT = "TMAP_TIMEOUT"
    HTTP_ERROR = "TMAP_HTTP_ERROR"
    NETWORK_ERROR = "TMAP_NETWORK_ERROR"
    INVALID_CONTENT_TYPE = "TMAP_INVALID_CONTENT_TYPE"
    INVALID_JSON = "TMAP_INVALID_JSON"
    EMPTY_ROUTE = "TMAP_EMPTY_ROUTE"
    UNEXPECTED = "TMAP_UNEXPECTED"


@dataclass
class RawCandidate:
    path: list[Point]
    distance_m: float
    bearing_offset: float
    clockwise: bool


@dataclass
class RouteResult:
    path: Optional[list[Point]] = None
    error: Optional[TmapError] = None


@dataclass
class GenerationStats:
    """호출 예산·캐시·단계별 후보 수 관측치."""

    tmap_calls: int = 0
    initial_calls: int = 0
    refine_calls: int = 0
    call_budget: int = 0
    cache_hit: bool = False
    initial_candidates: int = 0      # 1차에서 성공한 경로 수
    refine_targets: int = 0          # 보정 대상으로 고른 후보 수
    pool_size: int = 0               # 최종 후보 풀(서로 다른 경로) 크기
    accepted_in_band: int = 0        # ±accept_band 안의 서로 다른 경로 수
    selected_band_m: Optional[int] = None  # 최종 선택에 사용한 밴드
    early_stopped: bool = False
    budget_exhausted: bool = False
    errors: Counter = field(default_factory=Counter)

    def as_log_dict(self) -> dict:
        return {
            "tmap_calls": self.tmap_calls,
            "budget": self.call_budget,
            "initial_calls": self.initial_calls,
            "refine_calls": self.refine_calls,
            "cache_hit": self.cache_hit,
            "initial_candidates": self.initial_candidates,
            "refine_targets": self.refine_targets,
            "pool_size": self.pool_size,
            "accepted_in_band": self.accepted_in_band,
            "selected_band_m": self.selected_band_m,
            "early_stopped": self.early_stopped,
            "budget_exhausted": self.budget_exhausted,
            "errors": dict(self.errors),
        }


@dataclass
class CandidateBatch:
    candidates: list[RawCandidate] = field(default_factory=list)
    errors: Counter = field(default_factory=Counter)
    stats: GenerationStats = field(default_factory=GenerationStats)

    def __bool__(self) -> bool:
        return bool(self.candidates)


def _safe_snippet(text: Optional[str]) -> str:
    """로그에 남길 응답 본문 일부. 인증키와 라벨링된 비밀값은 지운다."""
    if not text:
        return ""
    redacted = _SECRET_LABELS.sub(r"\1=***", text)
    key = os.environ.get("TMAP_APP_KEY")
    if key:
        redacted = redacted.replace(key, "***")
    snippet = redacted[:_BODY_LOG_LIMIT].replace("\n", " ").replace("\r", " ")
    return snippet + ("…" if len(redacted) > _BODY_LOG_LIMIT else "")


def route_signature(path: list[Point]) -> tuple:
    """경로 지문. 같은 지문이면 사실상 같은 경로로 본다."""
    if not path:
        return ()
    n = len(path)
    idxs = [round(i * (n - 1) / (_SIGNATURE_SAMPLES - 1)) for i in range(_SIGNATURE_SAMPLES)]
    return tuple(
        (round(path[i][0], _SIGNATURE_PRECISION), round(path[i][1], _SIGNATURE_PRECISION))
        for i in idxs
    )


def _circle_waypoints(center, radius_m, n, offset_deg, clockwise):
    step = 360.0 / n
    bearings = [offset_deg + step * i for i in range(1, n)]
    if clockwise:
        bearings = [-b for b in bearings]
    return [destination_point(center, b % 360, radius_m) for b in bearings]


async def _tmap_route(client, start, end, passes) -> RouteResult:
    """TMAP 보행자 경로 1건 조회. 어떤 실패든 RouteResult 로 돌려준다."""
    payload = {
        "startX": start[1], "startY": start[0],
        "endX": end[1], "endY": end[0],
        "reqCoordType": "WGS84GEO", "resCoordType": "WGS84GEO",
        "startName": "start", "endName": "end",
    }
    if passes:
        payload["passList"] = "_".join(f"{lng},{lat}" for lat, lng in passes)

    headers = {"appKey": os.environ.get("TMAP_APP_KEY", "")}

    try:
        r = await client.post(TMAP_URL, json=payload, headers=headers)
        r.raise_for_status()
    except httpx.TimeoutException:
        logger.warning("[%s] TMAP 응답 시간 초과", TmapError.TIMEOUT.value)
        return RouteResult(error=TmapError.TIMEOUT)
    except httpx.HTTPStatusError as e:
        logger.warning(
            "[%s] TMAP HTTP %s | body=%s",
            TmapError.HTTP_ERROR.value, e.response.status_code,
            _safe_snippet(e.response.text),
        )
        return RouteResult(error=TmapError.HTTP_ERROR)
    except httpx.HTTPError as e:
        logger.warning(
            "[%s] TMAP 네트워크 오류 | %s", TmapError.NETWORK_ERROR.value, type(e).__name__
        )
        return RouteResult(error=TmapError.NETWORK_ERROR)

    # TMAP은 경로를 찾을 수 없을 때(예: 해상 좌표) 204 No Content 를 빈 본문으로 준다.
    # 파싱 실패가 아니라 '경로 없음'으로 분류해야 관측 지표가 왜곡되지 않는다.
    if r.status_code == 204 or not r.text.strip():
        logger.info("[%s] TMAP 경로 없음 (HTTP %s)", TmapError.EMPTY_ROUTE.value, r.status_code)
        return RouteResult(error=TmapError.EMPTY_ROUTE)

    content_type = r.headers.get("content-type", "")
    if "json" not in content_type.lower():
        logger.warning(
            "[%s] TMAP 비-JSON 응답 | content-type=%s body=%s",
            TmapError.INVALID_CONTENT_TYPE.value, content_type or "(없음)",
            _safe_snippet(r.text),
        )
        return RouteResult(error=TmapError.INVALID_CONTENT_TYPE)

    try:
        # strict=False: TMAP 응답에 제어문자가 섞여 오는 사례가 있어 관대하게 파싱한다.
        data = _json.loads(r.text, strict=False)
    except ValueError:  # JSONDecodeError 포함
        logger.warning(
            "[%s] TMAP 응답 JSON 파싱 실패 | body=%s",
            TmapError.INVALID_JSON.value, _safe_snippet(r.text),
        )
        return RouteResult(error=TmapError.INVALID_JSON)

    if not isinstance(data, dict):
        logger.warning(
            "[%s] TMAP 응답이 객체가 아님 | type=%s",
            TmapError.INVALID_JSON.value, type(data).__name__,
        )
        return RouteResult(error=TmapError.INVALID_JSON)

    coords: list[Point] = []
    for feat in data.get("features") or []:
        if not isinstance(feat, dict):
            continue
        geom = feat.get("geometry") or {}
        if geom.get("type") == "LineString":
            for pair in geom.get("coordinates") or []:
                try:
                    lng, lat = pair[0], pair[1]
                except (TypeError, IndexError):
                    continue
                coords.append((lat, lng))

    if not coords:
        logger.info("[%s] TMAP 응답에 경로 좌표 없음", TmapError.EMPTY_ROUTE.value)
        return RouteResult(error=TmapError.EMPTY_ROUTE)
    return RouteResult(path=coords)


# ── 예산·동시성·조기중단 조율 ───────────────────────────────────────────
@dataclass
class _Attempt:
    """후보 하나의 반경 보정 상태."""

    offset_deg: float
    clockwise: bool
    radius: float


class _Coordinator:
    """호출 예산, 동시 요청 수, 조기 중단 조건을 한곳에서 관리한다."""

    def __init__(self, budget, max_concurrency, target_m, accept_band_m, target_accepted):
        self.remaining = budget
        self.sem = asyncio.Semaphore(max_concurrency)
        self.target_m = target_m
        self.accept_band_m = accept_band_m
        self.target_accepted = target_accepted
        self.stop = False
        self.budget_exhausted = False
        self.pool: list[RawCandidate] = []
        self._seen: set[tuple] = set()
        self._accepted: set[tuple] = set()

    def try_reserve(self) -> bool:
        """호출 1회를 예약한다. 중단됐거나 예산이 없으면 False.

        asyncio 단일 스레드에서 await 없이 검사·감소하므로 원자적이다.
        """
        if self.stop:
            return False
        if self.remaining <= 0:
            self.budget_exhausted = True
            return False
        self.remaining -= 1
        return True

    def add(self, candidate: RawCandidate) -> bool:
        """후보를 풀에 넣는다. 이미 같은 경로가 있으면 False."""
        sig = route_signature(candidate.path)
        if sig in self._seen:
            return False
        self._seen.add(sig)
        self.pool.append(candidate)
        if abs(candidate.distance_m - self.target_m) <= self.accept_band_m:
            self._accepted.add(sig)
            if len(self._accepted) >= self.target_accepted:
                self.stop = True
        return True

    @property
    def accepted_count(self) -> int:
        return len(self._accepted)

    def in_band(self, candidate: RawCandidate) -> bool:
        return abs(candidate.distance_m - self.target_m) <= self.accept_band_m


async def _call_once(
    coord: _Coordinator, client, center, attempt: _Attempt, stats: GenerationStats,
    is_refine: bool,
) -> tuple[Optional[RawCandidate], Optional[TmapError]]:
    """TMAP 1회 호출. 예산이 없거나 중단됐으면 호출하지 않고 (None, None).

    성공한 경로는 **즉시** 코디네이터에 등록한다. 모든 태스크가 끝난 뒤에 등록하면
    조기 중단 조건이 1차 단계 안에서 성립할 수 없다.
    """
    async with coord.sem:
        # 세마포어를 기다리는 사이에 중단 조건이 성립했을 수 있다.
        if not coord.try_reserve():
            return None, None
        stats.tmap_calls += 1
        if is_refine:
            stats.refine_calls += 1
        else:
            stats.initial_calls += 1
        wps = _circle_waypoints(
            center, attempt.radius, n=4,
            offset_deg=attempt.offset_deg, clockwise=attempt.clockwise,
        )
        result = await _tmap_route(client, start=center, end=center, passes=wps)

    if result.path is None:
        return None, result.error

    dist = total_distance_m(result.path)
    if dist <= 0:
        return None, TmapError.EMPTY_ROUTE

    # 다음 보정을 위한 반경 갱신
    attempt.radius *= coord.target_m / dist
    candidate = RawCandidate(result.path, dist, attempt.offset_deg, attempt.clockwise)
    coord.add(candidate)  # 즉시 등록해야 다른 태스크가 중단 신호를 볼 수 있다
    return candidate, None


# ── TTL 캐시 ────────────────────────────────────────────────────────────
_cache: dict[tuple, tuple[float, list[RawCandidate]]] = {}


def _cache_key(center: Point, target_m: float) -> tuple:
    return (round(center[0], 5), round(center[1], 5), round(target_m))


def clear_candidate_cache() -> None:
    _cache.clear()


def _cache_get(key: tuple, ttl_sec: float) -> Optional[list[RawCandidate]]:
    entry = _cache.get(key)
    if entry is None:
        return None
    stored_at, candidates = entry
    if time.monotonic() - stored_at > ttl_sec:
        _cache.pop(key, None)
        return None
    return candidates


def _cache_put(key: tuple, candidates: list[RawCandidate], max_entries: int) -> None:
    if max_entries <= 0:
        return
    _cache[key] = (time.monotonic(), candidates)
    while len(_cache) > max_entries:
        _cache.pop(next(iter(_cache)))  # 가장 먼저 들어온 항목부터 버린다


# ── 밴드 선택 (추가 호출 없음) ──────────────────────────────────────────
def select_within_bands(
    candidates: list[RawCandidate], target_m: float, bands: list[int], need: int,
) -> tuple[list[RawCandidate], Optional[int]]:
    """이미 생성한 후보 중에서 밴드를 넓혀 가며 고른다. TMAP을 호출하지 않는다."""
    if not candidates:
        return [], None
    widest: list[RawCandidate] = []
    used: Optional[int] = None
    for band in bands:
        pool = [c for c in candidates if abs(c.distance_m - target_m) <= band]
        widest, used = pool, band
        if len(pool) >= need:
            return pool, band
    # 가장 넓은 밴드로도 부족하면 거기까지 모인 후보를 그대로 쓴다.
    return widest, used


# ── 메인 진입점 ─────────────────────────────────────────────────────────
async def generate_candidates(
    center, target_m, n_candidates=10, refine_top_k=5, max_refine_per_candidate=3,
    call_budget=25, max_concurrency=5, accept_band_m=200, target_accepted=3,
    timeout_sec=3.0, transport=None,
    cache_ttl_sec=300.0, cache_max_entries=128, use_cache=True,
) -> CandidateBatch:
    """후보 경로를 예산 안에서 생성한다.

    1차 생성 → (부족하면) 목표 거리에 가까운 후보만 반경 보정.
    ±accept_band_m 안의 서로 다른 경로가 target_accepted 개 모이면 즉시 중단한다.
    생성되는 경로는 항상 center 로 되돌아오는 순환형이다(왕복 웨이포인트 구조).
    """
    stats = GenerationStats(call_budget=call_budget)

    key = _cache_key(center, target_m)
    if use_cache:
        cached = _cache_get(key, cache_ttl_sec)
        if cached is not None:
            stats.cache_hit = True
            stats.pool_size = len(cached)
            stats.accepted_in_band = sum(
                1 for c in cached if abs(c.distance_m - target_m) <= accept_band_m
            )
            logger.info("TMAP 후보 캐시 적중 | %s", stats.as_log_dict())
            return CandidateBatch(candidates=list(cached), stats=stats)

    coord = _Coordinator(
        call_budget, max_concurrency, target_m, accept_band_m, target_accepted
    )
    errors: Counter = Counter()
    base_radius = target_m / (2 * math.pi)

    async with httpx.AsyncClient(timeout=timeout_sec, transport=transport) as client:
        # ── 1차: 후보당 1회 ────────────────────────────────────────────
        attempts = [
            _Attempt(offset_deg=(360.0 / n_candidates) * i,
                     clockwise=(i % 2 == 1), radius=base_radius)
            for i in range(n_candidates)
        ]

        async def initial(attempt):
            return await _call_once(coord, client, center, attempt, stats, is_refine=False)

        results = await asyncio.gather(
            *(initial(a) for a in attempts), return_exceptions=True
        )
        # 밴드 밖 후보만 보정 대상이 된다. 어느 후보에서 나왔는지 알아야
        # 반경을 이어서 조정할 수 있으므로 (시도, 결과 거리) 를 함께 들고 간다.
        out_of_band: list[tuple[_Attempt, float]] = []
        for attempt, res in zip(attempts, results):
            if isinstance(res, BaseException):
                logger.warning(
                    "[%s] 후보 생성 중 예외 | %s",
                    TmapError.UNEXPECTED.value, type(res).__name__,
                )
                errors[TmapError.UNEXPECTED.value] += 1
                continue
            candidate, error = res
            if candidate is not None:
                if not coord.in_band(candidate):
                    out_of_band.append((attempt, abs(candidate.distance_m - target_m)))
            elif error is not None:
                errors[error.value] += 1
        stats.initial_candidates = len(coord.pool)

        # ── 보정: 목표 거리에 가까운 후보만, 후보당 최대 N회 ──────────
        if not coord.stop and max_refine_per_candidate > 0 and out_of_band:
            out_of_band.sort(key=lambda pair: pair[1])
            targets = [attempt for attempt, _ in out_of_band[:refine_top_k]]
            stats.refine_targets = len(targets)

            async def refine(attempt):
                local_errors = []
                for _ in range(max_refine_per_candidate):
                    if coord.stop:
                        break
                    candidate, error = await _call_once(
                        coord, client, center, attempt, stats, is_refine=True
                    )
                    if candidate is None:
                        if error is not None:
                            local_errors.append(error.value)
                        break
                    if coord.in_band(candidate):
                        break
                return local_errors

            refine_results = await asyncio.gather(
                *(refine(a) for a in targets), return_exceptions=True
            )
            for res in refine_results:
                if isinstance(res, BaseException):
                    logger.warning(
                        "[%s] 보정 중 예외 | %s",
                        TmapError.UNEXPECTED.value, type(res).__name__,
                    )
                    errors[TmapError.UNEXPECTED.value] += 1
                    continue
                for code in res:
                    errors[code] += 1

    stats.errors = errors
    stats.pool_size = len(coord.pool)
    stats.accepted_in_band = coord.accepted_count
    stats.early_stopped = coord.stop
    stats.budget_exhausted = coord.budget_exhausted

    if use_cache and coord.pool:
        _cache_put(key, list(coord.pool), cache_max_entries)

    log = logger.warning if errors and not coord.pool else logger.info
    log("TMAP 후보 생성 결과 | %s", stats.as_log_dict())
    return CandidateBatch(candidates=coord.pool, errors=errors, stats=stats)
