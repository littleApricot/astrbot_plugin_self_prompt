"""Specify legacy migration and protected self-memory writes."""

import json
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_self_prompt.core_memory import current_memory, render_current
from astrbot_plugin_self_prompt.main import WRITE_CONTEXT
from astrbot_plugin_self_prompt.prompt_document import (
    ensure_layout,
    read_sections,
    replace_section,
)
from astrbot_plugin_self_prompt.tests.support import Sandbox, legacy_prompt


def test_old_layout_keeps_all_content_in_four_current_sections():
    original = (
        "外部附注\n"
        + legacy_prompt(
            fixed="用户原固定内容  \n",
            persona="原始人设",
            memory="原有其他记忆",
            nicknames="机器人：阿蓝",
        )
        + "\n结尾附注"
    )
    updated = ensure_layout(original)
    sections = current_memory(updated)["sections"]
    assert sections == {
        "persona": "用户原固定内容  \n\n\n原始人设",
        "output_preferences": "Short",
        "nicknames": ["机器人：阿蓝"],
        "other": "原有其他记忆",
    }
    assert updated.startswith("外部附注\n") and updated.endswith("\n结尾附注")
    assert ensure_layout(updated) == updated
    rendered = render_current(updated)
    assert "固定设定" not in rendered and "# 长期记忆" not in rendered
    assert all(
        "## " + title in rendered
        for title in ("性格和人设", "回复偏好", "昵称", "自我认知")
    )


@pytest.mark.asyncio
async def test_upgrading_keeps_notebook_and_exact_original_backup():
    s = await Sandbox().open()
    try:
        page = s.plugin.store.create_page(
            "default", "旧笔记", "原有用途", "原有正文", s.plugin.settings()
        )
        original = legacy_prompt(memory="原有其他记忆", include_nicknames=False)
        await s.plugin._original_update("default", system_prompt=original)
        await s.plugin._ensure_layout("default")
        actual = (await s.manager.get_persona("default")).system_prompt
        assert read_sections(actual)["other"] == "原有其他记忆"
        assert current_memory(actual)["sections"]["nicknames"] == []
        assert s.plugin.store.page("default", page["page_id"])["content"] == "原有正文"
        assert original in [
            p.read_text() for p in (s.plugin._data / "backups").rglob("*.txt")
        ]
    finally:
        await s.close()


@pytest.mark.asyncio
async def test_save_guard_rejects_protected_memory_tool_changes():
    s = await Sandbox().open()
    try:
        original = (await s.manager.get_persona("default")).system_prompt
        for key in ("persona", "output_preferences", "nicknames"):
            token = WRITE_CONTEXT.set({"kind": "memory_tool", "expected": original})
            try:
                with pytest.raises(ValueError, match="protected_sections_modified"):
                    await s.manager.update_persona(
                        "default",
                        system_prompt=replace_section(original, key, "Attempt"),
                    )
            finally:
                WRITE_CONTEXT.reset(token)
            assert (await s.manager.get_persona("default")).system_prompt == original
    finally:
        await s.close()


@pytest.mark.asyncio
async def test_interrupted_other_save_retains_sources_until_recovery():
    s = await Sandbox().open()
    try:
        original_save = s.plugin._original_update
        s.plugin._original_update = AsyncMock(
            side_effect=OSError("isolated interruption")
        )
        result = json.loads(await s.plugin.self_memory_update(s.event, "已确认的约定"))
        assert not result["ok"]
        assert s.plugin.store.prepared("default")
        s.plugin._original_update = original_save
        await s.plugin._recover("default")
        assert (
            read_sections((await s.manager.get_persona("default")).system_prompt)[
                "other"
            ]
            == "已确认的约定"
        )
        assert not s.plugin.store.prepared("default")
    finally:
        await s.close()
