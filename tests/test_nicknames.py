"""Check nickname migration and scoped writes using native isolated persistence."""

import asyncio
import json
from unittest.mock import patch

import httpx
import pytest
from astrbot.core.provider.register import llm_tools
from astrbot_plugin_self_prompt.core_memory import current_memory, owner_edit
from astrbot_plugin_self_prompt.main import WRITE_CONTEXT
from astrbot_plugin_self_prompt.prompt_document import (
    ensure_layout,
    read_sections,
    replace_section,
)
from astrbot_plugin_self_prompt.tests.support import Sandbox, app_for, legacy_prompt


def configured(names=None):
    return (
        "外部附注\n"
        + owner_edit(
            ensure_layout("人设原文"),
            {
                "persona": "人设原文",
                "output_preferences": "回复偏好原文",
                "nicknames": names if names is not None else ["机器人：阿蓝"],
                "other": "其他原文",
            },
        )
        + "\n末尾附注"
    )


@pytest.mark.asyncio
async def test_legacy_text_migrates_once_with_backup_and_owner_content_preserved():
    s = await Sandbox().open()
    try:
        original = (
            "外部附注\n"
            + legacy_prompt(nicknames="  机器人：阿蓝  \n\n成员甲：阿甲\n机器人：阿蓝")
            + "\n末尾附注"
        )
        await s.plugin._original_update("default", system_prompt=original)
        await s.plugin._ensure_layout("default")
        actual = (await s.manager.get_persona("default")).system_prompt
        assert current_memory(actual)["sections"]["nicknames"] == [
            "机器人：阿蓝",
            "成员甲：阿甲",
        ]
        for key in ("persona", "output_preferences", "other"):
            assert read_sections(actual)[key] == read_sections(original)[key]
        assert actual.startswith("外部附注\n") and actual.endswith("\n末尾附注")
        backups = list((s.plugin._data / "backups").rglob("*.txt"))
        assert original in [path.read_text() for path in backups]
        records = len(s.plugin.store.records("default"))
        await s.plugin._ensure_layout("default")
        assert (await s.manager.get_persona("default")).system_prompt == actual
        assert len(s.plugin.store.records("default")) == records
    finally:
        await s.close()


@pytest.mark.asyncio
async def test_nickname_tool_delta_dedup_rename_and_same_round_visibility():
    s = await Sandbox().open()
    try:
        original = configured()
        await s.manager.update_persona("default", system_prompt=original)
        await s.prepare(s.event)
        schema = llm_tools.get_func("nickname_update").parameters
        assert schema["required"] == []
        for key in ("add", "remove"):
            assert schema["properties"][key]["type"] == "array"
            assert schema["properties"][key]["items"] == {"type": "string"}
        first = json.loads(
            await s.plugin.nickname_update(
                s.event, add=[" 成员甲：阿甲 ", "成员甲：阿甲", "机器人：阿蓝"]
            )
        )
        assert first == {"ok": True, "nicknames": ["机器人：阿蓝", "成员甲：阿甲"]}
        # A correction is one atomic remove/add operation.
        changed = json.loads(
            await s.plugin.nickname_update(
                s.event, add=["机器人：小蓝"], remove=["机器人：阿蓝"]
            )
        )
        assert changed == {"ok": True, "nicknames": ["成员甲：阿甲", "机器人：小蓝"]}
        actual = (await s.manager.get_persona("default")).system_prompt
        assert actual == replace_section(
            original, "nicknames", json.dumps(changed["nicknames"], ensure_ascii=False)
        )
        assert "机器人：小蓝" in s.event.get_extra("self_prompt_system_text")
        assert s.event.get_extra("reply_gate_memory") == s.event.get_extra(
            "self_prompt_system_text"
        )
        assert (
            json.loads(await s.plugin.get_self_prompt(s.event))["sections"]["nicknames"]
            == changed["nicknames"]
        )
        request = await s.prepare(s.message(2))
        assert "机器人：小蓝" in request.system_prompt
        record = s.plugin.store.records("default")[0]
        assert record["kind"] == "nickname_tool" and record["state"] == "committed"
        count = len(s.plugin.store.records("default"))
        await s.plugin.nickname_update(s.event, add=["机器人：小蓝"], remove=["不存在"])
        assert len(s.plugin.store.records("default")) == count
        cleared = json.loads(
            await s.plugin.nickname_update(s.event, remove=changed["nicknames"])
        )
        assert cleared == {"ok": True, "nicknames": []}
    finally:
        await s.close()


@pytest.mark.asyncio
async def test_tool_rejects_invalid_items_and_cannot_write_other_areas():
    s = await Sandbox().open()
    try:
        original = configured()
        await s.manager.update_persona("default", system_prompt=original)
        for args in (
            {},
            {"add": "单个字符串"},
            {"add": [""]},
            {"add": [42]},
            {"add": ["<!-- self_prompt:persona -->"]},
            {"add": ["同项"], "remove": ["同项"]},
        ):
            assert (
                json.loads(await s.plugin.nickname_update(s.event, **args))["ok"]
                is False
            )
            assert (await s.manager.get_persona("default")).system_prompt == original
        # Exercise the save guard itself, beyond the public tool's narrow args.
        for target in (
            replace_section(original, "persona", "被篡改"),
            replace_section(original, "output_preferences", "被篡改"),
            replace_section(original, "other", "被篡改"),
            original.replace("## 回复偏好", "## 被篡改"),
            original.replace("外部附注", "被篡改"),
        ):
            token = WRITE_CONTEXT.set({"kind": "nickname_tool", "expected": original})
            try:
                with pytest.raises(ValueError):
                    await s.manager.update_persona("default", system_prompt=target)
            finally:
                WRITE_CONTEXT.reset(token)
            assert (await s.manager.get_persona("default")).system_prompt == original
    finally:
        await s.close()


@pytest.mark.asyncio
async def test_concurrent_deltas_preserve_both_changes_and_budget_failure_keeps_original():
    s = await Sandbox().open()
    try:
        await s.manager.update_persona("default", system_prompt=configured())
        events = [s.message(11), s.message(12)]
        for event in events:
            await s.prepare(event)
        native_get = s.manager.get_persona
        arrived = 0
        ready = asyncio.Event()

        async def same_initial_snapshot(persona_id):
            nonlocal arrived
            value = await native_get(persona_id)
            if arrived < 2:
                arrived += 1
                if arrived == 2:
                    ready.set()
                await ready.wait()
            return value

        with patch.object(s.manager, "get_persona", side_effect=same_initial_snapshot):
            results = await asyncio.gather(
                s.plugin.nickname_update(events[0], add=["成员乙：小乙"]),
                s.plugin.nickname_update(
                    events[1], add=["成员丙：小丙"], remove=["机器人：阿蓝"]
                ),
            )
        assert all(json.loads(value)["ok"] for value in results), results
        actual = (await native_get("default")).system_prompt
        assert set(current_memory(actual)["sections"]["nicknames"]) == {
            "成员乙：小乙",
            "成员丙：小丙",
        }
        s.config["long_term_memory_max_bytes"] = 60
        result = json.loads(
            await s.plugin.nickname_update(s.event, add=["很长的昵称" * 100])
        )
        assert result["error"] == "memory_budget_exceeded"
        assert (await native_get("default")).system_prompt == actual
    finally:
        await s.close()


@pytest.mark.asyncio
async def test_dashboard_nickname_list_is_protected_from_self_memory_tool():
    s = await Sandbox().open()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_for(s)),
            base_url="http://isolated.test",
            headers={"Authorization": "Bearer test-owner"},
        ) as client:
            prefix = "/api/v1/plugins/extensions/astrbot_plugin_self_prompt/"
            state = (await client.get(prefix + "state")).json()
            state["memory"]["sections"]["nicknames"] = [
                " 机器人：小蓝 ",
                "机器人：小蓝",
                "成员甲：阿甲",
            ]
            response = await client.post(
                prefix + "memory",
                json={
                    "persona_id": "default",
                    "stamp": state["stamp"],
                    **state["memory"],
                },
            )
            assert response.status_code == 200, response.text
            state = (await client.get(prefix + "state")).json()
            assert state["memory"]["sections"]["nicknames"] == [
                "机器人：小蓝",
                "成员甲：阿甲",
            ]
        owner = state["memory"]["sections"]["persona"]
        await s.plugin.get_self_prompt(s.event)
        changed = json.loads(
            await s.plugin.self_memory_update(s.event, "一条已确认的约定")
        )
        assert changed["ok"]
        actual = (await s.manager.get_persona("default")).system_prompt
        assert read_sections(actual)["persona"] == owner
        assert read_sections(actual)["other"] == "一条已确认的约定"
        assert current_memory(actual)["sections"]["nicknames"] == [
            "机器人：小蓝",
            "成员甲：阿甲",
        ]
    finally:
        await s.close()
