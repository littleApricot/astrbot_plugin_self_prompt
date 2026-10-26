"""Render current memory and project legacy tool metadata out of model inputs."""

import copy
import json
import re

from .nicknames import nickname_text, read_nicknames
from .prompt_document import (
    SECTIONS,
    ensure_layout,
    read_sections,
    replace_section,
)

CORE_SECTIONS = tuple(SECTIONS)
MARKERS = re.compile(r"<!-- /?self_prompt:[^>]*-->")


def current_memory(prompt):
    """Return only current owner instructions and long-term memory."""
    return {
        "sections": {
            key: read_nicknames(value) if key == "nicknames" else value
            for key, value in read_sections(prompt).items()
            if key in CORE_SECTIONS
        },
    }


def editable_bytes(prompt):
    return sum(
        len(value.encode())
        for key, value in read_sections(ensure_layout(prompt)).items()
        if key in {"nicknames", "other"}
    )


def render_current(prompt):
    """Render current sections without storage markup."""
    return MARKERS.sub("", ensure_layout(prompt)).strip()


def owner_edit(prompt, sections):
    """Apply a dashboard edit without losing protected surrounding notes."""
    required = set(CORE_SECTIONS) - {"chat_examples"}
    if (
        not isinstance(sections, dict)
        or not required.issubset(sections)
        or not set(sections).issubset(CORE_SECTIONS)
    ):
        raise ValueError("invalid_sections")
    # A dashboard opened before the upgrade must preserve any saved examples.
    result = ensure_layout(prompt)
    for key, text in sections.items():
        if key == "nicknames":
            text = nickname_text(read_nicknames(text))
        result = replace_section(result, key, text)
    return result


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
