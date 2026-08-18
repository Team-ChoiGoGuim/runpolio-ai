"""서버 없이 파이프라인을 직접 실행하고 지도로 결과를 확인하는 스크립트.

사용법:
  1) .env 파일에 TMAP_APP_KEY 입력
  2) python try_it.py
  3) 생성된 result_map.html을 브라우저에서 열기
"""
import asyncio
import json
import random
from dotenv import load_dotenv
load_dotenv()

from src.course.schemas import CourseRequest, Theme
from src.course.pipeline import create_courses
from src.course.visualizer import generate_map_html


class MockEmbeddingClient:
    """임베딩 모델 확정 전 임시 대용품 (랜덤 벡터)."""
    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[random.random() for _ in range(64)] for _ in texts]


async def main():
    # ── 테스트 요청: 여의도 한강공원 근처, 5km 순환 코스 ──
    req = CourseRequest(
        center=(37.5283, 126.9346),
        distance_km=5.0,
        duration_min=35.0,
        theme=Theme.RIVER,
        loop=True,
    )

    print("=" * 50)
    print(f"📍 요청: {req.center}")
    print(f"   거리: {req.distance_km}km / 목표 시간: {req.duration_min:g}분 / 테마: {req.theme.value}")
    print(f"   순환형: {req.loop}")
    print("=" * 50)

    result = await create_courses(req, MockEmbeddingClient())

    for w in result.warnings:
        print(f"\n⚠ 안내: {w}")

    if result.fallback_reason:
        print(f"\n⚠ 실패: {result.fallback_reason}")
        return

    # 터미널 출력
    print(f"\n✅ {len(result.courses)}개 코스 생성됨:\n")
    for i, c in enumerate(result.courses, 1):
        source_label = "🌿 두루누비" if c.source.value == "DURUNUBI" else "🤖 AI 생성"
        print(f"── 코스 {i} ({source_label}) ──")
        print(f"   거리: {c.distance}km / 목표 시간: {c.target_duration_min:g}분")
        print(f"   좌표 수: {len(c.path_data)}개")
        if c.source_course_id:
            print(f"   원본 코스 ID: {c.source_course_id}")
        print()

    # 지도 HTML 생성
    generate_map_html(result, "result_map.html")


if __name__ == "__main__":
    asyncio.run(main())
