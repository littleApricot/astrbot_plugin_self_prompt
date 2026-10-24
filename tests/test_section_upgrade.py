"""Exercise section upgrades and notebook transfers through real persistence."""

import json
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_self_prompt.auto_memory import apply_summary
from astrbot_plugin_self_prompt.core_memory import (
    current_memory,
    owner_edit,
    render_current,
)
from astrbot_plugin_self_prompt.prompt_document import (
    ensure_layout,
    read_fixed,
    read_sections,
)
from astrbot_plugin_self_prompt.tests.support import Sandbox


def transfer(text="术语甲表示先暂停。"):
    return json.dumps(
        {"updates": {}, "notebook_transfers": [{"section": "persona", "text": text}]},
        ensure_ascii=False,
    )


def test_old_layout_preserves_fixed_and_original_body():
    prompt = owner_edit(
        ensure_layout("原始人设。术语甲表示先暂停。其他人设。"),
        "用户固定设定",
        {
            "persona": "原始人设。术语甲表示先暂停。其他人设。",
            "output_preferences": "简洁",
            "nicknames": "甲昵称",
        },
    )
    old = prompt.replace("## 性格和人设\n", "## 性格和人设（包括昵称）\n").replace(
        "## 回复偏好\n", "## 输出偏好要求\n"
    )
    old = old.replace(
        "## 昵称\n<!-- self_prompt:nicknames -->\n甲昵称\n<!-- /self_prompt:nicknames -->",
        "",
    )
    new = ensure_layout(old)
    assert read_fixed(new) == "用户固定设定"
    assert read_sections(new)["persona"] == read_sections(old)["persona"]
    assert set(current_memory(new)["sections"]) == {
        "persona",
        "output_preferences",
        "nicknames",
    }
    rendered = render_current(new)
    assert "# 长期记忆" not in rendered
    assert (
        "## 性格和人设" in rendered
        and "## 回复偏好" in rendered
        and "## 昵称" in rendered
    )
    assert ensure_layout(new) == new
    moved = apply_summary(new, transfer())
    assert read_sections(moved)["persona"] == "原始人设。其他人设。"
    assert read_sections(moved)["memory"] == "术语甲表示先暂停。"
    assert read_fixed(moved) == "用户固定设定"


@pytest.mark.parametrize(
    "value",
    [
        {"section": "fixed_prompt", "text": "固定"},
        {"section": "persona", "text": "不存在的片段"},
        {"section": "persona", "text": ""},
        {"section": [], "text": "正文"},
    ],
)
def test_transfer_requires_exact_editable_original(value):
    with pytest.raises(ValueError):
        apply_summary(
            "人设正文", json.dumps({"updates": {}, "notebook_transfers": [value]})
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "capacity", "interrupted"])
async def test_transfer_save_acknowledges_only_after_notebook_and_persona(failure):
    sandbox = await Sandbox().open()
    try:
        plugin = sandbox.plugin
        await sandbox.manager.update_persona(
            "default", system_prompt="前。术语甲表示先暂停。后。"
        )
        sandbox.config["auto_memory_message_interval"] = 1
        sandbox.provider.text = transfer()
        await plugin.collect_history(sandbox.message(10, "请记住这个用法"))
        if failure == "capacity":
            sandbox.config["notebook_max_pages"] = 1
            plugin.store.create_page(
                "default", "已有页面", "保留", "已有正文", plugin.settings()
            )
        original = (await sandbox.manager.get_persona("default")).system_prompt
        original_save = plugin._original_update
        if failure == "interrupted":
            plugin._original_update = AsyncMock(
                side_effect=OSError("isolated save interruption")
            )
        await plugin._process_ready()
        if failure:
            assert (
                await sandbox.manager.get_persona("default")
            ).system_prompt == original
            assert plugin.store.pending_count() == 1
        if failure == "interrupted":
            plugin._original_update = original_save
            await plugin._recover("default")
        if failure != "capacity":
            actual = (await sandbox.manager.get_persona("default")).system_prompt
            assert read_sections(actual)["persona"] == "前。后。"
            pages = plugin.store.catalog("default", plugin.settings())["pages"]
            assert len(pages) == 1
            assert (
                plugin.store.page("default", pages[0]["page_id"])["content"]
                == "术语甲表示先暂停。"
            )
            assert plugin.store.pending_count() == 0
            record = next(
                row
                for row in plugin.store.records("default")
                if row["kind"] == "automatic" and row["state"] == "committed"
            )
            assert plugin.store.record("default", record["id"])["sources"][0]["text"]
    finally:
        await sandbox.close()
