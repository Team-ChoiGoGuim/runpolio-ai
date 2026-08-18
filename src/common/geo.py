"""지리 좌표 계산 유틸리티. WGS84 (lat, lng) 통일."""
from __future__ import annotations
import math

EARTH_RADIUS_M = 6_371_000.0
Point = tuple[float, float]  # (lat, lng)


def haversine_m(a: Point, b: Point) -> float:
    lat1, lng1 = map(math.radians, a)
    lat2, lng2 = map(math.radians, b)
    dlat, dlng = lat2 - lat1, lng2 - lng1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def cumulative_distances_m(path: list[Point]) -> list[float]:
    if not path:
        return []
    acc = [0.0]
    for prev, cur in zip(path, path[1:]):
        acc.append(acc[-1] + haversine_m(prev, cur))
    return acc


def total_distance_m(path: list[Point]) -> float:
    return cumulative_distances_m(path)[-1] if path else 0.0


def nearest_index(path: list[Point], point: Point) -> int:
    return min(range(len(path)), key=lambda i: haversine_m(path[i], point))


def min_distance_to_path_m(path: list[Point], point: Point) -> float:
    return min(haversine_m(p, point) for p in path)


def bbox_of(path: list[Point]) -> tuple[float, float, float, float]:
    """경로의 경계 상자 (min_lat, min_lng, max_lat, max_lng)."""
    lats = [p[0] for p in path]
    lngs = [p[1] for p in path]
    return min(lats), min(lngs), max(lats), max(lngs)


def distance_to_bbox_m(bbox: tuple[float, float, float, float], point: Point) -> float:
    """점에서 경계 상자까지의 최단 거리(근사).

    등거리원통 근사라 오차가 있으므로 프리필터 용도로만 쓴다.
    상자 내부의 어떤 점보다도 크지 않은 값을 돌려주는 것이 목적이며,
    호출부에서 안전 여유를 더해 사용한다.
    """
    min_lat, min_lng, max_lat, max_lng = bbox
    lat, lng = point
    dlat = max(min_lat - lat, 0.0, lat - max_lat)
    dlng = max(min_lng - lng, 0.0, lng - max_lng)
    if dlat == 0.0 and dlng == 0.0:
        return 0.0
    m_per_deg_lat = math.pi * EARTH_RADIUS_M / 180.0
    # 경도 1도의 거리는 위도가 높을수록 짧다. 관련 위도 중 가장 높은 값을 쓰면
    # 경도 성분을 작게 잡게 되어 참 거리를 넘어설 위험이 줄어든다.
    worst_lat = max(abs(lat), abs(min_lat), abs(max_lat))
    m_per_deg_lng = m_per_deg_lat * math.cos(math.radians(worst_lat))
    return math.hypot(dlat * m_per_deg_lat, dlng * m_per_deg_lng)


def _perpendicular_distance_m(pt: Point, start: Point, end: Point) -> float:
    """선분(start-end)에서 pt까지의 수직 거리(m). 국소 평면 근사."""
    m_per_deg_lat = math.pi * EARTH_RADIUS_M / 180.0
    m_per_deg_lng = m_per_deg_lat * math.cos(math.radians(start[0]))
    x0, y0 = pt[1] * m_per_deg_lng, pt[0] * m_per_deg_lat
    x1, y1 = start[1] * m_per_deg_lng, start[0] * m_per_deg_lat
    x2, y2 = end[1] * m_per_deg_lng, end[0] * m_per_deg_lat
    dx, dy = x2 - x1, y2 - y1
    if dx == 0.0 and dy == 0.0:
        return math.hypot(x0 - x1, y0 - y1)
    t = ((x0 - x1) * dx + (y0 - y1) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(x0 - (x1 + t * dx), y0 - (y1 + t * dy))


def douglas_peucker_indices(path: list[Point], epsilon_m: float) -> list[int]:
    """Douglas-Peucker 로 남길 좌표의 인덱스를 반환.

    인덱스를 돌려주므로 고도처럼 좌표와 짝을 이루는 배열도 같이 솎아낼 수 있다.
    재귀 대신 스택을 써서 깊은 경로에서도 안전하다.
    """
    if epsilon_m <= 0 or len(path) < 3:
        return list(range(len(path)))
    keep = [False] * len(path)
    keep[0] = keep[-1] = True
    stack = [(0, len(path) - 1)]
    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        max_d, idx = -1.0, first
        for i in range(first + 1, last):
            d = _perpendicular_distance_m(path[i], path[first], path[last])
            if d > max_d:
                max_d, idx = d, i
        if max_d > epsilon_m:
            keep[idx] = True
            stack.append((first, idx))
            stack.append((idx, last))
    return [i for i, k in enumerate(keep) if k]


def douglas_peucker(path: list[Point], epsilon_m: float) -> list[Point]:
    """Douglas-Peucker 경로 단순화."""
    return [path[i] for i in douglas_peucker_indices(path, epsilon_m)]


def elevation_gain_m(elevations: list[float], threshold_m: float = 1.0) -> float:
    """누적 상승고도. threshold 미만의 잔진동은 GPS 노이즈로 보고 무시한다."""
    if not elevations:
        return 0.0
    gain, anchor = 0.0, elevations[0]
    for e in elevations[1:]:
        diff = e - anchor
        if diff >= threshold_m:
            gain += diff
            anchor = e
        elif diff <= -threshold_m:
            anchor = e
    return gain


def destination_point(origin: Point, bearing_deg: float, distance_m: float) -> Point:
    lat1, lng1 = map(math.radians, origin)
    brg = math.radians(bearing_deg)
    d = distance_m / EARTH_RADIUS_M
    lat2 = math.asin(
        math.sin(lat1) * math.cos(d) + math.cos(lat1) * math.sin(d) * math.cos(brg)
    )
    lng2 = lng1 + math.atan2(
        math.sin(brg) * math.sin(d) * math.cos(lat1),
        math.cos(d) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lng2)
