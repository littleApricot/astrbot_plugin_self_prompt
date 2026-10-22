"""Expose tools for maintaining the current AstrBot persona prompt."""

from __future__ import annotations

import asyncio
import hashlib
import json

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools

from .prompt_document import SECTIONS, read_sections, replace_section, revision

TOOL_GUIDANCE = """
[自维护提示词]
你可以用 get_self_prompt 读取当前人格的提示词，用 update_self_prompt 修改指定部分。
三个部分分别是 persona（性格、人设、昵称等）、output_preferences（输出偏好要求）、
memory（其他需要长期记住的事情）。这些内容保存在当前人格中，共用该人格的会话共享。
遇到明确的人设、长期输出偏好、值得记住的事实或对旧信息的纠正时，可以主动维护。
修改前先读取最新内容和 revision，合并保留仍然有效的内容，删除已被纠正的旧信息，
避免重复记录；临时玩笑和一时的情绪不要直接当成永久设定。
update_self_prompt 的 content 是指定部分修改后的完整内容，其他部分会保留。
工具成功返回后才表示已经保存；新内容从下一次回复请求起自动加载。
[/自维护提示词]
""".strip()


class SelfPrompt(Star):
    """Edit the persona actually selected for the current request."""

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self._locks: dict[str, asyncio.Lock] = {}
        self._backup_dir = (
            StarTools.get_data_dir("astrbot_plugin_self_prompt") / "backups"
        )
        tool = context.get_llm_tool_manager().get_func("update_self_prompt")
        if tool is not None:
            tool.parameters["required"] = ["section", "content", "expected_revision"]
            tool.parameters["properties"]["section"]["enum"] = list(SECTIONS)

    @filter.on_llm_request(priority=100_000)
    async def prepare_prompt_tools(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """Bind tools to the resolved persona and explain the editing workflow.

        Args:
            event: Current message event.
            req: Fully assembled reply request.
        """
        event.set_extra("self_prompt_persona_id", None)
        if req.conversation is None:
            return
        (
            persona_id,
            persona,
            _,
            _,
        ) = await self.context.persona_manager.resolve_selected_persona(
            umo=event.unified_msg_origin,
            conversation_persona_id=req.conversation.persona_id,
            platform_name=event.get_platform_name(),
            provider_settings=self.context.get_config(event.unified_msg_origin).get(
                "provider_settings", {}
            ),
        )
        if not persona_id or persona is None:
            return
        event.set_extra("self_prompt_persona_id", persona_id)
        req.system_prompt = (req.system_prompt or "") + "\n\n" + TOOL_GUIDANCE

    @filter.llm_tool(name="get_self_prompt")
    async def get_self_prompt(self, event: AstrMessageEvent) -> str:
        """Read the current persona prompt's three editable sections and revision.

        Returns:
            JSON with ok, persona_id, revision and sections. Call this before editing;
            a legacy prompt appears in persona until the first successful update.
        """
        persona_id = event.get_extra("self_prompt_persona_id")
        if not persona_id:
            return json.dumps({"ok": False, "error": "no_editable_persona"})
        try:
            persona = await self.context.persona_manager.get_persona(persona_id)
            prompt = persona.system_prompt
            return json.dumps(
                {
                    "ok": True,
                    "persona_id": persona_id,
                    "revision": revision(prompt),
                    "sections": read_sections(prompt),
                },
                ensure_ascii=False,
            )
        except ValueError:
            return json.dumps({"ok": False, "error": "persona_or_document_invalid"})

    @filter.llm_tool(name="update_self_prompt")
    async def update_self_prompt(
        self,
        event: AstrMessageEvent,
        section: str,
        content: str,
        expected_revision: str,
    ) -> str:
        """Persist one part of your current persona prompt as temporary long-term memory.

        Args:
            section(string): Part to replace: persona for character, identity and
                nicknames; output_preferences for output requirements; memory for
                other facts worth remembering.
            content(string): Complete updated content for this section. Preserve
                valid existing information, merge corrections and remove duplicates.
                Empty text clears only this section.
            expected_revision(string): Latest revision returned by get_self_prompt.
                On revision_conflict, read again and merge before retrying.

        Returns:
            JSON with ok, revision and updated sections, or an error. Success means
            the prompt is persisted and takes effect on the next reply request.
        """
        persona_id = event.get_extra("self_prompt_persona_id")
        if not persona_id:
            return json.dumps({"ok": False, "error": "no_editable_persona"})
        if section not in SECTIONS:
            return json.dumps({"ok": False, "error": "unknown_section"})
        lock = self._locks.setdefault(persona_id, asyncio.Lock())
        async with lock:
            try:
                manager = self.context.persona_manager
                persona = await manager.get_persona(persona_id)
                prompt = persona.system_prompt
                current_revision = revision(prompt)
                if expected_revision != current_revision:
                    return json.dumps({"ok": False, "error": "revision_conflict"})
                updated = replace_section(prompt, section, content)
                # Back up before writing, and keep private persona text out of logs.
                directory = (
                    self._backup_dir / hashlib.sha256(persona_id.encode()).hexdigest()
                )
                directory.mkdir(parents=True, exist_ok=True)
                backup = directory / f"{current_revision}.txt"
                if not backup.exists():
                    backup.write_text(prompt, encoding="utf-8")
                saved = await manager.update_persona(persona_id, system_prompt=updated)
                if saved is None or saved.system_prompt != updated:
                    return json.dumps({"ok": False, "error": "save_not_confirmed"})
                self.logger.info("Updated self-prompt section: %s", section)
                return json.dumps(
                    {
                        "ok": True,
                        "revision": revision(updated),
                        "sections": read_sections(updated),
                        "effective": "next_request",
                    },
                    ensure_ascii=False,
                )
            except ValueError:
                return json.dumps({"ok": False, "error": "persona_or_document_invalid"})
            except Exception as exc:
                self.logger.error("Self-prompt update failed: %s", type(exc).__name__)
                return json.dumps({"ok": False, "error": "save_failed"})
