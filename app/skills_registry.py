from __future__ import annotations

from pathlib import Path
from typing import Any


def _skills_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "skills"


def list_skill_names() -> list[str]:
    skills_dir = _skills_dir()
    if not skills_dir.exists():
        return []
    return sorted([p.name for p in skills_dir.iterdir() if p.is_dir()])


def read_skill_markdown(tool_name: str) -> str | None:
    skills_dir = _skills_dir()
    skill_file = skills_dir / tool_name / "Skills.md"
    if not skill_file.exists():
        return None
    content = skill_file.read_text(encoding="utf-8").strip()
    return content or None


def load_all_skills_markdown() -> str:
    skills_dir = _skills_dir()
    if not skills_dir.exists():
        return ""

    blocks: list[str] = []
    for tool_name in list_skill_names():
        content = read_skill_markdown(tool_name)
        if not content:
            continue
        blocks.append(f"## {tool_name}\n{content}")

    return "\n\n".join(blocks)


def search_skills(query: str, *, max_results: int = 20) -> list[dict[str, Any]]:
    q = query.strip()
    if not q:
        return []

    q_lower = q.lower()
    results: list[dict[str, Any]] = []

    for tool_name in list_skill_names():
        content = read_skill_markdown(tool_name)
        if not content:
            continue

        content_lower = content.lower()
        idx = 0
        while True:
            pos = content_lower.find(q_lower, idx)
            if pos == -1:
                break

            start = max(0, pos - 120)
            end = min(len(content), pos + len(q) + 120)
            snippet = content[start:end].replace("\n", " ").strip()

            results.append(
                {
                    "tool_name": tool_name,
                    "match_index": pos,
                    "snippet": snippet,
                }
            )

            if len(results) >= max_results:
                return results

            idx = pos + len(q)

    return results
