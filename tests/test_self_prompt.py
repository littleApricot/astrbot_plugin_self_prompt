"""Exercise the new plugin against native persona persistence and request objects."""

import copy
import json
import unittest
from unittest.mock import patch

from astrbot.core.astr_main_agent import _ensure_persona_and_skills
from astrbot.core.provider.register import llm_tools
from astrbot_plugin_self_prompt.core_memory import (
    current_memory,
    owner_edit,
    project_history,
)
from astrbot_plugin_self_prompt.main import SelfPrompt
from astrbot_plugin_self_prompt.prompt_document import (
    read_sections,
    replace_section,
)
from astrbot_plugin_self_prompt.tests.support import Sandbox, legacy_prompt


class PluginIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.s = await Sandbox().open()
        self.p = self.s.plugin

    async def asyncTearDown(self):
        await self.s.close()

    async def test_tools_and_request_have_no_owner_bookkeeping(self):
        self.assertFalse(hasattr(self.p, "update_self_prompt"))
        self.assertFalse(hasattr(self.p, "notebook_list"))
        for name in (
            "get_self_prompt",
            "nickname_update",
            "self_memory_update",
            "notebook_read",
            "notebook_create",
            "notebook_update",
            "notebook_delete",
        ):
            tool = llm_tools.get_func(name)
            self.assertIsNotNone(tool)
            wire = json.dumps(
                {"parameters": tool.parameters, "description": tool.description}
            )
            self.assertNotRegex(wire, r"revision|version_id|版本")
        memory = json.loads(await self.p.get_self_prompt(self.s.event))
        self.assertEqual(set(memory), {"ok", "sections"})
        self.assertEqual(
            set(memory["sections"]),
            {"persona", "output_preferences", "chat_examples", "nicknames", "other"},
        )
        self.assertNotIn("self_prompt:", self.s.request.system_prompt)
        self.assertNotIn("版本", self.s.request.system_prompt)
        req = self.s.request
        with patch("astrbot.core.astr_main_agent.SkillManager") as skills:
            skills.return_value.list_skills.return_value = []
            await _ensure_persona_and_skills(req, {}, self.s.context, self.s.event)
        self.assertIn("notebook_create", req.func_tool.names())
        self.assertNotIn("update_self_prompt", req.func_tool.names())
        self.assertNotIn("notebook_list", req.func_tool.names())

    async def test_legacy_other_memory_remains_resident_without_loss(self):
        old = legacy_prompt(persona="Nickname Echo", memory="原文" * 9000)
        # Supply a legacy prompt as if it had existed before plugin installation.
        await self.p._original_update("default", system_prompt=old)
        await self.p._ensure_layout("default")
        current = (await self.s.manager.get_persona("default")).system_prompt
        self.assertEqual(read_sections(current)["other"], "原文" * 9000)
        pages = self.p.store.catalog("default", self.p.settings())["pages"]
        self.assertEqual(len(pages), 0)
        await self.p._ensure_layout("default")
        self.assertEqual(
            len(self.p.store.catalog("default", self.p.settings())["pages"]), 0
        )
        request = await self.s.prepare(self.s.message(2))
        self.assertIn("原文原文", request.system_prompt)

    async def test_notebook_guidance_is_only_in_reply_tool_definitions(self):
        await self.p.notebook_create(
            self.s.event, "流程", "处理故障时读取", "private page body"
        )
        req = await self.s.prepare(self.s.message(2))
        with patch("astrbot.core.astr_main_agent.SkillManager") as skills:
            skills.return_value.list_skills.return_value = []
            await _ensure_persona_and_skills(req, {}, self.s.context, self.s.event)
        schemas = {
            row["function"]["name"]: row["function"]
            for row in req.func_tool.openai_schema()
        }
        message_text = req.system_prompt + json.dumps(
            [req.contexts, await req.assemble_context()], ensure_ascii=False
        )
        for name, guidance in {
            "notebook_read": "根据本轮目录的页面名称和描述主动读取正文",
            "notebook_create": "遇到值得记住的事实",
            "notebook_update": "整体替换正文前分段读完整页",
            "notebook_delete": "释放页数",
        }.items():
            self.assertIn(guidance, schemas[name]["description"])
            self.assertNotIn(guidance, message_text)
        self.assertNotIn("[记忆与记事本]", message_text)
        self.assertIn("流程", message_text)
        self.assertNotIn("private page body", message_text)

    async def test_upgrade_does_not_require_free_notebook_pages(self):
        self.s.config["notebook_max_pages"] = 1
        old = legacy_prompt(persona="Core", memory="x" * 13000)
        await self.p._original_update("default", system_prompt=old)
        await self.p._ensure_layout("default")
        self.assertEqual(
            read_sections((await self.s.manager.get_persona("default")).system_prompt)[
                "other"
            ],
            "x" * 13000,
        )
        self.assertEqual(
            self.p.store.catalog("default", self.p.settings())["page_count"], 0
        )

    async def test_only_directory_is_injected_and_reply_history_kept(self):
        saved = json.loads(
            await self.p.notebook_create(
                self.s.event, "流程", "处理故障时读取", "private page body"
            )
        )
        event = self.s.message(2)
        req = await self.s.prepare(event)
        req.contexts = [{"role": "user", "content": "old full reply context"}]
        await self.p.prepare_prompt_tools(event, req)
        self.assertIn("流程", req.extra_user_content_parts[0]["text"])
        self.assertNotIn("private page body", str(req.extra_user_content_parts))
        self.assertEqual(len(req.extra_user_content_parts), 1)
        self.assertEqual(
            req.contexts, [{"role": "user", "content": "old full reply context"}]
        )
        self.assertTrue(req.extra_user_content_parts[0]["_no_save"])
        data = json.loads(await self.p.notebook_read(event, saved["page_id"]))
        self.assertEqual(data["content"], "private page body")

    async def test_tools_read_update_delete_and_concurrent_editor(self):
        created = json.loads(
            await self.p.notebook_create(
                self.s.event, "Facts", "Facts for Alice", "Initial fact"
            )
        )
        page_id = created["page_id"]
        other = self.s.message(2)
        await self.s.prepare(other)
        result = json.loads(
            await self.p.notebook_update(other, page_id, content="No read")
        )
        self.assertEqual(result["error"], "read_page_first")
        await self.p.notebook_read(other, page_id)
        await self.p.notebook_update(self.s.event, page_id, content="New fact")
        result = json.loads(
            await self.p.notebook_update(other, page_id, content="Stale fact")
        )
        self.assertEqual(result["error"], "page_changed")
        await self.p.notebook_read(other, page_id)
        self.assertTrue(json.loads(await self.p.notebook_delete(other, page_id))["ok"])
        self.assertEqual(
            self.p.store.catalog("default", self.p.settings())["page_count"], 0
        )
        self.assertEqual(
            json.loads(await self.p.notebook_read(other, page_id))["error"],
            "page_not_found",
        )

    async def test_pagination_requires_whole_page_before_replacement(self):
        page = json.loads(
            await self.p.notebook_create(
                self.s.event, "Long", "Long procedure", "x" * 8001
            )
        )
        event = self.s.message(2)
        await self.s.prepare(event)
        await self.p.notebook_read(event, page["page_id"])
        self.assertEqual(
            json.loads(
                await self.p.notebook_update(event, page["page_id"], content="new")
            )["error"],
            "read_page_first",
        )
        await self.p.notebook_read(event, page["page_id"], 4000)
        await self.p.notebook_read(event, page["page_id"], 8000)
        self.assertTrue(
            json.loads(
                await self.p.notebook_update(event, page["page_id"], content="complete")
            )["ok"]
        )

    async def test_create_after_delete_and_after_change_is_not_stale_cached_success(
        self,
    ):
        original = json.loads(
            await self.p.notebook_create(self.s.event, "A", "purpose", "one")
        )
        await self.p.notebook_delete(self.s.event, original["page_id"])
        second = json.loads(
            await self.p.notebook_create(self.s.event, "A", "purpose", "one")
        )
        self.assertNotEqual(original["page_id"], second["page_id"])
        await self.p.notebook_update(self.s.event, second["page_id"], content="two")
        third = json.loads(
            await self.p.notebook_create(self.s.event, "A", "purpose", "one")
        )
        self.assertNotEqual(second["page_id"], third["page_id"])

    async def test_projection_archives_original_fields_once(self):
        req = self.s.request
        req.contexts = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "c",
                        "function": {
                            "name": "update_self_prompt",
                            "arguments": '{"expected_revision":"old"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "c",
                "content": '{"revision":"old","ok":true}',
            },
        ]
        before = copy.deepcopy(req.contexts)
        await self.p.prepare_prompt_tools(self.s.event, req)
        await self.p.prepare_prompt_tools(self.s.event, req)
        self.assertNotIn("revision", json.dumps(req.contexts))
        with self.p.store._connect() as db:
            saved = [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT message FROM original_tool_messages ORDER BY rowid"
                )
            ]
        self.assertEqual(saved, before)

    async def test_native_editor_preserves_other_persona_fields(self):
        await self.s.manager.update_persona(
            "default", tools=["get_self_prompt"], skills=[], begin_dialogs=["hello"]
        )
        prompt = (await self.s.manager.get_persona("default")).system_prompt
        await self.s.manager.update_persona(
            "default", system_prompt=replace_section(prompt, "persona", "Name")
        )
        saved = await self.s.manager.get_persona("default")
        self.assertEqual(saved.tools, ["get_self_prompt"])
        self.assertEqual(saved.skills, [])
        self.assertEqual(saved.begin_dialogs, ["hello"])

    async def test_external_content_change_is_not_reported_as_unchanged(self):
        current = (await self.s.manager.get_persona("default")).system_prompt
        await self.p._original_update(
            "default",
            system_prompt=replace_section(current, "persona", "External edit"),
        )
        await self.p._ensure_layout("default")
        row = self.p.store.records("default")[0]
        self.assertEqual(row["kind"], "external")
        self.assertTrue(row["changed"])

    async def test_native_editor_saves_each_snapshot_and_keeps_preferences(self):
        original = (await self.s.manager.get_persona("default")).system_prompt
        first = owner_edit(
            original,
            {
                "persona": "First",
                "output_preferences": "Exact preference  \n",
                "nicknames": ["Echo"],
                "other": "",
            },
        )
        await self.s.manager.update_persona("default", system_prompt=first)
        second = replace_section(first, "persona", "Second")
        await self.s.manager.update_persona("default", system_prompt=second)
        manual = [
            self.p.store.record("default", row["id"])
            for row in self.p.store.records("default")
            if row["kind"] == "manual"
        ]
        self.assertEqual(
            [
                current_memory(row["content"])["sections"]["persona"]
                for row in manual[:2]
            ],
            ["Second", "First"],
        )
        self.assertEqual(
            current_memory(second)["sections"]["output_preferences"],
            "Exact preference  \n",
        )
        self.assertTrue(all(row["sources"] == [] for row in manual))

    async def test_legacy_history_projection_preserves_original_and_other_tools(self):
        content = json.dumps(
            {
                "ok": True,
                "revision": "private-hash",
                "sections": {"persona": "Keep this"},
            }
        )
        history = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "c",
                        "function": {"name": "get_self_prompt", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "c",
                "content": [{"type": "text", "text": content}],
            },
            {"role": "tool", "tool_call_id": "other", "content": content},
        ]
        before = copy.deepcopy(history)
        projected = project_history(history)
        self.assertNotIn("private-hash", projected[1]["content"][0]["text"])
        self.assertIn("Keep this", projected[1]["content"][0]["text"])
        self.assertEqual(projected[2], history[2])
        self.assertEqual(history, before)

    async def test_reload_restores_editor_hook_and_preserves_pending(self):
        self.p.store.append(
            self.s.event.unified_msg_origin,
            "default",
            "legacy-1",
            {"text": "old source"},
        )
        await self.p.terminate()
        self.assertIs(self.s.manager.update_persona, self.p._original_update)
        with patch(
            "astrbot_plugin_self_prompt.main.StarTools.get_data_dir",
            return_value=self.s.data,
        ):
            other = SelfPrompt(self.s.context, self.s.config)
        await other._ensure_layout("default")
        self.assertEqual(other.store.pending_count(), 1)
        await other.terminate()
