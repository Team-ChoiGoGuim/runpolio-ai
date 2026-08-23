"""API 없이 검증 가능한 순수 로직 테스트."""
from src.common.geo import destination_point, haversine_m, total_distance_m
from src.course.durunubi import extract_segment, find_nearby_courses


def _straight_path(start=(37.5300, 126.9200), n=101, step_m=100):
    path = [start]
    for _ in range(n - 1):
        path.append(destination_point(path[-1], bearing_deg=0, distance_m=step_m))
    return path


def test_haversine_known_distance():
    d = haversine_m((37.0, 127.0), (37.01, 127.0))
    assert abs(d - 1112) < 5


def test_destination_point_roundtrip():
    origin = (37.5, 127.0)
    moved = destination_point(origin, bearing_deg=45, distance_m=800)
    assert abs(haversine_m(origin, moved) - 800) < 1


def test_extract_segment_out_and_back():
    """extract_segment 는 항상 왕복(out-and-back) 구간을 반환한다. 편도 모드는 없다."""
    path = _straight_path()
    seg = extract_segment(path, center=path[0], target_m=4000)
    assert seg is not None
    assert abs(total_distance_m(seg) - 4000) <= 200
    assert haversine_m(seg[0], seg[-1]) < 1


def test_extract_segment_insufficient_length_returns_none():
    path = _straight_path(n=11)
    assert extract_segment(path, center=path[0], target_m=5000) is None


def test_extract_segment_starts_near_user_point():
    path = _straight_path()
    seg = extract_segment(path, center=path[50], target_m=2000)
    assert seg is not None
    assert haversine_m(seg[0], path[50]) < 50


def test_find_nearby_theme_filter():
    db = [
        {"course_id": "a", "theme": ["해변"], "path": [[37.53, 126.92], [37.54, 126.92]]},
        {"course_id": "b", "theme": ["강변"], "path": [[37.53, 126.92], [37.54, 126.92]]},
    ]
    hits = find_nearby_courses(center=(37.53, 126.92), radius_m=1000, theme="강변", db=db)
    assert [c["course_id"] for c in hits] == ["b"]
