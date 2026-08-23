"""두루누비 코스 저장소 — 경량 인덱스 + 좌표 경로 lazy load.

저장 구조 (scripts/collect_durunubi.py 가 생성):

    data/durunubi/
      index.json          # 코스 메타데이터 + bbox (전량 메모리 상주)
      paths/{crsIdx}.json # 좌표 경로 + 고도 (후보로 걸린 코스만 읽음)
      raw/                # 원본 API 응답 (재가공용 보존)
      state.json          # 수집 재개 상태

인덱스만 상주시키고 경로는 필요할 때 읽는 이유:
전 코스의 좌표를 요청마다 파싱하면 응답 시간 NFR(AI-CRS-008, 5초)을 잠식한다.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Optional

from src.common.geo import Point, distance_to_bbox_m

_DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "durunubi"

# bbox 프리필터는 등거리원통 근사라 오차가 있다. 참 후보를 떨어뜨리지 않도록
# 반경에 여유를 얹어 1차로 넓게 거른 뒤, 정밀 계산으로 다시 좁힌다.
_BBOX_MARGIN_RATIO = 0.05
_BBOX_MARGIN_M = 50.0

_index_cache: Optional[list[dict]] = None
_index_cache_key: Optional[tuple] = None
_path_cache: dict[str, list[Point]] = {}


def index_path(data_dir: Path = _DATA_DIR) -> Path:
    return data_dir / "index.json"


def paths_dir(data_dir: Path = _DATA_DIR) -> Path:
    return data_dir / "paths"


def load_index(data_dir: Path = _DATA_DIR, force: bool = False) -> list[dict]:
    """인덱스를 읽어 메모리에 캐시한다. 파일 mtime이 바뀌면 자동 갱신."""
    global _index_cache, _index_cache_key
    f = index_path(data_dir)
    if not f.exists():
        return []
    key = (str(f), f.stat().st_mtime_ns, f.stat().st_size)
    if not force and _index_cache is not None and _index_cache_key == key:
        return _index_cache
    with open(f, encoding="utf-8") as fh:
        payload = json.load(fh)
    _index_cache = payload.get("courses", [])
    _index_cache_key = key
    return _index_cache


def load_meta(data_dir: Path = _DATA_DIR) -> dict:
    """인덱스 파일의 부가 정보(수집 출처·시각·코스 수)를 읽는다.

    `load_index()` 는 조회 성능을 위해 `courses` 배열만 돌려주므로,
    `/health` 같은 상태 조회용으로 나머지 메타데이터가 필요할 때 이걸 쓴다.
    """
    f = index_path(data_dir)
    if not f.exists():
        return {}
    with open(f, encoding="utf-8") as fh:
        payload = json.load(fh)
    return {
        "source": payload.get("source"),
        "collected_at": payload.get("collected_at"),
        "course_count": payload.get("course_count", 0),
        "source_total_count": payload.get("source_total_count"),
    }


def clear_cache() -> None:
    global _index_cache, _index_cache_key
    _index_cache = None
    _index_cache_key = None
    _path_cache.clear()


def load_path(course_id: str, data_dir: Path = _DATA_DIR) -> list[Point]:
    """코스 좌표 경로를 읽는다 (프로세스 수명 동안 캐시)."""
    if course_id in _path_cache:
        return _path_cache[course_id]
    f = paths_dir(data_dir) / f"{course_id}.json"
    if not f.exists():
        return []
    with open(f, encoding="utf-8") as fh:
        payload = json.load(fh)
    path = [(float(a), float(b)) for a, b in payload.get("path", [])]
    _path_cache[course_id] = path
    return path


def load_elevations(course_id: str, data_dir: Path = _DATA_DIR) -> list[float]:
    f = paths_dir(data_dir) / f"{course_id}.json"
    if not f.exists():
        return []
    with open(f, encoding="utf-8") as fh:
        return [float(e) for e in json.load(fh).get("elevation", [])]


def bbox_candidates(
    center: Point, radius_m: float, index: Optional[Iterable[dict]] = None,
    data_dir: Path = _DATA_DIR,
) -> list[dict]:
    """bbox 프리필터 — 좌표 파일을 열지 않고 후보를 좁힌다."""
    rows = index if index is not None else load_index(data_dir)
    cutoff = radius_m * (1.0 + _BBOX_MARGIN_RATIO) + _BBOX_MARGIN_M
    out = []
    for row in rows:
        bb = row.get("bbox")
        if not bb or len(bb) != 4:
            out.append(row)  # bbox 없는 레코드는 보수적으로 통과시킨다
            continue
        if distance_to_bbox_m((bb[0], bb[1], bb[2], bb[3]), center) <= cutoff:
            out.append(row)
    return out
