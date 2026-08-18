"""두루누비 코스 매칭 및 구간 추출.

조회 경로:
  ① 경량 인덱스(bbox+메타)를 메모리에서 훑어 후보를 좁히고
  ② 살아남은 소수의 코스만 좌표 파일을 열어 정밀 거리를 잰다.

전 코스의 좌표를 매 요청마다 읽지 않는 것이 핵심이다. 인덱스가 없으면
레거시 단일 파일(data/durunubi_courses.json)로 자동 폴백한다.
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Optional

from src.common.geo import (
    Point, cumulative_distances_m, min_distance_to_path_m, nearest_index,
)
from src.course import durunubi_store

_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "durunubi_courses.json"


def load_course_db(db_path: Path = _DB_PATH) -> list[dict]:
    """레거시 단일 파일 로더. 인덱스 구조 도입 전 형식과의 호환용."""
    if not db_path.exists():
        return []
    with open(db_path, encoding="utf-8") as f:
        return json.load(f)


def _theme_ok(course: dict, theme: Optional[str], require: bool) -> bool:
    if not theme or not require:
        return True
    return theme in (course.get("theme") or [])


def _scan_inline(
    courses: list[dict], center: Point, radius_m: float,
    theme: Optional[str], require_theme_match: bool,
) -> list[dict]:
    """좌표가 이미 실려 있는 코스 목록을 그대로 훑는다."""
    hits = []
    for c in courses:
        if not _theme_ok(c, theme, require_theme_match):
            continue
        path = [tuple(p) for p in c["path"]]
        if not path:
            continue
        d = min_distance_to_path_m(path, center)
        if d <= radius_m:
            hits.append((d, c))
    hits.sort(key=lambda x: x[0])
    return [c for _, c in hits]


def find_nearby_courses(
    center: Point, radius_m: float, theme: Optional[str] = None,
    require_theme_match: bool = True, db: Optional[list[dict]] = None,
    data_dir: Optional[Path] = None,
) -> list[dict]:
    """반경 내 두루누비 코스를 가까운 순으로 반환한다.

    db를 넘기면 그 목록만 사용한다(테스트·레거시 경로). 넘기지 않으면
    인덱스 → bbox 프리필터 → 좌표 lazy load 순으로 조회한다.
    반환되는 각 코스에는 좌표가 "path" 키로 채워져 있다.
    """
    if db is not None:
        return _scan_inline(db, center, radius_m, theme, require_theme_match)

    kwargs = {"data_dir": data_dir} if data_dir is not None else {}
    index = durunubi_store.load_index(**kwargs)
    if not index:
        legacy = load_course_db()
        if legacy:
            return _scan_inline(legacy, center, radius_m, theme, require_theme_match)
        return []

    # ① 테마 → ② bbox 로 후보를 줄인 뒤에야 좌표를 읽는다.
    rows = [c for c in index if _theme_ok(c, theme, require_theme_match)]
    rows = durunubi_store.bbox_candidates(center, radius_m, index=rows, **kwargs)

    hits = []
    for row in rows:
        cid = row.get("course_id")
        if not cid:
            continue
        path = durunubi_store.load_path(cid, **kwargs)
        if not path:
            continue
        d = min_distance_to_path_m(path, center)
        if d <= radius_m:
            hits.append((d, dict(row, path=path)))
    hits.sort(key=lambda x: x[0])
    return [c for _, c in hits]


def extract_segment(
    path: list[Point], center: Point, target_m: float, loop: bool,
) -> Optional[list[Point]]:
    if len(path) < 2:
        return None
    start_idx = nearest_index(path, center)
    one_way_m = target_m / 2 if loop else target_m

    forward = _walk(path, start_idx, one_way_m, direction=+1)
    backward = _walk(path, start_idx, one_way_m, direction=-1)
    segment = forward if forward is not None else backward
    if segment is None:
        return None

    if loop:
        return segment + segment[-2::-1]
    return segment


def _walk(path, start_idx, distance_m, direction):
    ordered = path[start_idx:] if direction > 0 else path[: start_idx + 1][::-1]
    acc = cumulative_distances_m(ordered)
    if acc[-1] < distance_m:
        return None
    for i, d in enumerate(acc):
        if d < distance_m:
            continue
        if i == 0:
            return [ordered[0]]
        # 마지막 한 변 위에서 목표 거리에 해당하는 점을 보간한다.
        # 단순화된 경로는 변 하나가 수백 m에 달할 수 있어, 정점 단위로
        # 끊으면 요청 거리를 크게 넘길 수 있다.
        edge = d - acc[i - 1]
        if edge <= 0:
            return ordered[: i + 1]
        t = (distance_m - acc[i - 1]) / edge
        p0, p1 = ordered[i - 1], ordered[i]
        cut = (p0[0] + (p1[0] - p0[0]) * t, p0[1] + (p1[1] - p0[1]) * t)
        return ordered[:i] + [cut]
    return ordered
