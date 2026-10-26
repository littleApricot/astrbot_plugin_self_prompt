"""Keep owner-only persona sections separate from writable current memory."""

from __future__ import annotations

import hashlib
import re

from .nicknames import nickname_text, read_nicknames

SECTIONS = {
    "persona": "性格和人设",
    "output_preferences": "回复偏好",
    "chat_examples": "聊天示例",
    "nicknames": "昵称",
    "other": "自我认知",
}
START = "<!-- self_prompt:document:v3 -->"
END = "<!-- /self_prompt:document:v3 -->"
V2_START = "<!-- self_prompt:editable:v2 -->"
V2_END = "<!-- /self_prompt:editable:v2 -->"
FIXED_START = "<!-- self_prompt:fixed:v2 -->"
FIXED_END = "<!-- /self_prompt:fixed:v2 -->"
LEGACY_START = "<!-- self_prompt:v1 -->"
LEGACY_END = "<!-- /self_prompt:v1 -->"
OLD_SECTIONS = ("persona", "output_preferences", "memory", "nicknames")
MARKERS = re.compile(r"<!-- /?self_prompt:[^>]*-->")


def revision(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _bounds(prompt: str, opening: str, closing: str) -> tuple[int, int]:
    if prompt.count(opening) != 1 or prompt.count(closing) != 1:
        raise ValueError("invalid_document_markers")
    begin = prompt.index(opening) + len(opening)
    end = prompt.index(closing)
    if begin > end:
        raise ValueError("invalid_document_markers")
    return begin, end


def _section_spans(body: str, keys=SECTIONS, optional=()) -> dict[str, re.Match]:
    result = {}
    previous_end = 0
    for key in keys:
        opening = f"<!-- self_prompt:{key} -->"
        closing = f"<!-- /self_prompt:{key} -->"
        if key in optional and opening not in body and closing not in body:
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
    if len(MARKERS.findall(body)) != 2 * len(result):
        raise ValueError("unexpected_section_markers")
    return result


def _unwrap(text: str) -> str:
    return text[1:-1] if text.startswith("\n") and text.endswith("\n") else text


def _document(sections: dict[str, str]) -> str:
    parts = [START]
    for key, title in SECTIONS.items():
        content = sections.get(key, "")
        if key == "nicknames":
            content = nickname_text(read_nicknames(content))
        parts.append(
            f"## {title}\n<!-- self_prompt:{key} -->\n"
            f"{content}\n<!-- /self_prompt:{key} -->"
        )
    return "\n\n".join([*parts, END])


def ensure_layout(prompt: str) -> str:
    """Retain legacy text while adopting independently owned sections."""
    if START in prompt or END in prompt:
        begin, end = _bounds(prompt, START, END)
        spans = _section_spans(prompt[begin:end], optional=("chat_examples",))
        if len(MARKERS.findall(prompt)) != 2 * (len(spans) + 1):
            raise ValueError("unexpected_section_markers")
        match = spans["nicknames"]
        layout = (
            prompt[: begin + match.start(1)]
            + nickname_text(read_nicknames(match.group(1)))
            + prompt[begin + match.end(1) :]
        )
        if "chat_examples" not in spans:
            # Insert after preferences without rewriting existing section text.
            position = begin + spans["output_preferences"].end()
            section = (
                "\n\n## 聊天示例\n<!-- self_prompt:chat_examples -->\n"
                "\n<!-- /self_prompt:chat_examples -->"
            )
            layout = layout[:position] + section + layout[position:]
        return layout.replace(
            "## 其他\n<!-- self_prompt:other -->",
            "## 自我认知\n<!-- self_prompt:other -->",
            1,
        )
    if any(marker in prompt for marker in (V2_START, V2_END, FIXED_START, FIXED_END)):
        begin, end = _bounds(prompt, V2_START, V2_END)
        fixed_begin, fixed_end = _bounds(prompt, FIXED_START, FIXED_END)
        if not (
            fixed_end + len(FIXED_END) <= begin - len(V2_START)
            or end + len(V2_END) <= fixed_begin - len(FIXED_START)
        ):
            raise ValueError("overlapping_fixed_and_editable")
        spans = _section_spans(prompt[begin:end], OLD_SECTIONS, ("nicknames",))
        if len(MARKERS.findall(prompt)) != 2 * (len(spans) + 2):
            raise ValueError("unexpected_section_markers")
        old = {key: "" for key in OLD_SECTIONS} | {
            key: match.group(1) for key, match in spans.items()
        }
        fixed = _unwrap(prompt[fixed_begin:fixed_end])
        sections = {
            "persona": "\n\n".join(part for part in (fixed, old["persona"]) if part),
            "output_preferences": old["output_preferences"],
            "nicknames": old["nicknames"],
            "other": old["memory"],
        }
        # Preserve external notes without rewriting headings inside user content.
        replacements = [
            (begin - len(V2_START), end + len(V2_END), _document(sections)),
            (fixed_begin - len(FIXED_START), fixed_end + len(FIXED_END), ""),
        ]
        parts = []
        cursor = 0
        for start, stop, content in sorted(replacements):
            outside = re.sub(
                r"(?m)^# (?:你维护的固定设定|机器人可维护内容|长期记忆)[ \t]*$",
                "",
                prompt[cursor:start],
            )
            parts.extend((outside, content))
            cursor = stop
        return "".join([*parts, prompt[cursor:]])
    if LEGACY_START in prompt or LEGACY_END in prompt:
        begin, end = _bounds(prompt, LEGACY_START, LEGACY_END)
        spans = _section_spans(prompt[begin:end], OLD_SECTIONS, ("nicknames",))
        if len(MARKERS.findall(prompt)) != 2 * (len(spans) + 1):
            raise ValueError("unexpected_section_markers")
        old = {key: "" for key in OLD_SECTIONS} | {
            key: match.group(1) for key, match in spans.items()
        }
        return _document(
            {
                "persona": "\n\n".join(
                    part
                    for part in (
                        prompt[: begin - len(LEGACY_START)],
                        old["persona"],
                        prompt[end + len(LEGACY_END) :],
                    )
                    if part
                ),
                "output_preferences": old["output_preferences"],
                "nicknames": old["nicknames"],
                "other": old["memory"],
            }
        )
    if MARKERS.search(prompt):
        raise ValueError("invalid_document_markers")
    return _document({"persona": prompt})


def read_sections(prompt: str) -> dict[str, str]:
    layout = ensure_layout(prompt)
    begin, end = _bounds(layout, START, END)
    return {
        key: match.group(1) for key, match in _section_spans(layout[begin:end]).items()
    }


def replace_section(prompt: str, section: str, content: str) -> str:
    """Replace one section; callers enforce the writer's permitted fields."""
    if section not in SECTIONS:
        raise ValueError("unknown_section")
    if not isinstance(content, str) or MARKERS.search(content):
        raise ValueError("invalid_section_content")
    if section == "nicknames":
        content = nickname_text(read_nicknames(content))
    baseline = ensure_layout(prompt)
    begin, end = _bounds(baseline, START, END)
    match = _section_spans(baseline[begin:end])[section]
    return (
        baseline[: begin + match.start(1)] + content + baseline[begin + match.end(1) :]
    )


def assert_editable_change(
    original: str, updated: str, allowed=("nicknames", "other")
) -> None:
    """Enforce exact ownership of every section and all surrounding text."""
    if not set(allowed).issubset({"nicknames", "other"}):
        raise ValueError("invalid_sections")
    baseline = ensure_layout(original)
    candidate = ensure_layout(updated)
    values = read_sections(candidate)
    for key in allowed:
        baseline = replace_section(baseline, key, values[key])
    if baseline != candidate:
        raise ValueError("protected_sections_modified")
