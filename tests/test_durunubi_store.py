"""인덱스/bbox/lazy-load 조회 구조 테스트."""
import json

import pytest

from src.common.geo import (
    bbox_of, distance_to_bbox_m, douglas_peucker, douglas_peucker_indices,
    elevation_gain_m, haversine_m, total_distance_m,
)
from src.course import durunubi, durunubi_store


def _line(start=(37.5000, 127.0000), n=50, dlat=0.001):
    return [(start[0] + i * dlat, start[1]) for i in range(n)]


@pytest.fixture
def store_dir(tmp_path):
    """인덱스 1건 + 경로 파일 1건짜리 임시 저장소."""
    path = _line()
    min_lat, min_lng, max_lat, max_lng = bbox_of(path)
    (tmp_path / "paths").mkdir()
    (tmp_path / "paths" / "C1.json").write_text(
        json.dumps({"course_id": "C1", "path": [list(p) for p in path],
                    "elevation": [10.0] * len(path)}),
        encoding="utf-8",
    )
    (tmp_path / "index.json").write_text(
        json.dumps({
            "schema_version": 1, "course_count": 1,
            "courses": [{
                "course_id": "C1", "name": "테스트 코스", "theme": ["해변"],
                "bbox": [min_lat, min_lng, max_lat, max_lng],
                "crs_level": 2, "distance_km_measured": 5.4,
            }],
        }),
        encoding="utf-8",
    )
    durunubi_store.clear_cache()
    yield tmp_path
    durunubi_store.clear_cache()


# ── geo 유틸 ────────────────────────────────────────────────────────────
def test_bbox_of_covers_all_points():
    path = _line()
    min_lat, min_lng, max_lat, max_lng = bbox_of(path)
    assert all(min_lat <= p[0] <= max_lat and min_lng <= p[1] <= max_lng for p in path)


def test_distance_to_bbox_zero_inside():
    assert distance_to_bbox_m((37.0, 127.0, 38.0, 128.0), (37.5, 127.5)) == 0.0


def test_distance_to_bbox_approximation_stays_tight():
    """근사 오차가 프리필터 여유(5%+50m) 안에 들어와야 한다."""
    path = _line()
    bbox = bbox_of(path)
    for probe in [(37.40, 127.00), (37.60, 127.10), (37.55, 126.90), (36.9, 126.5)]:
        true_min = min(haversine_m(p, probe) for p in path)
        approx = distance_to_bbox_m(bbox, probe)
        assert approx <= true_min * 1.01 + 50


def test_bbox_prefilter_never_drops_a_true_match(store_dir):
    """프리필터의 실제 계약: 반경 안에 있는 코스는 반드시 후보로 살아남는다."""
    idx = durunubi_store.load_index(store_dir)
    path = durunubi_store.load_path("C1", store_dir)
    for probe in [(37.50, 127.0), (37.52, 127.004), (37.549, 126.995), (37.47, 127.0)]:
        for radius in (500, 1000, 3000, 5000):
            truly_near = min(haversine_m(p, probe) for p in path) <= radius
            survived = bool(
                durunubi_store.bbox_candidates(probe, radius, idx, store_dir)
            )
            if truly_near:
                assert survived, f"참 후보 탈락: {probe} r={radius}"


def test_douglas_peucker_keeps_endpoints_and_reduces():
    path = _line(n=200, dlat=0.0001)
    simplified = douglas_peucker(path, epsilon_m=5.0)
    assert simplified[0] == path[0] and simplified[-1] == path[-1]
    assert len(simplified) < len(path)


def test_douglas_peucker_preserves_length_within_tolerance():
    path = _line(n=200, dlat=0.0001)
    before = total_distance_m(path)
    after = total_distance_m(douglas_peucker(path, epsilon_m=5.0))
    assert abs(after - before) / before < 0.01


def test_douglas_peucker_indices_align_with_points():
    path = _line(n=60, dlat=0.0005)
    idx = douglas_peucker_indices(path, epsilon_m=5.0)
    assert [path[i] for i in idx] == douglas_peucker(path, epsilon_m=5.0)


def test_elevation_gain_ignores_small_noise():
    assert elevation_gain_m([10.0, 10.3, 10.1, 10.4], threshold_m=1.0) == 0.0
    assert elevation_gain_m([10.0, 20.0, 15.0, 25.0], threshold_m=1.0) == 20.0


# ── 저장소 ──────────────────────────────────────────────────────────────
def test_load_index_and_cache(store_dir):
    idx = durunubi_store.load_index(store_dir)
    assert len(idx) == 1 and idx[0]["course_id"] == "C1"
    assert durunubi_store.load_index(store_dir) is idx  # 두 번째 호출은 캐시


def test_load_index_missing_dir_returns_empty(tmp_path):
    assert durunubi_store.load_index(tmp_path / "none") == []


def test_lazy_path_loads_only_on_demand(store_dir):
    durunubi_store.load_index(store_dir)
    assert durunubi_store._path_cache == {}  # 인덱스만 읽은 시점엔 좌표 미로딩
    path = durunubi_store.load_path("C1", store_dir)
    assert len(path) == 50
    assert "C1" in durunubi_store._path_cache


def test_bbox_candidates_excludes_far_courses(store_dir):
    idx = durunubi_store.load_index(store_dir)
    assert durunubi_store.bbox_candidates((37.52, 127.0), 3000, idx, store_dir) == idx
    assert durunubi_store.bbox_candidates((35.0, 129.0), 3000, idx, store_dir) == []


# ── 조회 통합 ───────────────────────────────────────────────────────────
def test_find_nearby_uses_index_and_fills_path(store_dir):
    hits = durunubi.find_nearby_courses(
        center=(37.5100, 127.0), radius_m=2000, data_dir=store_dir,
    )
    assert len(hits) == 1
    assert hits[0]["course_id"] == "C1"
    assert len(hits[0]["path"]) == 50           # 좌표가 채워져 반환된다
    assert hits[0]["crs_level"] == 2            # 메타데이터도 함께 전달


def test_find_nearby_theme_filter_via_index(store_dir):
    assert durunubi.find_nearby_courses(
        center=(37.51, 127.0), radius_m=2000, theme="강변", data_dir=store_dir,
    ) == []
    assert len(durunubi.find_nearby_courses(
        center=(37.51, 127.0), radius_m=2000, theme="해변", data_dir=store_dir,
    )) == 1


def test_find_nearby_far_center_returns_nothing(store_dir):
    assert durunubi.find_nearby_courses(
        center=(35.1, 129.0), radius_m=3000, data_dir=store_dir,
    ) == []


def test_extract_segment_works_on_indexed_course(store_dir):
    hits = durunubi.find_nearby_courses(
        center=(37.5000, 127.0), radius_m=2000, data_dir=store_dir,
    )
    seg = durunubi.extract_segment(hits[0]["path"], (37.5000, 127.0), 2000, loop=False)
    assert seg is not None
    assert abs(total_distance_m(seg) - 2000) < 200
