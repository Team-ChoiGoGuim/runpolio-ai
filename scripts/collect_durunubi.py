"""두루누비 코스 수집 배치 (AI-CRS-002).

공공데이터포털 공개 API에서 코스 목록을 받고, 코스별 GPX를 내려받아
조회용 인덱스와 좌표 파일로 가공한다. 중단 후 재실행하면 이미 끝난 코스는
건너뛴다.

  python scripts/collect_durunubi.py              # 수집(재개)
  python scripts/collect_durunubi.py --limit 5    # 5건만 (동작 확인용)
  python scripts/collect_durunubi.py --force      # 전량 재수집
  python scripts/collect_durunubi.py --reindex    # 재다운로드 없이 인덱스만 재생성

주의: 이 API는 공개된 144개 코스만 제공한다. 원본 사이트와의 불일치는
docs/DATA_SOURCES.md 참고. 향후 전체 데이터가 열리면 CourseListSource
구현체만 교체하면 된다.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional, Protocol

import httpx
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.geo import (  # noqa: E402
    bbox_of, douglas_peucker_indices, elevation_gain_m, haversine_m, total_distance_m,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = ROOT / "data" / "durunubi"
API_BASE = "https://apis.data.go.kr/B551011/Durunubi"

# 코스 설명 텍스트에서 테마를 추정하는 키워드 사전.
# 두루누비는 테마 필드를 주지 않으므로 여기서 파생시킨다(휴리스틱).
THEME_KEYWORDS = {
    "해변": ["해변", "해수욕장", "바다", "해안", "해상", "포구", "항구", "등대", "섬"],
    "강변": ["강변", "강길", "천변", "하천", "River", "수변", "나루"],
    "공원": ["공원", "수목원", "생태공원", "둔치", "산책로"],
    "호수": ["호수", "저수지", "호반", "댐"],
    "도심": ["도심", "시가지", "시장", "downtown", "거리", "골목"],
}


# ── 수집 소스 (향후 교체 지점) ──────────────────────────────────────────
class CourseListSource(Protocol):
    """코스 목록 공급자. 전체 데이터가 열리면 이 인터페이스만 새로 구현한다."""

    name: str

    def fetch(self) -> list[dict]:
        """원본 메타데이터 dict 리스트를 반환. gpxpath 키 필수."""


class DataGoKrDurunubi:
    """공공데이터포털 한국관광공사 두루누비 정보 서비스 (courseList)."""

    name = "data.go.kr/B551011/Durunubi"

    def __init__(self, service_key: str, timeout: float = 30.0):
        self.service_key = service_key
        self.timeout = timeout

    def _get(self, op: str, **extra) -> dict:
        params = {
            "serviceKey": self.service_key,
            "MobileOS": "ETC",
            "MobileApp": "runpolio",
            "_type": "json",
            "numOfRows": "500",
            "pageNo": "1",
            **extra,
        }
        r = httpx.get(f"{API_BASE}/{op}", params=params, timeout=self.timeout)
        r.raise_for_status()
        payload = r.json()
        header = payload.get("response", {}).get("header", {})
        if header.get("resultCode") not in ("0000", "00"):
            raise RuntimeError(f"{op} 실패: {header}")
        return payload["response"]["body"]

    def fetch(self) -> list[dict]:
        body = self._get("courseList")
        items = (body.get("items") or {}).get("item") or []
        items = items if isinstance(items, list) else [items]
        total = body.get("totalCount")
        if total is not None and len(items) != int(total):
            print(f"  ! totalCount={total} 인데 {len(items)}건 수신 — 페이지 추가 조회")
            items = self._fetch_paged(int(total))
        return items

    def _fetch_paged(self, total: int) -> list[dict]:
        seen, page = {}, 1
        while len(seen) < total and page < 50:
            body = self._get("courseList", numOfRows="100", pageNo=str(page))
            items = (body.get("items") or {}).get("item") or []
            items = items if isinstance(items, list) else [items]
            if not items:
                break
            for it in items:
                seen[it["crsIdx"]] = it
            page += 1
        return list(seen.values())

    def fetch_routes(self) -> list[dict]:
        body = self._get("routeList")
        items = (body.get("items") or {}).get("item") or []
        return items if isinstance(items, list) else [items]


# ── GPX 파싱 ────────────────────────────────────────────────────────────
Segment = tuple[list[tuple[float, float]], list[float]]

# 한 코스의 GPX에 트랙이 여러 개 담긴 경우가 있다(본선 + 우회로 등).
# 이어붙이면 트랙 사이에 수 km 짜리 가짜 직선이 생기므로 반드시 분리한다.
_SPLIT_GAP_M = 1000.0


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _read_points(container) -> Segment:
    coords, eles = [], []
    for el in container:
        if _local(el.tag) not in ("trkpt", "rtept", "wpt"):
            continue
        try:
            coords.append((float(el.attrib["lat"]), float(el.attrib["lon"])))
        except (KeyError, ValueError):
            continue
        ele = next((c for c in el if _local(c.tag) == "ele"), None)
        try:
            eles.append(float(ele.text) if ele is not None and ele.text else 0.0)
        except (ValueError, TypeError):
            eles.append(0.0)
    return coords, eles


def _split_on_gaps(seg: Segment) -> list[Segment]:
    """세그먼트 안에 끊긴 구간이 있으면 나눈다."""
    coords, eles = seg
    if len(coords) < 2:
        return [seg]
    out, start = [], 0
    for i in range(1, len(coords)):
        if haversine_m(coords[i - 1], coords[i]) > _SPLIT_GAP_M:
            out.append((coords[start:i], eles[start:i]))
            start = i
    out.append((coords[start:], eles[start:]))
    return [s for s in out if len(s[0]) >= 2]


def parse_gpx_segments(xml_text: str) -> list[Segment]:
    """GPX를 연속된 궤적 단위로 끊어서 반환한다."""
    root = ET.fromstring(xml_text)
    containers = [el for el in root.iter() if _local(el.tag) == "trkseg"]
    if not containers:
        containers = [el for el in root.iter() if _local(el.tag) == "trk"]
    if not containers:
        containers = [root]

    segments: list[Segment] = []
    for c in containers:
        pts = _read_points(list(c.iter()))
        if len(pts[0]) >= 2:
            segments.extend(_split_on_gaps(pts))
    return segments


def choose_segment(segments: list[Segment], expected_km: float) -> tuple[Segment, int]:
    """제공 거리에 가장 가까운 궤적을 고른다. 없으면 가장 긴 것."""
    if not segments:
        return ([], []), -1
    lengths = [total_distance_m(s[0]) / 1000.0 for s in segments]
    if expected_km > 0:
        best = min(range(len(segments)), key=lambda i: abs(lengths[i] - expected_km))
    else:
        best = max(range(len(segments)), key=lambda i: lengths[i])
    return segments[best], best


def infer_themes(course: dict) -> list[str]:
    """설명 텍스트에서 테마를 추정한다(휴리스틱). POI 기반 태깅 전까지의 임시 수단."""
    blob = " ".join(
        str(course.get(k) or "")
        for k in ("crsKorNm", "crsSummary", "crsContents", "crsTourInfo", "sigun")
    )
    return [t for t, kws in THEME_KEYWORDS.items() if any(k in blob for k in kws)]


def _to_int(value, default=0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _to_float(value, default=0.0) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _strip_html(text: Optional[str]) -> str:
    if not text:
        return ""
    return re.sub(r"<[^>]+>", " ", text).replace("\r", " ").strip()


# ── 상태 관리 ───────────────────────────────────────────────────────────
def load_state(data_dir: Path) -> dict:
    f = data_dir / "state.json"
    if f.exists():
        with open(f, encoding="utf-8") as fh:
            return json.load(fh)
    return {"completed": {}, "failed": {}}


def save_json_atomic(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


# ── 코스 1건 처리 ───────────────────────────────────────────────────────
def process_course(
    raw: dict, data_dir: Path, epsilon_m: float, client: httpx.Client,
) -> dict:
    """GPX를 받아 좌표 파일을 쓰고 인덱스 레코드를 만든다."""
    cid = raw["crsIdx"]
    gpx_url = (raw.get("gpxpath") or "").strip()
    if not gpx_url:
        raise ValueError("gpxpath 없음")

    resp = client.get(gpx_url, timeout=60.0, follow_redirects=True)
    resp.raise_for_status()
    segments = parse_gpx_segments(resp.text)
    expected_km = _to_float(raw.get("crsDstnc"))
    (coords, eles), seg_i = choose_segment(segments, expected_km)
    if len(coords) < 2:
        raise ValueError(f"GPX 좌표 부족 ({len(coords)}점)")

    raw_n = len(coords)
    keep_idx = douglas_peucker_indices(coords, epsilon_m)
    simplified = [coords[i] for i in keep_idx]
    # 좌표와 같은 인덱스로 고도도 함께 솎아낸다.
    ele_out = [eles[i] for i in keep_idx] if len(eles) == raw_n else []

    save_json_atomic(
        data_dir / "paths" / f"{cid}.json",
        {
            "course_id": cid,
            "path": [[round(a, 6), round(b, 6)] for a, b in simplified],
            "elevation": [round(e, 1) for e in ele_out],
        },
    )

    min_lat, min_lng, max_lat, max_lng = bbox_of(simplified)
    measured_km = total_distance_m(simplified) / 1000.0

    return {
        "course_id": cid,
        "route_id": raw.get("routeIdx"),
        "name": raw.get("crsKorNm"),
        "bbox": [round(min_lat, 6), round(min_lng, 6), round(max_lat, 6), round(max_lng, 6)],
        "theme": infer_themes(raw),
        "sigun": raw.get("sigun"),
        "crs_level": _to_int(raw.get("crsLevel"), 0),
        "distance_km_source": _to_float(raw.get("crsDstnc")),
        "distance_km_measured": round(measured_km, 3),
        "duration_min_source": _to_int(raw.get("crsTotlRqrmHour")),
        "cycle": raw.get("crsCycle"),
        "brd_div": raw.get("brdDiv"),
        "elevation_gain_m": round(elevation_gain_m(ele_out), 1),
        "n_points": len(simplified),
        "n_points_raw": raw_n,
        "n_segments": len(segments),
        "segment_index": seg_i,
        "summary": _strip_html(raw.get("crsSummary")),
        "contents": _strip_html(raw.get("crsContents")),
        "tour_info": _strip_html(raw.get("crsTourInfo")),
        "traveler_info": _strip_html(raw.get("travelerinfo")),
        "gpx_url": gpx_url,
        "created_at": raw.get("createdtime"),
        "modified_at": raw.get("modifiedtime"),
    }


def build_index(records: Iterable[dict], source_name: str, raw_total: int) -> dict:
    rows = sorted(records, key=lambda r: r["course_id"])
    return {
        "schema_version": 1,
        "source": source_name,
        # RFC 3339(OpenAPI date-time)를 만족하도록 UTC 오프셋을 붙인다.
        "collected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_total_count": raw_total,
        "course_count": len(rows),
        "courses": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="두루누비 코스 수집 배치")
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    ap.add_argument("--limit", type=int, default=0, help="처리할 코스 수 상한 (0=전체)")
    ap.add_argument("--force", action="store_true", help="완료분도 다시 수집")
    ap.add_argument("--reindex", action="store_true", help="재다운로드 없이 인덱스만 재생성")
    ap.add_argument("--epsilon", type=float, default=5.0, help="경로 단순화 허용오차(m), 0=원본유지")
    ap.add_argument("--sleep", type=float, default=0.2, help="GPX 요청 간 대기(초)")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    key = os.getenv("DATA_GO_KR_KEY")
    if not key:
        print("ERROR: .env 의 DATA_GO_KR_KEY 가 비어 있습니다.", file=sys.stderr)
        return 2

    data_dir: Path = args.data_dir
    (data_dir / "paths").mkdir(parents=True, exist_ok=True)
    (data_dir / "raw").mkdir(parents=True, exist_ok=True)

    state = load_state(data_dir)
    completed: dict = {} if args.force else state.get("completed", {})
    failed: dict = {}

    source = DataGoKrDurunubi(key)

    if args.reindex:
        raw_file = data_dir / "raw" / "courseList.json"
        if not raw_file.exists():
            print("ERROR: --reindex 는 이전 수집 결과가 필요합니다.", file=sys.stderr)
            return 2
        with open(raw_file, encoding="utf-8") as fh:
            items = json.load(fh)
        print(f"원본 {len(items)}건으로 인덱스만 재생성합니다.")
    else:
        print(f"[1/3] 코스 목록 조회 — {source.name}")
        items = source.fetch()
        save_json_atomic(data_dir / "raw" / "courseList.json", items)
        try:
            save_json_atomic(data_dir / "raw" / "routeList.json", source.fetch_routes())
        except Exception as e:  # 노선 목록은 부가정보라 실패해도 진행
            print(f"  ! routeList 조회 실패(무시): {e}")
        print(f"  수신 {len(items)}건 (원본 보존: raw/courseList.json)")

    if args.limit:
        items = items[: args.limit]

    print(f"[2/3] GPX 수집 및 가공 (epsilon={args.epsilon}m)")
    records: list[dict] = []
    t0 = time.time()
    with httpx.Client(headers={"User-Agent": "runpolio-collector/1.0"}) as client:
        for i, raw in enumerate(items, 1):
            cid = raw.get("crsIdx")
            name = raw.get("crsKorNm", "?")
            prev = completed.get(cid)
            path_file = data_dir / "paths" / f"{cid}.json"
            fresh = (
                prev
                and prev.get("modified_at") == raw.get("modifiedtime")
                and path_file.exists()
            )
            if fresh and not args.force:
                records.append(prev["record"])
                print(f"  [{i:>3}/{len(items)}] skip  {name}")
                continue
            try:
                rec = process_course(raw, data_dir, args.epsilon, client)
                records.append(rec)
                completed[cid] = {"modified_at": raw.get("modifiedtime"), "record": rec}
                print(
                    f"  [{i:>3}/{len(items)}] ok    {name}  "
                    f"{rec['n_points_raw']}→{rec['n_points']}점  "
                    f"{rec['distance_km_measured']}km"
                )
            except Exception as e:
                failed[cid] = f"{type(e).__name__}: {e}"
                print(f"  [{i:>3}/{len(items)}] FAIL  {name} — {e}")
            # 중단되더라도 진행분이 남도록 매 건 상태를 저장한다.
            save_json_atomic(
                data_dir / "state.json", {"completed": completed, "failed": failed}
            )
            if args.sleep:
                time.sleep(args.sleep)

    print(f"[3/3] 인덱스 생성")
    index = build_index(records, source.name, len(items))
    save_json_atomic(data_dir / "index.json", index)

    elapsed = time.time() - t0
    print()
    print(f"완료: {len(records)}건 성공 / {len(failed)}건 실패 / {elapsed:.1f}초")
    print(f"  인덱스 : {data_dir / 'index.json'}")
    print(f"  경로   : {data_dir / 'paths'} ({len(records)}개 파일)")
    if failed:
        print("  실패 목록:")
        for cid, msg in failed.items():
            print(f"    {cid}: {msg}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
