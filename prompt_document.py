"""Separate owner-maintained instructions from the bot's editable sections."""

from __future__ import annotations

import hashlib
import re

SECTIONS = {
    "persona": "性格和人设",
    "output_preferences": "回复偏好",
    "memory": "其他需要记住的事情",
    "nicknames": "昵称",
}
START = "<!-- self_prompt:editable:v2 -->"
END = "<!-- /self_prompt:editable:v2 -->"
FIXED_START = "<!-- self_prompt:fixed:v2 -->"
FIXED_END = "<!-- /self_prompt:fixed:v2 -->"
LEGACY_START = "<!-- self_prompt:v1 -->"
LEGACY_END = "<!-- /self_prompt:v1 -->"


def revision(prompt: str) -> str:
    """Return a revision covering both fixed and editable content."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _bounds(prompt: str, opening: str, closing: str) -> tuple[int, int]:
    if prompt.count(opening) != 1 or prompt.count(closing) != 1:
        raise ValueError("invalid_document_markers")
    begin = prompt.index(opening) + len(opening)
    end = prompt.index(closing)
    if begin > end:
        raise ValueError("invalid_document_markers")
    return begin, end


def _section_spans(body: str) -> dict[str, re.Match]:
    result = {}
    previous_end = 0
    for key in SECTIONS:
        opening = f"<!-- self_prompt:{key} -->"
        closing = f"<!-- /self_prompt:{key} -->"
        if key == "nicknames" and opening not in body and closing not in body:
            continue
        if body.count(opening) != 1 or body.count(closing) != 1:
            raise ValueError("invalid_section_markers")
        match = re.search(
            re.escape(opening + "\n") + r"(.*?)" + re.escape("\n" + closing),
            body,
            re.DOTALL,
        )
        if match is None or match.start() < previous_end:
            raise ValueError("invalid_section_markers")
        result[key] = match
        previous_end = match.end()
    if len(re.findall(r"<!-- /?self_prompt:[^>]*-->", body)) != 2 * len(result):
        raise ValueError("unexpected_section_markers")
    return result


def _is_split(prompt: str) -> bool:
    return any(marker in prompt for marker in (START, END, FIXED_START, FIXED_END))


def _split_bounds(prompt: str) -> tuple[int, int, int, int]:
    begin, end = _bounds(prompt, START, END)
    fixed_begin, fixed_end = _bounds(prompt, FIXED_START, FIXED_END)
    if not (
        fixed_end + len(FIXED_END) <= begin - len(START)
        or end + len(END) <= fixed_begin - len(FIXED_START)
    ):
        raise ValueError("overlapping_fixed_and_editable")
    _section_spans(prompt[begin:end])
    return begin, end, fixed_begin, fixed_end


def _legacy_sections(prompt: str) -> dict[str, str]:
    if LEGACY_START not in prompt and LEGACY_END not in prompt:
        return {key: prompt if key == "persona" else "" for key in SECTIONS}
    begin, end = _bounds(prompt, LEGACY_START, LEGACY_END)
    result = {key: "" for key in SECTIONS} | {
        key: match.group(1) for key, match in _section_spans(prompt[begin:end]).items()
    }
    # Existing surrounding notes also remain bot-editable after migration.
    surrounding = [
        prompt[: begin - len(LEGACY_START)],
        result["persona"],
        prompt[end + len(LEGACY_END) :],
    ]
    result["persona"] = "\n\n".join(part for part in surrounding if part)
    return result


def ensure_layout(prompt: str) -> str:
    """Migrate existing content into the editable part; leave fixed content empty."""
    if _is_split(prompt):
        begin, end, _, _ = _split_bounds(prompt)
        body = prompt[begin:end]
        body = body.replace("## 性格和人设（包括昵称）\n", "## 性格和人设\n")
        body = body.replace("## 输出偏好要求\n", "## 回复偏好\n")
        if "nicknames" not in _section_spans(body):
            body = body.rstrip() + (
                "\n\n## 昵称\n<!-- self_prompt:nicknames -->\n\n"
                "<!-- /self_prompt:nicknames -->\n\n"
            )
        return prompt[:begin] + body + prompt[end:]
    sections = _legacy_sections(prompt)
    parts = [
        "# 你维护的固定设定",
        FIXED_START + "\n\n" + FIXED_END,
        "# 机器人可维护内容",
        START,
    ]
    for key, title in SECTIONS.items():
        parts.append(
            f"## {title}\n<!-- self_prompt:{key} -->\n"
            f"{sections[key]}\n<!-- /self_prompt:{key} -->"
        )
    parts.append(END)
    return "\n\n".join(parts)


def read_sections(prompt: str) -> dict[str, str]:
    """Read only bot-editable sections, retaining compatibility with old prompts."""
    if not _is_split(prompt):
        return _legacy_sections(prompt)
    begin, end, _, _ = _split_bounds(prompt)
    return {key: "" for key in SECTIONS} | {
        key: match.group(1) for key, match in _section_spans(prompt[begin:end]).items()
    }


def read_fixed(prompt: str) -> str:
    """Read owner instructions as reference; tools cannot write this field."""
    if not _is_split(prompt):
        _legacy_sections(prompt)
        return ""
    _, _, begin, end = _split_bounds(prompt)
    content = prompt[begin:end]
    return (
        content[1:-1]
        if content.startswith("\n") and content.endswith("\n")
        else content
    )


def assert_editable_change(original: str, updated: str) -> None:
    """Reject writes changing any bytes outside the editable body."""
    baseline = ensure_layout(original)
    begin, end, _, _ = _split_bounds(baseline)
    new_begin, new_end, _, _ = _split_bounds(updated)
    if (baseline[:begin], baseline[end:]) != (updated[:new_begin], updated[new_end:]):
        raise ValueError("fixed_prompt_modified")


def replace_section(prompt: str, section: str, content: str) -> str:
    """Replace one editable section while preserving the fixed part byte for byte."""
    if section not in SECTIONS:
        raise ValueError("unknown_section")
    if not isinstance(content, str) or any(
        marker in content for marker in ("<!-- self_prompt:", "<!-- /self_prompt:")
    ):
        raise ValueError("invalid_section_content")
    baseline = ensure_layout(prompt)
    begin, end, _, _ = _split_bounds(baseline)
    match = _section_spans(baseline[begin:end])[section]
    updated = (
        baseline[: begin + match.start(1)] + content + baseline[begin + match.end(1) :]
    )
    assert_editable_change(prompt, updated)
    return updated
