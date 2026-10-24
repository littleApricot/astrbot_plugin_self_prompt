"""Render current memory and project legacy tool metadata out of model inputs."""

import copy
import json
import re

from .prompt_document import (
    FIXED_END,
    FIXED_START,
    START,
    ensure_layout,
    read_fixed,
    read_sections,
    replace_section,
)

CORE_SECTIONS = ("persona", "output_preferences")
MARKERS = re.compile(r"<!-- /?self_prompt:[^>]*-->")


def current_memory(prompt):
    """Return only current owner instructions and long-term memory."""
    return {
        "fixed_prompt": read_fixed(prompt),
        "sections": {
            key: value
            for key, value in read_sections(prompt).items()
            if key in CORE_SECTIONS
        },
    }


def render_current(prompt):
    """Preserve external notes while removing management markup and empty notes."""
    layout = ensure_layout(prompt)
    layout = re.sub(
        r"## 其他需要记住的事情\s*<!-- self_prompt:memory -->.*?<!-- /self_prompt:memory -->",
        "",
        layout,
        flags=re.DOTALL,
    )
    layout = re.sub(
        r"(?m)^# 机器人可维护内容\s*\n+(?=" + re.escape(START) + ")",
        "# 长期记忆\n\n",
        layout,
    )
    return MARKERS.sub("", layout).strip()


def owner_edit(prompt, fixed, sections):
    """Apply a dashboard edit without losing protected surrounding notes."""
    if not isinstance(fixed, str) or MARKERS.search(fixed):
        raise ValueError("invalid_content")
    if not isinstance(sections, dict) or set(sections) != set(CORE_SECTIONS):
        raise ValueError("invalid_sections")
    result = ensure_layout(prompt)
    for key, text in sections.items():
        result = replace_section(result, key, text)
    begin = result.index(FIXED_START) + len(FIXED_START)
    end = result.index(FIXED_END, begin)
    return result[:begin] + "\n" + fixed + "\n" + result[end:]


def project_history(history):
    """Remove this plugin's old bookkeeping fields on a detached history copy."""
    result = copy.deepcopy(history)
    relevant = set()
    names = {"get_self_prompt", "update_self_prompt"}
    hidden = {"revision", "expected_revision", "version_id", "catalog_revision"}
    for message in result:
        if not isinstance(message, dict):
            continue
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function", {})
            if not isinstance(function, dict):
                continue
            if function.get("name") not in names:
                continue
            relevant.add(call.get("id"))
            try:
                args = json.loads(function.get("arguments", "{}"))
                if isinstance(args, dict):
                    function["arguments"] = json.dumps(
                        {
                            key: value
                            for key, value in args.items()
                            if key not in hidden
                        },
                        ensure_ascii=False,
                    )
            except (ValueError, TypeError):
                pass
        if message.get("role") != "tool" or message.get("tool_call_id") not in relevant:
            continue
        content = message.get("content", "")
        parts = (
            content
            if isinstance(content, list)
            else [{"type": "text", "text": content}]
        )
        for part in parts:
            if not isinstance(part, dict) or part.get("type") != "text":
                continue
            try:
                payload = json.loads(part.get("text", ""))
                if isinstance(payload, dict):
                    if payload.get("error") in {
                        "revision_conflict",
                        "version_conflict",
                    }:
                        payload["error"] = "content_changed"
                        payload["message"] = "内容已变化，请重新读取。"
                    part["text"] = json.dumps(
                        {
                            key: value
                            for key, value in payload.items()
                            if key not in hidden
                        },
                        ensure_ascii=False,
                    )
            except (ValueError, TypeError):
                pass
        if isinstance(content, str):
            message["content"] = parts[0]["text"]
    return result
