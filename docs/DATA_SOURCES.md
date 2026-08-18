# 데이터 소스 — 두루누비 (AI-CRS-002)

코스 후보 데이터의 출처, 알려진 제약, 수집·조회 구조를 기록한다.
**신규 합류자는 아래 "⚠️ 제약 사항"을 먼저 읽을 것.**

## 출처

| 항목 | 값 |
|---|---|
| 서비스 | 한국관광공사_두루누비 정보 서비스_GW |
| 엔드포인트 | `https://apis.data.go.kr/B551011/Durunubi/{courseList,routeList}` |
| 인증 | `.env` 의 `DATA_GO_KR_KEY` (공공데이터포털 Decoding 키를 `params=` 로 전달) |
| 호출 제한 | 개발계정 **1,000건/일** (응답 헤더 `x-ratelimit-limit` 로 확인 가능) |
| 데이터 갱신주기 | 일 1회 (매뉴얼 v4.1 기준) |
| GPX 파일 | `www.durunubi.kr` 정적 서버에서 제공 — **API 쿼터를 소비하지 않음** |
| 라이선스 | 공공누리(KOGL) — 공공데이터포털 배포 조건에 따름 |

전량 수집에 필요한 게이트웨이 호출은 **2회**(courseList + routeList)뿐이라 쿼터는 사실상 제약이 아니다.

## ⚠️ 제약 사항

### 1. 공개 API는 144개만 제공한다 (설명은 284개)

공공데이터포털 서비스 설명은 "코리아둘레길의 **284개 코스** 상세 GPX 정보"라고 명시하지만,
실제 `courseList` 응답은 **144건**(약 51%)에서 고정된다.

확인한 내용 (2026-08-12 기준):

- `numOfRows`/`pageNo` 를 어떻게 조합해도 `totalCount=144`
- `routeIdx` 별 합계도 144 (남파랑 31 / 서해랑 44 / DMZ 30 / 해파랑 39)
- `crsKorNm`, `crsLevel` 필터는 정상 동작하나 **모두 144라는 동일 모집단 위에서만** 작동
  (`crsLevel` 1·2·3 합계 = 정확히 144)
- `courseList` 외 코스 조회 오퍼레이션은 존재하지 않음 (매뉴얼상 오퍼레이션은 2개뿐)

### 2. 원본 사이트와 불일치한다

두루누비 사이트(`durunubi.kr`)에는 **297개 코스가 GPX와 함께 존재**한다
(해파랑 50 / 남파랑 90 / 서해랑 109 / DMZ 48, DMZ는 우회로 포함).

누락분이 "데이터 없음"이 아니라는 근거:

- 해파랑길 3코스(`T_CRS_MNG0000004228`), 5코스(`T_CRS_MNG0000004190`)는
  사이트에 GPX까지 갖춘 채 존재하나 API에서는 `crsKorNm` 검색으로도 조회되지 않는다
- 누락 패턴에 규칙성이 없다 (연속 구간 절단도, ID 범위 절단도 아님)

### 3. 매뉴얼과 실제 서비스가 어긋나 있다

`TourAPI_Guide_(두루누비)v4.1` 기준:

- 응답 명세의 `totalCount` 샘플데이터가 **740** — 원래 훨씬 큰 규모로 설계된 서비스
- 매뉴얼 공식 예제가 **자전거길**(밀양강 자전거길, `brdDiv=DNBW`)인데,
  현재 이 데이터는 코스·노선 모두 조회되지 않는다 (`routeList&brdDiv=DNBW` → 0건)
- **`brdDiv` 파라미터가 동작하지 않는다** — `DNBW`(자전거길)를 요청해도
  걷기길 144건이 그대로 반환된다
- 매뉴얼 `사용 제약 사항(비고)` 은 `N/A` — 데이터 범위를 제한한다는 문서상 근거 없음

→ 의도된 제한이 아니라 **제공자 측 데이터 동기화 문제**로 판단된다.
문의 초안: [`docs/inquiry_tourapi.md`](inquiry_tourapi.md)

### 4. 문서화되지 않은 내부 API는 사용하지 않는다

조사 과정에서 `durunubi.kr` 의 내부 엔드포인트로 297건 전량을 받을 수 있음을 확인했으나,
**이용약관·라이선스 적용 범위가 불명확하고 사전 공지 없이 변경될 수 있어 채택하지 않았다.**
본 구현은 공공데이터포털 공개 API만 사용한다.

### 5. 수도권 커버리지가 사실상 없다

시도별 분포: 강원 35 · 전남 29 · 충남 17 · 경북 17 · 경기 15 · 경남 14 ·
전북 6 · 부산 4 · 울산 4 · 인천 2 · **서울 1**

코리아둘레길은 해안선·DMZ 노선이라 내륙 도심 구간이 없다.
**서울·대전 등에서는 두루누비 매칭이 잡히지 않고 TMAP 폴백으로 동작한다.**
이는 284개가 전부 열려도 해결되지 않는 구조적 한계다.

## 수집

```bash
python scripts/collect_durunubi.py            # 수집 (중단 지점부터 재개)
python scripts/collect_durunubi.py --limit 5  # 5건만, 동작 확인용
python scripts/collect_durunubi.py --force    # 전량 재수집
python scripts/collect_durunubi.py --reindex  # 재다운로드 없이 인덱스만 재생성
```

`--epsilon` 으로 경로 단순화 허용오차를 조절한다(기본 5m, `0` 이면 원본 유지).
매 코스 처리 후 `state.json` 을 갱신하므로 중단해도 진행분이 남는다.
`modifiedtime` 이 그대로면 재실행 시 건너뛴다.

### 생성물

```
data/durunubi/
  index.json          # 코스 메타 + bbox (약 288 KB, 메모리 상주)
  paths/{crsIdx}.json # 좌표 + 고도 (144개, 합계 약 0.72 MB, lazy load)
  raw/courseList.json # 원본 API 응답 (재가공용 보존)
  raw/routeList.json
  state.json          # 재개 상태
```

보존 메타데이터: `crs_level`(난이도 1~3), `distance_km_source`/`distance_km_measured`,
`duration_min_source`, `elevation_gain_m`(GPX 고도 기반), `cycle`(순환형태), `sigun`,
`summary`/`contents`/`tour_info`/`traveler_info`(임베딩용 텍스트), `theme`(설명 텍스트 기반 추정).

## 조회 구조

인덱스(bbox+메타)만 메모리에 상주시키고, bbox 프리필터를 통과한 소수의 코스만
좌표 파일을 읽는다. 전 코스 좌표를 매 요청마다 파싱하면 응답 시간 NFR(AI-CRS-008, 5초)을
잠식하기 때문이다.

```
요청 → 테마 필터 → bbox 프리필터 → (통과분만) 좌표 lazy load → 정밀 거리 → 정렬
```

`find_nearby_courses(..., db=[...])` 로 목록을 직접 넘기면 기존 동작(인메모리 전수 스캔)을
그대로 쓴다. 인덱스가 없으면 레거시 `data/durunubi_courses.json` 으로 자동 폴백한다.

검증: `python scripts/verify_durunubi.py`

## 알려진 데이터 품질 이슈

- **GPX에 트랙이 2개 이상인 코스가 16건 있다** (본선 + 우회로 등).
  전부 이어붙이면 트랙 사이에 최대 13km짜리 가짜 직선이 생기므로,
  수집기가 트랙을 분리한 뒤 `crsDstnc` 에 가장 가까운 것을 본선으로 선택한다
  (`n_segments`, `segment_index` 로 기록).
- 제공 거리(`crsDstnc`)와 GPX 실측 거리 편차는 중앙값 2.2%, 10% 이내가 141/144건.
  `crsDstnc` 가 정수 km 반올림값이라 대부분 여기서 온다. 최대 편차는 24%.
- 테마(`theme`)는 두루누비가 제공하지 않아 **설명 텍스트 키워드로 추정한 값**이다
  (AI-CRS-004의 POI 기반 태깅 전까지의 임시 수단). 132/144건에 태깅됨.

## 향후 확장 (284개 이상이 열릴 경우)

`scripts/collect_durunubi.py` 의 `CourseListSource` 프로토콜만 새로 구현하면 된다.

```python
class CourseListSource(Protocol):
    name: str
    def fetch(self) -> list[dict]: ...   # gpxpath 키를 가진 원본 dict 리스트
```

이후 파이프라인(GPX 파싱 → 단순화 → bbox → 인덱스)과 조회 계층은 그대로 재사용된다.
인덱스에는 `schema_version` 과 `source` 를 기록해 두므로 소스 교체 이력을 추적할 수 있다.
