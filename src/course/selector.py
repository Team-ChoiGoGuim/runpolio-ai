"""후보 → 상위 N개 선별 (임베딩 기반 시맨틱 매칭)."""
from __future__ import annotations
import math
from typing import Protocol
from src.course.generator import RawCandidate
from src.course.schemas import CourseRequest


class EmbeddingClient(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def build_profile_sentence(req: CourseRequest) -> str:
    parts = []
    if req.theme:
        parts.append(f"{req.theme.value} 근처를 지나는")
    parts.append(f"{req.distance_km:.1f}km")
    parts.append("순환형" if req.loop else "편도형")
    parts.append("러닝 코스")
    return " ".join(parts)


def build_candidate_sentence(cand: RawCandidate, poi_summary: str = "") -> str:
    direction = _bearing_to_korean(cand.bearing_offset)
    rotation = "시계 방향" if cand.clockwise else "반시계 방향"
    base = f"{cand.distance_m / 1000:.1f}km {direction} 방면 {rotation} 순환 코스"
    return f"{base}, {poi_summary}" if poi_summary else base


def _bearing_to_korean(deg: float) -> str:
    dirs = ["북", "북동", "동", "남동", "남", "남서", "서", "북서"]
    return dirs[round((deg % 360) / 45) % 8] + "쪽"


def select_top_k(
    req, candidates, client: EmbeddingClient, top_k=3, poi_summaries=None,
) -> list[RawCandidate]:
    if len(candidates) <= top_k:
        return candidates
    poi_summaries = poi_summaries or [""] * len(candidates)
    profile = build_profile_sentence(req)
    sentences = [profile] + [
        build_candidate_sentence(c, s) for c, s in zip(candidates, poi_summaries)
    ]
    vectors = client.embed(sentences)
    profile_vec, cand_vecs = vectors[0], vectors[1:]
    scored = sorted(
        zip(candidates, cand_vecs),
        key=lambda cv: _cosine(profile_vec, cv[1]),
        reverse=True,
    )
    return [c for c, _ in scored[:top_k]]
