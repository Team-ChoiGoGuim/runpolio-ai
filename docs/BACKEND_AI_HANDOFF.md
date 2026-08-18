# 백엔드 연동 가이드 — Runpolio AI 코스 추천

AI-CRS(커스텀 러닝 코스 생성) 모듈을 백엔드에 붙이기 위한 문서.
**연동에 필요한 것은 `POST /api/v1/courses/recommend` 하나다.**

| 문서 | 용도 |
|---|---|
| [`openapi-runpolio-ai.yaml`](openapi-runpolio-ai.yaml) | **백엔드 연동 계약** — 추천 API |
| [`openapi.yaml`](openapi.yaml) | 내부 운영용 — 두루누비 원천 데이터 조회 (부록 참고) |
| [`DATA_SOURCES.md`](DATA_SOURCES.md) | 데이터 출처·수집 구조·제약 사항 |
| [`inquiry_tourapi.md`](inquiry_tourapi.md) | 한국관광공사 문의 초안 |

Swagger Editor([editor.swagger.io](https://editor.swagger.io))에 YAML을 붙여넣으면 바로 열린다.

---

## ⚠️ 먼저 알아야 할 것

### 1. HTTP 서버는 아직 없다

`openapi-runpolio-ai.yaml` 은 **계약 명세이고, 이를 서빙하는 FastAPI 앱(`main.py`)은
아직 만들지 않았다.** 현재 호출 가능한 것은 파이썬 함수뿐이다.

```python
from src.course.pipeline import create_courses   # async
```

명세는 이 함수의 입출력을 그대로 옮긴 것이라, `main.py` 를 붙이면 그대로 성립한다.
서버 구현이 필요하면 알려줄 것.

### 2. 두루누비 데이터는 144개뿐이다

공공데이터포털 서비스 설명은 **284개 코스**를 명시하지만 공개 API가 실제로 반환하는 것은
**144개**(약 51%)다. 원본 사이트(durunubi.kr)에는 297개가 GPX와 함께 존재하나 API에는 없다.
제공자 측 데이터 동기화 문제로 판단되며 문의 초안을 준비해 두었다.

근거와 재현 방법은 [`DATA_SOURCES.md`](DATA_SOURCES.md) 참고.

### 3. 수도권에서는 두루누비 매칭이 없다

코리아둘레길은 해안선·DMZ 노선이라 **서울 1건 / 인천 2건**뿐이다.

```
강원 35 · 전남 29 · 충남 17 · 경북 17 · 경기 15 · 경남 14
전북 6 · 부산 4 · 울산 4 · 인천 2 · 서울 1
```

서울·대전 등의 요청은 `AI_GENERATED` 3개로만 응답된다. **오류가 아니라 정상 동작이다.**
284개가 전부 열려도 해결되지 않는 구조적 한계다.

---

## 동작 규칙

두루누비 탐색과 TMAP 후보 생성을 **항상 병행**한 뒤 조합한다.

| 두루누비 매칭 | 응답 구성 |
|---|---|
| 있음 | `DURUNUBI` 1개 + `AI_GENERATED` 2개 = **3개** |
| 없음 | `AI_GENERATED` **3개** |

- **3개는 상한이다.** 목표 거리 ±400m 안에 드는 후보가 모자라면 그 이하가 온다.
  TMAP이 전부 실패하고 두루누비만 매칭되면 1개만 오기도 한다.
- **`fallback_reason` 은 `courses` 가 완전히 빈 경우에만 채워진다.**
  코스가 1개라도 있으면 항상 `null`. 프론트에서 "결과 없음" 판단은
  `courses.length === 0` 으로 할 것.
- 두루누비 코스가 있으면 **항상 배열의 첫 항목**이다.
- 조건 미충족도 **HTTP 200**으로 응답한다. 4xx가 아니다.

실측 응답 시간: 630~890ms (NFR 5초).

---

## 반경 탐색과 TMAP 호출 예산

한 번의 추천 요청이 쓰는 TMAP 호출은 **최대 25회로 제한**된다.

| 단계 | 호출 수 | 내용 |
|---|---|---|
| 1차 생성 | 최대 **10회** | 후보 10개, 각 1회 |
| 반경 보정 | 최대 **15회** | 목표 거리에 가까운 **5개**만, 후보당 최대 **3회** |
| ±300m·±400m | **0회** | 이미 만든 후보 중에서만 선택 |
| **합계** | **≤ 25회** | `tmap_call_budget` 이 하드 리밋으로 재차 강제 |

- **동시 요청은 최대 5개**로 제한된다(`max_concurrency`).
- **±200m 안에서 서로 다른 경로 3개를 확보하면 즉시 모든 생성을 중단한다.**
  진행 중인 호출은 완료되지만 새 호출은 발생하지 않는다.
- "서로 다른 경로"는 균등 표본 좌표 8개를 약 11m 격자로 반올림한 지문으로 판정한다.
  같은 경로가 반복 생성되면 1개로 센다.
- 밴드를 못 맞춘 경로도 **후보 풀에 보존**된다. ±300m·±400m 단계는 이 풀에서 고르며
  **새 TMAP 호출을 하지 않는다.**

### 후보 캐시

중심 위치·거리·순환 조건이 같으면 TTL 동안 후보를 재사용한다.

- 캐시 키: `(위도 5자리, 경도 5자리, 목표 거리 m, loop)`
- 기본 TTL 300초, 최대 128개 항목 (`candidate_cache`)
- **캐시 적중 시 TMAP 호출 0회.** 실측 1,301ms → **10ms**

### 관측 로그

호출 수·캐시 적중·단계별 후보 수가 매 요청 로그로 남는다.

```
TMAP 후보 생성 결과 | {'tmap_calls': 16, 'budget': 25, 'initial_calls': 10,
 'refine_calls': 6, 'cache_hit': False, 'initial_candidates': 10,
 'refine_targets': 5, 'pool_size': 16, 'accepted_in_band': 4,
 'early_stopped': True, 'budget_exhausted': False, 'errors': {}}

코스 생성 요약 | {..., 'selected_band_m': 200, 'durunubi_matched': True,
 'band_pool': 4, 'returned_tmap': 2}
```

| 지표 | 뜻 |
|---|---|
| `tmap_calls` / `budget` | 실제 호출 수 / 상한 |
| `initial_calls` / `refine_calls` | 1차 / 보정 단계 호출 수 |
| `cache_hit` | 캐시 적중 여부 |
| `initial_candidates` | 1차에서 성공한 서로 다른 경로 수 |
| `refine_targets` | 보정 대상으로 고른 후보 수 (≤5) |
| `pool_size` | 최종 후보 풀 크기 |
| `accepted_in_band` | ±200m 안의 서로 다른 경로 수 |
| `selected_band_m` | 최종 선택에 쓰인 밴드 (200/300/400) |
| `early_stopped` | 조기 중단 여부 |
| `budget_exhausted` | 예산 소진으로 멈췄는지 |

조정은 `configs/course.yaml` 의 `generation`·`candidate_cache` 에서 한다.

---

## 요청 / 응답

### 요청

```json
{
  "center": {"lat": 35.1587, "lng": 129.1604},
  "distance_km": 5.0,
  "duration_min": 32.0,
  "theme": "해변",
  "loop": true
}
```

| 필드 | 필수 | 기본값 | 설명 |
|---|---|---|---|
| `center.lat` / `center.lng` | ✅ | — | WGS84 |
| `distance_km` | ✅ | — | **경로 생성의 실제 목표.** ±200m 우선, 부족하면 ±300m·±400m |
| `duration_min` | ✅ | — | 사용자가 설정한 목표 시간(분). **생성 조건이 아님** |
| `theme` | | 없음 | `해변`/`강변`/`공원`/`호수`/`도심` |
| `loop` | | `true` | 순환 여부 |

**`difficulty` 는 제거됐다.** 요청에 넣어도 무시된다.

#### 거리와 목표 시간의 관계

경로는 **항상 `distance_km` 기준**으로 만든다. `duration_min` 은 목표 요약과
오차 표시에만 쓰이고, 코스 선택·생성 조건이 되지 않는다.

둘이 현실적으로 맞지 않아도(예: 5km를 10분) **요청을 거절하지 않는다.**
코스는 정상 생성되고 `warnings` 에 안내가 담긴다. 판정 범위는
`configs/course.yaml` 의 `pace_sanity` (기본 3~15분/km).

```json
"warnings": [
  "목표 시간은 참고값이며 현재 경로 생성은 거리 기준입니다. 입력하신 5km / 10분은 약 2.0분/km 페이스로, 일반적인 러닝 범위(3~15분/km)를 벗어납니다."
]
```

#### `warnings` 는 두 종류다

| 종류 | 발생 조건 | 설정 |
|---|---|---|
| 목표 페이스 비현실 | `duration_min / distance_km` 이 3~15분/km 밖 | `pace_sanity` |
| 시간 오차 초과 | `time_error_min > 5` 인 코스가 있음 | `time_error_warn_min` |

**둘 다 동시에 올 수 있고, 어느 쪽도 코스를 제거하지 않는다.**
배열을 순회해 전부 보여줄 것 — 첫 항목만 읽으면 안 된다.

### 응답

```json
{
  "goal_summary": {
    "target_distance_km": 5.0,
    "target_duration_min": 30.0,
    "target_pace_sec_per_km": 360,
    "target_pace_display": "6:00/km",
    "target_speed_kmh": 10.0,
    "target_intensity": "중"
  },
  "courses": [
    {
      "course_id": "crs_22b3affe",
      "source": "DURUNUBI",
      "source_course_id": "T_CRS_MNG0000004378",
      "theme": ["해변"],
      "distance": 5.0,
      "target_duration_min": 30.0,
      "distance_error_m": 0,
      "distance_accuracy": "EXACT",
      "estimated_duration_min": 30.0,
      "time_error_min": 0.0,
      "path_data": [[35.158975, 129.160273], [35.159314, 129.16214]],
      "center_location": [35.1587, 129.1604]
    }
  ],
  "warnings": [],
  "fallback_reason": null
}
```

| 필드 | 설명 |
|---|---|
| `goal_summary` | 사용자 목표 요약 (아래 참고). **코스 생성 실패 시에도 반환** |
| `course_id` | ⚠️ **임시 식별자** — 저장·재조회 금지 (아래 참고) |
| `source` | `DURUNUBI` / `AI_GENERATED` |
| `source_course_id` | 두루누비 원본 `crsIdx`. **영속 ID.** `AI_GENERATED` 는 `null` |
| `distance` | 실제 생성된 거리(km), 소수점 2자리 |
| `distance_error_m` | `|실제 거리 − 목표 거리|` (m) |
| `distance_accuracy` | `EXACT`(±200m) / `RELAXED_300M` / `RELAXED_400M` |
| `estimated_duration_min` | 목표 페이스로 이 거리를 달렸을 때의 시간(분). **완주 예측 아님** |
| `time_error_min` | `|예상 시간 − 목표 시간|` (분). 표시·경고용 |
| `target_duration_min` | 사용자 입력 목표 시간(분). `goal_summary` 와 동일 값 |
| `path_data` | **`[위도, 경도]` 순서** — GeoJSON과 반대 |
| `warnings` | 안내 문구 배열. 성공/실패 무관하게 채워질 수 있음 (2종, 아래 참고) |
| `fallback_reason` | `courses` 가 **빈 경우에만** 채워짐 |

> ⚠️ **`distance` 로 오차를 재계산하지 말 것.** `distance` 는 소수점 2자리
> 반올림값(±5m)이고 `distance_error_m`·`estimated_duration_min` 은 원본 정밀도로
> 계산된다. 예를 들어 실제 4,822m 코스는 `distance: 4.82` / `distance_error_m: 178`
> 로 오는데, 4.82km로 되짚으면 180m가 나온다. **서버가 준 값을 그대로 쓸 것.**

**제거된 필드**: `difficulty`, `expected_duration`, `elevation_gain`.

---

## 목표 요약 (`goal_summary`)

`distance_km` 과 `duration_min` 을 환산한 값이다. 전부 입력에서 파생되며
경로 생성 조건이 아니다. **요청이 유효하면 코스 생성에 실패해도 항상 반환된다.**

```
target_pace_sec_per_km = duration_min × 60 / distance_km
target_speed_kmh       = distance_km / (duration_min / 60)
```

| 필드 | 5km / 30분 예시 |
|---|---|
| `target_pace_sec_per_km` | `360` |
| `target_pace_display` | `"6:00/km"` |
| `target_speed_kmh` | `10.0` |
| `target_intensity` | `"중"` |

### ⚠️ `target_intensity` 는 코스 난이도가 아니다

**사용자가 설정한 목표가 얼마나 빡센지**를 나타내는 **목표 운동 강도**다.
코스의 경사·노면·난이도와는 무관하다. 같은 코스라도 사용자가 목표 시간을
바꾸면 값이 달라진다.

| 강도 | 목표 페이스 | 예시 |
|---|---|---|
| `상` | 5분 30초/km 이하 (≤330초) | 5km / 25분 → `5:00/km` |
| `중` | 5분 30초 초과 ~ 7분/km 이하 (331~420초) | 5km / 30분 → `6:00/km` |
| `하` | 7분/km 초과 (>420초) | 5km / 40분 → `8:00/km` |

기준은 `configs/course.yaml` 의 `target_intensity` 에서 조정한다.

```yaml
target_intensity:
  high_max_pace_sec_per_km: 330
  medium_max_pace_sec_per_km: 420
```

---

## 거리·시간 오차

```
distance_error_m       = |실제 거리 − 목표 거리|
estimated_duration_min = 실제 거리(km) × target_pace_sec_per_km / 60
time_error_min         = |estimated_duration_min − target_duration_min|
```

### 거리 정확도

| 값 | 오차 |
|---|---|
| `EXACT` | ±200m 이내 |
| `RELAXED_300M` | ±201~300m |
| `RELAXED_400M` | ±301~400m |

**±400m 를 초과하는 코스는 반환하지 않는다.** 거리 후보 선정 정책(±200m 우선,
부족하면 기존 후보만으로 ±300m·±400m 완화)은 그대로다.

### ⚠️ `estimated_duration_min` 은 완주 시간 예측이 아니다

**사용자가 설정한 목표 페이스로 실제 생성된 거리를 달렸을 때의 시간**일 뿐이다.
따라서 `time_error_min` 은 "거리가 목표에서 벗어난 만큼 시간이 어긋난다"는 뜻이지,
이 사용자가 실제로 그만큼 걸린다는 예측이 아니다.

실제 완주 시간을 예측하려면 아래와 같은 **독립적인 데이터**가 필요하며 현재는
반영하지 않는다.

- 코스 경사도 (오르막·내리막 구간의 페이스 저하)
- 신호등·횡단보도 대기 시간
- 노면 상태, 계단·병목 구간
- 개인 러닝 기록 (실제 페이스 이력, 지구력 곡선)

### 시간 오차 경고

`time_error_min` 은 **후보 생성·필터 조건이 아니라 표시·경고 기준**이다.
5분(`time_error_warn_min`)을 넘어도 **코스를 제거하지 않고** `warnings` 에만 안내를 추가한다.

```json
"warnings": [
  "목표 시간과 5분 넘게 차이나는 코스가 2개 있습니다(최대 5.6분). 시간 오차는 목표 페이스로 실제 생성된 거리를 달렸을 때의 차이이며, 실제 완주 시간 예측이 아닙니다."
]
```

페이스가 느릴수록 같은 거리 오차가 큰 시간 오차로 증폭된다. 예를 들어
5km/70분(14분/km) 목표에서 거리 오차 400m는 시간 오차 5.6분이 되지만,
5km/25분(5분/km) 목표에서는 2.0분에 그친다.

**좌표 순서 주의.** `[lat, lng]` 다. 카카오맵·구글맵 SDK는 대체로 `(lat, lng)` 를 쓰지만
GeoJSON으로 변환할 때는 뒤집어야 한다.

### ⚠️ `course_id` 는 저장하면 안 된다

`course_id`(`crs_` + 8자리 hex)는 **추천 요청마다 새로 만드는 임시 값**이다.
같은 조건으로 다시 요청하면 다른 값이 나온다.

- **영구 저장 키로 쓰지 말 것**
- **재조회 키로 쓸 수 없음** — 이 ID로 코스를 되찾는 엔드포인트는 없다
- 코스를 보관하려면 `path_data` 를 포함한 **코스 내용을 저장**하고 백엔드가 자체 ID를 부여할 것

두루누비 원본 코스를 가리켜야 한다면 **`source_course_id`** 를 쓴다. 이 값은 안정적이며
내부 운영 API `GET /courses/{course_id}` 에 그대로 넣을 수 있다.

```
course_id        crs_62376d6a           ← 임시. 매 요청 변경
source_course_id T_CRS_MNG0000004378    ← 영속. 두루누비 crsIdx
```

---

## 테스트 시나리오

명세의 `examples` 에 아래 4가지가 **실제 응답 그대로** 들어 있다.

| 시나리오 | 요청 | 기대 결과 |
|---|---|---|
| 성공 (두루누비 매칭) | 해운대 `35.1587, 129.1604` / 5km / 32분 | `DURUNUBI` 1 + `AI_GENERATED` 2 |
| 두루누비 미매칭 | 서울시청 `37.5663, 126.9779` / 5km / 30분 | `AI_GENERATED` 3, `fallback_reason: null` |
| 비현실적 목표 시간 | 해운대 / 5km / **10분** | 코스 정상 생성 + `warnings` 1건 |
| 부분 성공 | 해운대 + TMAP 장애 | `DURUNUBI` 1개만, `fallback_reason: null` |
| 완전 실패 | 두루누비·TMAP 모두 실패 | `courses: []` + `fallback_reason` |
| 해상 좌표 | `34.0, 124.0` | `courses: []` + `fallback_reason` (**500 아님**) |
| 목표 강도 상 | 5km / 25분 | `5:00/km`, `12.0km/h`, `상` |
| 목표 강도 중 | 5km / 30분 | `6:00/km`, `10.0km/h`, `중` |
| 목표 강도 하 | 5km / 40분 | `8:00/km`, `7.5km/h`, `하` |

두루누비 매칭이 잘 잡히는 좌표(수동 테스트용):

```
부산 해운대해수욕장  35.1587, 129.1604  → 해파랑길 2코스 (33m)
강릉 경포해변        37.7952, 128.9106  → 해파랑길 39코스 (206m)
목포 유달산          34.7869, 126.3792  → 서해랑길 18코스 (129m)
고성 통일전망대      38.5730, 128.3620  → DMZ 34코스 (1,433m)
```

미매칭 확인용: 서울 시청 `37.5663, 126.9779`, 대전 정부청사 `36.3620, 127.3845`

---

## 별도 구현 항목

### 1. 목표 시간을 실제 추천 조건으로 사용하기

`duration_min` 은 이제 **목표 요약(`goal_summary`)과 코스별 오차 계산에 쓰인다.**
다만 여전히 **경로 생성 조건은 아니다** — 코스 선택·생성에는 `distance_km` 만 쓴다.

목표 시간을 실제 생성 조건으로 지원하려면 결정·구현이 필요하다.

- 시간 → 거리 역산의 기준 페이스를 무엇으로 둘 것인가
  (난이도가 제거됐으므로 사용자 프로필·과거 기록 등 다른 근거가 필요하다)
- `distance_km` 과 `duration_min` 이 충돌할 때의 우선순위 규칙
- 충돌 시 응답 정책 — 현재처럼 `warnings` 로 안내할지, 거리를 재조정할지

지금은 불일치를 **경고로만 알리고 거리 기준으로 생성**한다.
판정 범위는 `configs/course.yaml` 의 `pace_sanity` 에서 조정할 수 있다.

### 2. 실제 완주 시간 예측

`estimated_duration_min` 은 목표 페이스 환산값이라 실제 소요 시간과 다르다.
실측 기반 예측을 하려면 경사도, 신호등·횡단보도 대기, 노면 상태, 개인 러닝 기록
같은 독립 데이터가 필요하다. 두루누비 코스는 GPX 고도를 수집해 두었으므로
경사도 항목은 바로 붙일 수 있지만, `AI_GENERATED` 는 고도 원천이 없다.

### 3. 난이도 산정 (AI-CRS-005)

`difficulty` 는 추천 API 계약에서 **완전히 제거**됐다.
두루누비 원본의 `crs_level`(1/2/3)과 GPX 고도는 수집 데이터에 그대로 남아 있으므로
(내부 운영 API `openapi.yaml` 에서 조회 가능), 난이도 개념을 다시 도입할 때 재사용할 수 있다.

### 4. 테마 태깅 (AI-CRS-004)

두루누비 코스의 `theme` 은 코스 설명 텍스트의 키워드로 추정한 값이다(144건 중 132건 태깅).
POI 기반 실제 태깅으로 교체가 필요하다. `require_theme_match: true` 와 맞물려
매칭률에 직접 영향을 준다.

---

## TMAP 실패 처리 (완료)

TMAP 호출 실패는 **어떤 유형이든 예외로 새어나가지 않는다.** 모두 내부에서 흡수되어
`courses: []` + `fallback_reason` 으로 응답한다(HTTP 200). 후보 하나가 실패해도
나머지 후보 생성은 계속된다.

실패 유형은 내부 관측용 구조화 코드로 서버 로그에 남는다.

| 코드 | 발생 조건 |
|---|---|
| `TMAP_TIMEOUT` | 연결·응답 타임아웃 |
| `TMAP_HTTP_ERROR` | 4xx / 5xx 응답 |
| `TMAP_NETWORK_ERROR` | 연결 실패 등 네트워크 오류 |
| `TMAP_INVALID_CONTENT_TYPE` | `Content-Type` 이 JSON이 아님 (예: HTML 오류 페이지) |
| `TMAP_INVALID_JSON` | 본문 JSON 파싱 실패 |
| `TMAP_EMPTY_ROUTE` | 204 No Content / 빈 본문 / 경로 좌표 없음 |
| `TMAP_UNEXPECTED` | 그 외 예상치 못한 예외 |

> 목표 거리를 못 맞춘 응답은 **실패가 아니다.** 밴드 밖 경로도 후보 풀에 남아
> ±300m·±400m 단계에서 쓰이므로 오류 코드가 붙지 않는다. 대신 `accepted_in_band`
> 와 `selected_band_m` 지표로 관측한다.

로그 예시:

```
WARNING [TMAP_EMPTY_ROUTE] TMAP 경로 없음 (HTTP 204)
WARNING TMAP 후보 생성 결과: 성공 0 / 실패 10 | {'TMAP_EMPTY_ROUTE': 10}
```

**로그 안전성**: 비정상 응답 본문은 앞 200자만 남기고, `TMAP_APP_KEY` 값과
`appKey`/`token`/`authorization` 등으로 라벨링된 값은 마스킹한다.

> 해상 좌표(`34.0, 124.0`) 요청 시 TMAP은 **204 No Content** 를 빈 본문으로 반환한다.
> 이전에는 이 응답에서 `JSONDecodeError` 가 전파돼 500이 났으나, 지금은
> `TMAP_EMPTY_ROUTE` 로 분류되어 정상 계약으로 응답한다.

---

## 데이터 수집 · 실행

### 최초 설정

```bash
pip install -r requirements.txt
```

`.env` 에 키를 채운다 (`.env.example` 참고).

```
TMAP_APP_KEY=...        # 필수 — AI_GENERATED 코스 생성
DATA_GO_KR_KEY=...      # 필수 — 두루누비 수집
```

### 두루누비 데이터 수집

```bash
python scripts/collect_durunubi.py
```

144개 코스를 수집하는 데 **약 36초**, 공공데이터포털 쿼터는 **2건**만 쓴다
(GPX는 durunubi.kr 정적 서버라 쿼터 미소모, 개발계정 한도는 1,000건/일).
중단해도 재실행하면 이어서 진행한다.

| 옵션 | 용도 |
|---|---|
| `--limit N` | N건만 수집 (동작 확인) |
| `--force` | 완료분 포함 전량 재수집 |
| `--reindex` | 재다운로드 없이 인덱스만 재생성 |
| `--epsilon M` | 경로 단순화 허용오차(m). 기본 5, `0`이면 원본 유지 |

**갱신 주기**: 두루누비 원본은 일 1회 갱신된다. `modifiedtime` 이 같으면 건너뛰므로
재실행 비용은 낮다. 정기 실행 주기는 아직 정하지 않았다.

### 검증

```bash
python scripts/verify_durunubi.py
```

저장 구조, 조회 성능(전수 스캔 대비), 대표 위치 매칭, 다운샘플링 오차를 출력한다.

```bash
python -m pytest tests/ -q
```

### 서버 없이 파이프라인 실행

```bash
python try_it.py
```

`result_map.html` 이 생성되어 브라우저에서 코스를 확인할 수 있다.

---

## 수집 결과 파일 구조

```
data/durunubi/
  index.json           # 코스 메타 + bbox — 288KB, 메모리 상주
  paths/{crsIdx}.json  # 좌표 + 고도 — 144개, 합계 1.1MB, lazy load
  raw/courseList.json  # 원본 API 응답 보존
  raw/routeList.json
  state.json           # 수집 재개 상태
```

> `.gitignore` 에서 제외되어 있다. 36초면 재생성되지만, 팀원이 API 키 없이 바로
> 돌리게 하려면 `data/durunubi/` 줄을 지우고 커밋하면 된다 (약 1.3MB).

### `index.json`

```json
{
  "schema_version": 1,
  "source": "data.go.kr/B551011/Durunubi",
  "collected_at": "2026-08-18T15:14:30+09:00",
  "source_total_count": 144,
  "course_count": 144,
  "courses": [ { /* 아래 필드 */ } ]
}
```

| 필드 | 설명 |
|---|---|
| `course_id` | 두루누비 `crsIdx` (영속 식별자) |
| `route_id` | 소속 노선 (해파랑길/남파랑길/서해랑길/DMZ) |
| `name`, `sigun` | 코스명, 행정구역 |
| `bbox` | `[min_lat, min_lng, max_lat, max_lng]` — 프리필터용 |
| `theme` | 설명 텍스트 기반 추정 테마 |
| `crs_level` | 두루누비 난이도 1(하)/2(중)/3(상) |
| `distance_km_source` | 두루누비 제공 거리(정수 km 반올림) |
| `distance_km_measured` | GPX 실측 거리 |
| `duration_min_source` | 두루누비 제공 **도보** 소요시간(분). 러닝 기준 아님 |
| `elevation_gain_m` | GPX 고도 기반 누적 상승고도 |
| `cycle` | 순환형 / 비순환형 |
| `n_points` / `n_points_raw` | 저장 좌표 수 / 원본 좌표 수 |
| `n_segments` / `segment_index` | GPX 트랙 수 / 선택한 트랙 (아래 참고) |
| `summary`, `contents`, `tour_info`, `traveler_info` | 설명 텍스트 (임베딩용) |
| `gpx_url`, `created_at`, `modified_at` | 원본 추적용 |

### `paths/{crsIdx}.json`

```json
{
  "course_id": "T_CRS_MNG0000004378",
  "path": [[35.158975, 129.160273], [35.159314, 129.16214]],
  "elevation": [13.7, 15.5]
}
```

`path` 와 `elevation` 은 같은 인덱스로 대응된다. 좌표는 `[위도, 경도]`.

### 조회 구조

인덱스만 메모리에 상주시키고, bbox 프리필터를 통과한 소수의 코스만 좌표 파일을 읽는다.

```
요청 → 테마 필터 → bbox 프리필터 → (통과분만) 좌표 lazy load → 정밀 거리 → 정렬
```

전수 스캔 대비 18~82배 빠르고, 6개 대표 위치에서 **결과가 완전히 동일함을 검증**했다.

### 데이터 품질 메모

- GPX에 트랙이 2개 이상인 코스가 **16건** 있다(본선 + 우회로 등). 이어붙이면 최대 13km짜리
  가짜 직선이 생기므로, 수집기가 트랙을 분리해 `crsDstnc` 에 가장 가까운 것을 본선으로 고른다.
- 제공 거리와 GPX 실측 거리 편차는 중앙값 2.2%, 10% 이내가 141/144건, 최대 24%.
- 경로 단순화(ε=5m)로 인한 거리 오차는 평균 0.69% / 최대 1.19%.

---

## 부록 — 내부 운영 API

수집 데이터를 직접 조회하는 엔드포인트로, **백엔드 연동에는 필요하지 않다.**
수집 결과 점검과 매칭 디버깅용이다. 명세: [`openapi.yaml`](../openapi.yaml)

| 엔드포인트 | 용도 |
|---|---|
| `GET /health` | 인덱스 로드 상태, 수집 코스 수, `last_updated` |
| `GET /courses/nearby?lat=&lng=&radius_m=` | 반경 내 두루누비 코스 후보 |
| `GET /courses/{course_id}` | 코스 메타·난이도·거리·설명 |
| `GET /courses/{course_id}/path` | 코스 좌표 경로 |

여기서 쓰는 `course_id` 는 두루누비 `crsIdx` 로 **영속 식별자**다.
추천 API 응답의 임시 `course_id`(`crs_xxxxxxxx`)와는 다른 값이니 혼동하지 말 것.
