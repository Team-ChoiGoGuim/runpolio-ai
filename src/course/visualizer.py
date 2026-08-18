"""파이프라인 결과를 지도 위에 시각화하는 HTML 파일 생성.

Leaflet + OpenStreetMap 사용 (API 키 불필요, 무료).
생성된 HTML을 브라우저에서 열면 경로가 지도 위에 그려져 보인다.
"""
from __future__ import annotations
import json
from pathlib import Path
from src.course.schemas import CourseResponse

# 코스별 색상 (두루누비: 초록, TMAP 생성: 파랑/빨강/보라...)
COLORS = ["#2ecc71", "#3498db", "#e74c3c", "#9b59b6", "#f39c12"]


def generate_map_html(response: CourseResponse, output_path: str = "result_map.html"):
    """CourseResponse → 지도 HTML 파일 생성."""

    courses_js = []
    for i, course in enumerate(response.courses):
        coords_js = json.dumps([[lat, lng] for lat, lng in course.path_data])
        color = COLORS[i % len(COLORS)]
        label = f"코스 {i+1} ({course.source.value}) — {course.distance}km"
        courses_js.append(f"""
        {{
            coords: {coords_js},
            color: "{color}",
            label: "{label}",
            distance: {course.distance},
            duration: {int(course.target_duration_min * 60)},
            source: "{course.source.value}",
            theme: {json.dumps(course.theme, ensure_ascii=False)}
        }}""")

    # 지도 중심: 첫 코스의 첫 좌표
    if response.courses and response.courses[0].path_data:
        center = response.courses[0].path_data[0]
    else:
        center = (37.5283, 126.9346)

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>런폴리오 코스 시각화</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, 'Pretendard', sans-serif; background: #f5f5f5; }}

  #map {{ width: 100%; height: 70vh; }}

  .panel {{
    padding: 20px;
    max-width: 900px;
    margin: 0 auto;
  }}
  h1 {{
    font-size: 20px;
    padding: 16px 20px;
    background: #1a1a2e;
    color: white;
  }}

  .course-cards {{
    display: flex;
    gap: 12px;
    margin-top: 16px;
    flex-wrap: wrap;
  }}
  .course-card {{
    flex: 1;
    min-width: 200px;
    background: white;
    border-radius: 12px;
    padding: 16px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.1);
    cursor: pointer;
    border: 3px solid transparent;
    transition: border-color 0.2s;
  }}
  .course-card:hover {{ border-color: #ddd; }}
  .course-card.active {{ border-color: var(--card-color); }}

  .card-color-dot {{
    width: 14px; height: 14px;
    border-radius: 50%;
    display: inline-block;
    margin-right: 8px;
    vertical-align: middle;
  }}
  .card-title {{ font-weight: 700; font-size: 15px; margin-bottom: 8px; }}
  .card-meta {{ font-size: 13px; color: #666; line-height: 1.6; }}
  .card-badge {{
    display: inline-block;
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 99px;
    font-weight: 600;
    margin-top: 6px;
  }}
  .badge-durunubi {{ background: #d5f5e3; color: #1e8449; }}
  .badge-ai {{ background: #d6eaf8; color: #2471a3; }}

  .start-marker {{
    background: #e74c3c;
    border: 3px solid white;
    border-radius: 50%;
    width: 16px; height: 16px;
    box-shadow: 0 2px 6px rgba(0,0,0,0.3);
  }}
</style>
</head>
<body>

<h1>🏃 런폴리오 — AI 코스 추천 결과</h1>
<div id="map"></div>

<div class="panel">
  <div class="course-cards" id="cards"></div>
</div>

<script>
const courses = [{",".join(courses_js)}
];

// ── 지도 초기화 ──
const map = L.map('map').setView([{center[0]}, {center[1]}], 14);
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
  attribution: '&copy; OpenStreetMap contributors'
}}).addTo(map);

// 출발 지점 마커
const startIcon = L.divIcon({{ className: 'start-marker', iconSize: [16, 16] }});
L.marker([{center[0]}, {center[1]}], {{ icon: startIcon }}).addTo(map)
  .bindPopup('<b>출발 지점</b>');

// ── 경로 그리기 ──
const polylines = [];
const bounds = L.latLngBounds();

courses.forEach((c, i) => {{
  const line = L.polyline(c.coords, {{
    color: c.color,
    weight: 5,
    opacity: 0.85,
  }}).addTo(map);

  line.bindPopup(`<b>${{c.label}}</b><br>거리: ${{c.distance}}km<br>시간: ${{Math.round(c.duration/60)}}분`);
  polylines.push(line);
  c.coords.forEach(p => bounds.extend(p));
}});

map.fitBounds(bounds, {{ padding: [40, 40] }});

// ── 카드 UI ──
const cardsDiv = document.getElementById('cards');

courses.forEach((c, i) => {{
  const card = document.createElement('div');
  card.className = 'course-card active';
  card.style.setProperty('--card-color', c.color);
  card.dataset.index = i;

  const badgeClass = c.source === 'DURUNUBI' ? 'badge-durunubi' : 'badge-ai';
  const badgeText = c.source === 'DURUNUBI' ? '두루누비 코스' : 'AI 생성 코스';

  card.innerHTML = `
    <div class="card-title">
      <span class="card-color-dot" style="background:${{c.color}}"></span>
      코스 ${{i + 1}}
    </div>
    <div class="card-meta">
      거리: ${{c.distance}}km<br>
      예상 시간: ${{Math.round(c.duration / 60)}}분<br>
      테마: ${{c.theme.length ? c.theme.join(', ') : '-'}}
    </div>
    <span class="card-badge ${{badgeClass}}">${{badgeText}}</span>
  `;

  // 카드 클릭 → 해당 경로 토글
  card.addEventListener('click', () => {{
    const visible = map.hasLayer(polylines[i]);
    if (visible) {{
      map.removeLayer(polylines[i]);
      card.classList.remove('active');
    }} else {{
      polylines[i].addTo(map);
      card.classList.add('active');
      map.fitBounds(polylines[i].getBounds(), {{ padding: [40, 40] }});
    }}
  }});

  cardsDiv.appendChild(card);
}});
</script>
</body>
</html>"""

    Path(output_path).write_text(html, encoding="utf-8")
    print(f"\n🗺  지도 파일 생성됨: {output_path}")
    print(f"   브라우저에서 열어보세요!")
