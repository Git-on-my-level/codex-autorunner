"""Helpers for matching and filtering picker-style option lists."""

from __future__ import annotations

from collections.abc import Mapping, Sequence


def _normalize(value: str) -> str:
    return " ".join(value.lower().split())


def find_exact_picker_item(
    items: Sequence[tuple[str, str]],
    query: str,
    *,
    aliases: Mapping[str, Sequence[str]] | None = None,
) -> tuple[str, str] | None:
    normalized_query = _normalize(query)
    if not normalized_query:
        return None
    alias_map = aliases or {}
    for value, label in items:
        candidate_values = [_normalize(value), _normalize(label)]
        for alias in alias_map.get(value, ()):
            candidate_values.append(_normalize(alias))
        if normalized_query in candidate_values:
            return value, label
    return None


def filter_picker_items(
    items: Sequence[tuple[str, str]],
    query: str,
    *,
    limit: int,
    aliases: Mapping[str, Sequence[str]] | None = None,
) -> list[tuple[str, str]]:
    if limit <= 0:
        return []
    normalized_query = _normalize(query)
    if not normalized_query:
        return list(items[:limit])

    tokens = [token for token in normalized_query.split(" ") if token]
    if not tokens:
        return list(items[:limit])

    alias_map = aliases or {}
    scored: list[tuple[int, int, tuple[str, str]]] = []
    for index, item in enumerate(items):
        value, label = item
        fields: list[str] = [_normalize(value), _normalize(label)]
        for alias in alias_map.get(value, ()):
            fields.append(_normalize(alias))
        fields = [field for field in fields if field]
        if not fields:
            continue
        combined = " ".join(fields)
        if any(token not in combined for token in tokens):
            continue

        score = 0
        for field in fields:
            if field == normalized_query:
                score = max(score, 100)
            elif field.startswith(normalized_query):
                score = max(score, 80)
            elif normalized_query in field:
                score = max(score, 60)
        if fields[0] == normalized_query:
            score += 20
        elif fields[0].startswith(normalized_query):
            score += 10

        scored.append((score, index, item))

    scored.sort(key=lambda entry: (-entry[0], entry[1]))
    return [item for _, _, item in scored[:limit]]
