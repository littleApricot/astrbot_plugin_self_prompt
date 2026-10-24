"""Specify restart-safe reply counters and constrained proactive memory writes."""

import json

import pytest
from astrbot.core.agent.message import Message, dump_messages_with_checkpoints
from astrbot_plugin_self_prompt.main import MEMORY_REMINDER
from astrbot_plugin_self_prompt.prompt_document import ensure_layout, read_sections
from astrbot_plugin_self_prompt.store import Store
from astrbot_plugin_self_prompt.tests.support import Sandbox


def test_hundred_round_reminder_resets_without_an_edit_and_survives_restart(tmp_path):
    store = Store(tmp_path / "memory.db")
    for number in range(1, 100):
        assert not store.reply_round("room", "p", str(number), 100)
    assert Store(store.path).progress("p") == [{"origin": "room", "rounds": 99}]
    assert store.reply_round("room", "p", "100", 100)
    assert store.progress("p")[0]["rounds"] == 0
    assert not store.reply_round("room", "p", "100", 100)
    for number in range(101, 200):
        assert not store.reply_round("room", "p", str(number), 100)
    assert Store(store.path).reply_round("room", "p", "200", 100)


def test_only_actual_model_changes_reset_shared_persona_counters(tmp_path):
    store = Store(tmp_path / "memory.db")
    for room, persona in (("a", "p"), ("b", "p"), ("a", "q")):
        store.reply_round(room, persona, "1", 100)
    unchanged = store.stage("p", "same", "same", "memory_tool")
    store.finish(unchanged)
    assert all(row["rounds"] == 1 for row in store.progress("p"))
    manual = store.stage("p", "old", "new", "manual")
    store.finish(manual)
    assert all(row["rounds"] == 1 for row in store.progress("p"))
    changed = store.stage("p", "old", "new", "nickname_tool")
    store.finish(changed)
    assert all(row["rounds"] == 0 for row in store.progress("p"))
    assert store.progress("q")[0]["rounds"] == 1
    store.reply_round("a", "p", "2", 100)
    changed = store.stage("p", "old", "new", "memory_tool")
    store.finish(changed)
    assert store.progress("p")[0]["rounds"] == 0


def test_old_other_heading_changes_without_rewriting_its_body():
    old = ensure_layout("Owner").replace("## 自我认知\n", "## 其他\n")
    updated = ensure_layout(old)
    assert "## 自我认知\n" in updated
    assert read_sections(old) == read_sections(updated)
    assert ensure_layout(updated) == updated


@pytest.mark.asyncio
async def test_reminder_is_temporary_and_tool_continuations_do_not_count_twice():
    s = await Sandbox().open()
    try:
        s.config["memory_reminder_interval"] = 1
        await s.plugin.remind_memory(s.event, s.request)
        await s.plugin.remind_memory(s.event, s.request)
        current = await s.request.assemble_context()
        assert json.dumps(current, ensure_ascii=False).count(MEMORY_REMINDER) == 1
        saved = dump_messages_with_checkpoints([Message.model_validate(current)])
        assert MEMORY_REMINDER not in json.dumps(saved, ensure_ascii=False)
        assert s.plugin.store.progress("default")[0]["rounds"] == 0
        assert not hasattr(s.plugin, "_summarize")
        assert not hasattr(s.plugin, "_worker")
        assert s.provider.calls == []
    finally:
        await s.close()


@pytest.mark.asyncio
async def test_lcm_gets_the_reminder_only_in_its_temporary_declaration():
    s = await Sandbox().open()
    try:
        s.config["memory_reminder_interval"] = 1
        s.plugin._lcm_managed = lambda: True
        event = s.message(2)
        req = await s.prepare(event)
        await s.plugin.remind_memory(event, req)
        assert MEMORY_REMINDER not in json.dumps(
            await req.assemble_context(), ensure_ascii=False
        )
        rules = s.plugin.lcm_context_rules()
        assert MEMORY_REMINDER in rules["temporary"](event)
        assert MEMORY_REMINDER not in rules["system"](event)
    finally:
        await s.close()


@pytest.mark.asyncio
async def test_self_memory_tool_preserves_protected_sections_and_rejects_stale_writes():
    s = await Sandbox().open()
    try:
        other_event = s.message(2)
        await s.prepare(other_event)
        before = read_sections((await s.manager.get_persona("default")).system_prompt)
        s.plugin.store.reply_round(
            s.event.unified_msg_origin, "default", "counted", 100
        )
        result = json.loads(
            await s.plugin.self_memory_update(s.event, "相处中形成的认识")
        )
        assert result["ok"] and result["changed"]
        after = read_sections((await s.manager.get_persona("default")).system_prompt)
        for key in ("persona", "output_preferences", "nicknames"):
            assert before[key] == after[key]
        assert s.plugin.store.progress("default")[0]["rounds"] == 0
        stale = json.loads(await s.plugin.self_memory_update(other_event, "过期结果"))
        assert stale["error"] == "content_changed"
        await s.plugin.get_self_prompt(other_event)
        assert json.loads(
            await s.plugin.self_memory_update(other_event, "合并后的认识")
        )["ok"]
        s.config["long_term_memory_max_bytes"] = 1
        too_long = json.loads(
            await s.plugin.self_memory_update(other_event, "超额正文")
        )
        assert too_long["error"] == "memory_budget_exceeded"
        assert s.provider.calls == []
    finally:
        await s.close()
