"""수집 결과 검증 — 조회 성능(신규 vs 전수 스캔)과 매칭 정확도를 비교한다.

    python scripts/verify_durunubi.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.geo import haversine_m, min_distance_to_path_m, total_distance_m  # noqa: E402
from src.course import durunubi, durunubi_store  # noqa: E402

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "durunubi"
RADIUS_M = 3000.0

# 대표 위치 — 노선별 도심/관문 지점과, 매칭이 없어야 하는 내륙 대도시
PROBES = [
    ("부산 해운대해수욕장", (35.1587, 129.1604)),
    ("강릉 경포해변",       (37.7952, 128.9106)),
    ("목포 유달산",         (34.7869, 126.3792)),
    ("고성 통일전망대",      (38.5730, 128.3620)),
    ("서울 시청",           (37.5663, 126.9779)),
    ("대전 정부청사",        (36.3620, 127.3845)),
]


def load_all_paths() -> dict[str, list]:
    """전수 스캔 기준선: 모든 좌표 파일을 통째로 읽는다(구 방식 모사)."""
    out = {}
    for f in sorted((DATA_DIR / "paths").glob("*.json")):
        with open(f, encoding="utf-8") as fh:
            payload = json.load(fh)
        out[payload["course_id"]] = [(a, b) for a, b in payload["path"]]
    return out


def baseline_scan(center, radius_m, index, all_paths) -> list[str]:
    hits = []
    for row in index:
        path = all_paths.get(row["course_id"])
        if not path:
            continue
        d = min_distance_to_path_m(path, center)
        if d <= radius_m:
            hits.append((d, row["course_id"]))
    hits.sort()
    return [c for _, c in hits]


def main() -> int:
    if not durunubi_store.index_path(DATA_DIR).exists():
        print("인덱스가 없습니다. scripts/collect_durunubi.py 를 먼저 실행하세요.")
        return 2

    print("=" * 72)
    print("1) 저장 구조")
    print("=" * 72)
    with open(DATA_DIR / "index.json", encoding="utf-8") as fh:
        meta = json.load(fh)
    idx_kb = durunubi_store.index_path(DATA_DIR).stat().st_size / 1024
    path_files = list((DATA_DIR / "paths").glob("*.json"))
    paths_mb = sum(f.stat().st_size for f in path_files) / 1024 / 1024
    print(f"  수집 코스      : {meta['course_count']}건 (source_total={meta['source_total_count']})")
    print(f"  index.json     : {idx_kb:,.0f} KB  ← 메모리 상주")
    print(f"  paths/*.json   : {len(path_files)}개, 합계 {paths_mb:.2f} MB  ← lazy load")
    print(f"  좌표 총계      : {sum(c['n_points_raw'] for c in meta['courses']):,} → "
          f"{sum(c['n_points'] for c in meta['courses']):,}점")

    print()
    print("=" * 72)
    print("2) 전수 스캔 기준선 준비")
    print("=" * 72)
    t = time.perf_counter()
    all_paths = load_all_paths()
    load_ms = (time.perf_counter() - t) * 1000
    print(f"  전 좌표 파일 로드: {load_ms:,.0f} ms  (구 방식은 요청마다 이 비용을 냄)")

    durunubi_store.clear_cache()
    t = time.perf_counter()
    index = durunubi_store.load_index(DATA_DIR)
    idx_ms = (time.perf_counter() - t) * 1000
    print(f"  인덱스 로드      : {idx_ms:,.1f} ms  (1회, 이후 캐시)")

    print()
    print("=" * 72)
    print("3) 조회 성능 및 결과 일치 (반경 3km, 테마 미지정)")
    print("=" * 72)
    print(f"  {'위치':<18} {'후보(bbox)':>11} {'매칭':>5} {'신규(ms)':>10} {'전수(ms)':>10} {'배속':>7} 일치")
    print("  " + "-" * 74)
    all_match = True
    speedups = []
    for label, center in PROBES:
        n_cand = len(durunubi_store.bbox_candidates(center, RADIUS_M, index, DATA_DIR))

        t = time.perf_counter()
        for _ in range(5):
            new_hits = durunubi.find_nearby_courses(
                center=center, radius_m=RADIUS_M, data_dir=DATA_DIR,
            )
        new_ms = (time.perf_counter() - t) * 1000 / 5

        t = time.perf_counter()
        for _ in range(5):
            base_ids = baseline_scan(center, RADIUS_M, index, all_paths)
        base_ms = (time.perf_counter() - t) * 1000 / 5

        new_ids = [c["course_id"] for c in new_hits]
        same = new_ids == base_ids
        all_match &= same
        speedups.append(base_ms / new_ms if new_ms else 0)
        print(f"  {label:<18} {n_cand:>7}/{len(index)} {len(new_ids):>5} "
              f"{new_ms:>10.2f} {base_ms:>10.2f} {base_ms/new_ms:>6.0f}x "
              f"{'✅' if same else '❌'}")
    print()
    print(f"  결과 일치: {'전부 동일 — 프리필터가 매칭을 바꾸지 않음' if all_match else '불일치 발생!'}")
    print(f"  평균 속도향상: {sum(speedups)/len(speedups):.0f}배 "
          f"(전수 스캔은 별도로 매 요청 {load_ms:,.0f} ms 파싱 비용이 추가됨)")

    print()
    print("=" * 72)
    print("4) 대표 매칭 결과 (반경 3km 최근접 코스 + 5km 구간 추출)")
    print("=" * 72)
    for label, center in PROBES:
        hits = durunubi.find_nearby_courses(
            center=center, radius_m=RADIUS_M, data_dir=DATA_DIR,
        )
        if not hits:
            print(f"\n  {label}: 반경 {RADIUS_M/1000:.0f}km 내 코스 없음 → TMAP 폴백")
            continue
        top = hits[0]
        d = min_distance_to_path_m(top["path"], center)
        print(f"\n  {label}  (후보 {len(hits)}건)")
        print(f"    최근접 : {top['name']}  ({d:,.0f}m 떨어짐)")
        print(f"    메타   : {top['distance_km_measured']}km / 난이도 {top['crs_level']} / "
              f"고도상승 {top['elevation_gain_m']}m / {top['sigun']}")
        print(f"    테마   : {top['theme']}")
        seg = durunubi.extract_segment(top["path"], center, 5000, loop=True)
        if seg:
            print(f"    5km 왕복 구간 추출: {len(seg)}점, 실측 {total_distance_m(seg)/1000:.2f}km, "
                  f"시종점 간격 {haversine_m(seg[0], seg[-1]):.1f}m")
        else:
            print(f"    5km 구간 추출 실패 (코스 길이 부족)")

    print()
    print("=" * 72)
    print("5) 다운샘플링 오차 (원본 GPX 재다운로드 후 직접 비교)")
    print("=" * 72)
    import httpx
    from scripts.collect_durunubi import choose_segment, parse_gpx_segments
    from src.common.geo import douglas_peucker

    sample = meta["courses"][:: max(1, len(meta["courses"]) // 6)][:6]
    errs = []
    with httpx.Client(headers={"User-Agent": "runpolio-verify/1.0"}) as client:
        for c in sample:
            r = client.get(c["gpx_url"], timeout=60.0, follow_redirects=True)
            (coords, _), _ = choose_segment(
                parse_gpx_segments(r.text), c["distance_km_source"]
            )
            full_km = total_distance_m(coords) / 1000.0
            simp_km = total_distance_m(douglas_peucker(coords, 5.0)) / 1000.0
            err = abs(simp_km - full_km) / full_km * 100 if full_km else 0.0
            errs.append(err)
            print(f"  {c['name']:<30} 원본 {full_km:7.3f}km → 단순화 {simp_km:7.3f}km  "
                  f"오차 {err:.3f}%  ({len(coords)}→{c['n_points']}점)")
    print(f"\n  다운샘플링 거리 오차: 평균 {sum(errs)/len(errs):.3f}% / 최대 {max(errs):.3f}%")

    print()
    print("=" * 72)
    print("6) 데이터 품질 — 두루누비 제공 거리 vs GPX 실측")
    print("=" * 72)
    diffs = sorted(
        (abs(c["distance_km_measured"] - c["distance_km_source"]) / c["distance_km_source"] * 100,
         c["name"], c["distance_km_source"], c["distance_km_measured"])
        for c in meta["courses"] if c["distance_km_source"] > 0
    )
    pct = [d for d, *_ in diffs]
    print(f"  편차: 중앙값 {pct[len(pct)//2]:.1f}% / 평균 {sum(pct)/len(pct):.1f}% / 최대 {pct[-1]:.1f}%")
    print(f"  10% 이내 {sum(1 for d in pct if d <= 10)}건 / "
          f"10~50% {sum(1 for d in pct if 10 < d <= 50)}건 / "
          f"50% 초과 {sum(1 for d in pct if d > 50)}건")
    multi = [c for c in meta["courses"] if c.get("n_segments", 1) > 1]
    print(f"  GPX에 트랙이 2개 이상인 코스: {len(multi)}건 (본선만 선택함)")
    if pct[-1] > 30:
        print("  편차 상위 3건:")
        for d, name, s, m in diffs[-3:][::-1]:
            print(f"    {d:6.1f}%  {name:<28} 제공 {s}km / 실측 {m}km")
    return 0 if all_match else 1


if __name__ == "__main__":
    raise SystemExit(main())
