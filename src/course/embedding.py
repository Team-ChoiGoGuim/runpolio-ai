"""임베딩 클라이언트.

AI-CRS-004(테마 매칭)가 후보 선별에 쓰는 EmbeddingClient(src/course/selector.py)의
구현체를 모아둔다.
"""
from __future__ import annotations

import random


class MockEmbeddingClient:
    """임베딩 모델 제공자 확정 전까지 쓰는 임시 대용품 (랜덤 벡터).

    선별 로직(selector.select_top_k)의 배선을 검증하는 용도일 뿐, 실제 의미
    기반 매칭은 하지 않는다. 실 서비스 배포 전 반드시 실제 임베딩 클라이언트로
    교체할 것 — EMBEDDING_API_KEY 가 아직 비어 있는 상태(.env.example 참고).
    """

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[random.random() for _ in range(64)] for _ in texts]
