from __future__ import annotations

import math
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any

TOKEN_RE = re.compile(r"[A-Za-z0-9_:\-]+")


def tokenize(text: str) -> list[str]:
    return [token.lower() for token in TOKEN_RE.findall(str(text or "")) if len(token) > 1]


def sparse_vector(tokens: list[str], buckets: int = 512) -> dict[int, float]:
    counts = Counter(tokens)
    if not counts:
        return {}
    total = float(sum(counts.values()))
    vector: dict[int, float] = {}
    for token, count in counts.items():
        bucket = hash(token) % buckets
        vector[bucket] = vector.get(bucket, 0.0) + (count / total)
    return vector


def cosine(left: dict[int, float], right: dict[int, float]) -> float:
    if not left or not right:
        return 0.0
    dot = sum(value * right.get(key, 0.0) for key, value in left.items())
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return dot / (left_norm * right_norm)


def lexical_score(query_tokens: list[str], doc_tokens: list[str]) -> float:
    if not query_tokens or not doc_tokens:
        return 0.0
    query_counts = Counter(query_tokens)
    doc_counts = Counter(doc_tokens)
    overlap = 0.0
    for token, q_count in query_counts.items():
        if token in doc_counts:
            overlap += min(q_count, doc_counts[token])
    return overlap / math.sqrt(max(len(query_tokens) * len(doc_tokens), 1))


def parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.fromisoformat(raw[:10])
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def recency_score(value: Any, *, half_life_days: float = 45.0) -> float:
    parsed = parse_datetime(value)
    if parsed is None:
        return 0.0
    age_days = max((datetime.now(timezone.utc) - parsed).total_seconds() / 86400.0, 0.0)
    return math.exp(-age_days / max(half_life_days, 1.0))


def normalized_upper(value: Any) -> str:
    return str(value or "").upper().replace(" FUT", "").strip()
