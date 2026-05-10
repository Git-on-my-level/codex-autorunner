from __future__ import annotations


def _truncate_text(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return f"{text[: limit - 3]}..."


REVIEW_COMMIT_BUTTON_LABEL_LIMIT = 80


def _parse_review_commit_log(output: str) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for record in output.split("\x1e"):
        record = record.strip()
        if not record:
            continue
        sha, _sep, subject = record.partition("\x1f")
        if not sha:
            continue
        entries.append((sha, subject.strip()))
    return entries


def _format_review_commit_label(sha: str, subject: str) -> str:
    short_sha = sha[:7]
    if subject:
        label = f"{short_sha} - {subject}"
    else:
        label = short_sha
    return _truncate_text(label, REVIEW_COMMIT_BUTTON_LABEL_LIMIT)
