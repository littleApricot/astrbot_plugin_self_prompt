"""Maintain three editable sections inside an AstrBot persona prompt."""

from __future__ import annotations

import hashlib
import re

SECTIONS = {
    "persona": "性格和人设（包括昵称）",
    "output_preferences": "输出偏好要求",
    "memory": "其他需要记住的事情",
}
START = "<!-- self_prompt:v1 -->"
END = "<!-- /self_prompt:v1 -->"


def revision(prompt: str) -> str:
    """Return a content revision for detecting stale edits.

    Args:
        prompt: Current persisted persona prompt.

    Returns:
        SHA-256 of the complete prompt.
    """
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def read_sections(prompt: str) -> dict[str, str]:
    """Read managed sections, treating a legacy prompt as the persona section.

    Args:
        prompt: Complete persona prompt, including optional surrounding content.

    Returns:
        All three section contents.

    Raises:
        ValueError: Managed markers are incomplete or repeated.
    """
    if START not in prompt and END not in prompt:
        return {key: prompt if key == "persona" else "" for key in SECTIONS}
    if prompt.count(START) != 1 or prompt.count(END) != 1:
        raise ValueError("invalid_document_markers")
    begin = prompt.index(START) + len(START)
    end = prompt.index(END)
    if begin > end:
        raise ValueError("invalid_document_markers")
    body = prompt[begin:end]
    result = {}
    for key in SECTIONS:
        opening = f"<!-- self_prompt:{key} -->"
        closing = f"<!-- /self_prompt:{key} -->"
        if body.count(opening) != 1 or body.count(closing) != 1:
            raise ValueError("invalid_section_markers")
        match = re.search(
            re.escape(opening + "\n") + r"(.*?)" + re.escape("\n" + closing),
            body,
            re.DOTALL,
        )
        if match is None:
            raise ValueError("invalid_section_markers")
        result[key] = match.group(1)
    return result


def replace_section(prompt: str, section: str, content: str) -> str:
    """Replace one section while preserving other and surrounding content.

    Args:
        prompt: Current complete persona prompt.
        section: One of persona, output_preferences, or memory.
        content: Complete replacement content for that section; empty clears it.

    Returns:
        Updated complete persona prompt.

    Raises:
        ValueError: A section, content value, or existing marker is invalid.
    """
    if section not in SECTIONS:
        raise ValueError("unknown_section")
    if not isinstance(content, str) or any(
        marker in content for marker in ("<!-- self_prompt:", "<!-- /self_prompt:")
    ):
        raise ValueError("invalid_section_content")
    sections = read_sections(prompt)
    if START not in prompt:
        sections[section] = content
        parts = [START]
        for key, title in SECTIONS.items():
            parts.append(
                f"## {title}\n<!-- self_prompt:{key} -->\n"
                f"{sections[key]}\n<!-- /self_prompt:{key} -->"
            )
        parts.append(END)
        return "\n\n".join(parts)

    begin = prompt.index(START) + len(START)
    end = prompt.index(END)
    body = prompt[begin:end]
    pattern = re.compile(
        re.escape(f"<!-- self_prompt:{section} -->\n")
        + r"(.*?)"
        + re.escape(f"\n<!-- /self_prompt:{section} -->"),
        re.DOTALL,
    )
    match = pattern.search(body)
    assert match is not None  # read_sections validated the document.
    return prompt[: begin + match.start(1)] + content + prompt[begin + match.end(1) :]
