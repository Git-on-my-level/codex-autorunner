import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

_MERMAID_FENCE_RE = re.compile(
    r"(?ms)^```[ \t]*mermaid[^\n]*\n(?P<body>.*?)^```[ \t]*\n?"
)


@dataclass(frozen=True)
class MermaidFence:
    index: int
    body: str
    start: int
    end: int


def extract_mermaid_fences(markdown: str) -> list[MermaidFence]:
    fences: list[MermaidFence] = []
    for idx, match in enumerate(_MERMAID_FENCE_RE.finditer(markdown), start=1):
        fences.append(
            MermaidFence(
                index=idx,
                body=match.group("body").strip(),
                start=match.start(),
                end=match.end(),
            )
        )
    return fences


def rewrite_markdown_with_mermaid_images(
    markdown: str, fences: Sequence[MermaidFence], image_names: Sequence[str]
) -> str:
    if len(fences) != len(image_names):
        raise ValueError("fence/image count mismatch")

    parts: list[str] = []
    cursor = 0
    for fence, image_name in zip(fences, image_names):
        parts.append(markdown[cursor : fence.start])
        parts.append(f"![Mermaid Diagram {fence.index}]({image_name})\n\n")
        cursor = fence.end
    parts.append(markdown[cursor:])
    return "".join(parts)


def artifact_base_name(source: Path) -> str:
    # Keep filenames simple and stable for filebox exports.
    base = source.stem.strip() or "export"
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("-")
    return normalized or "export"
