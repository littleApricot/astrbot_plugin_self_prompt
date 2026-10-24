"""Validate nickname lists and retain text from earlier plugin versions."""

import json


def validate_nicknames(value):
    if not isinstance(value, list):
        raise ValueError("invalid_nicknames")
    names = []
    for name in value:
        if (
            not isinstance(name, str)
            or not name.strip()
            or "<!-- self_prompt:" in name
            or "<!-- /self_prompt:" in name
        ):
            raise ValueError("invalid_nicknames")
        name = name.strip()
        if name not in names:
            names.append(name)
    return names


def read_nicknames(content):
    """Read canonical arrays or preserve each nonempty legacy text line."""
    if isinstance(content, list):
        return validate_nicknames(content)
    if not isinstance(content, str):
        raise ValueError("invalid_nicknames")
    try:
        value = json.loads(content)
    except ValueError:
        value = None
    if isinstance(value, list):
        return validate_nicknames(value)
    return validate_nicknames([line for line in content.splitlines() if line.strip()])


def nickname_text(value):
    return json.dumps(validate_nicknames(value), ensure_ascii=False)
