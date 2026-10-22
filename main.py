"""Expose tools for maintaining the current AstrBot persona prompt."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools

from .auto_memory import SUMMARY_PROMPT, apply_summary, history_chunks
from .memory_queue import MemoryQueue
from .prompt_document import SECTIONS, read_sections, replace_section, revision

BATCH_SIZE = 100
INPUT_CHAR_LIMIT = 60_000

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

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context, config)
        self.config = config if config is not None else {}
        self._locks: dict[str, asyncio.Lock] = {}
        data = StarTools.get_data_dir("astrbot_plugin_self_prompt")
        self._backup_dir = data / "backups"
        self.memory_queue = MemoryQueue(data / "auto_memory.sqlite3")
        self._wake = asyncio.Event()
        self._worker: asyncio.Task | None = None
        self._retry_after: dict[str, float] = {}
        tool = context.get_llm_tool_manager().get_func("update_self_prompt")
        if tool is not None:
            tool.parameters["required"] = ["section", "content", "expected_revision"]
            tool.parameters["properties"]["section"]["enum"] = list(SECTIONS)

    async def initialize(self) -> None:
        """Resume complete persisted batches after startup."""
        if self.config.get("auto_memory_enabled", True):
            self._start_worker()

    def _start_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._process_queue())
        self._wake.set()

    async def _selected_persona(
        self,
        event: AstrMessageEvent,
        conversation=None,
    ) -> str | None:
        if conversation is None:
            manager = self.context.conversation_manager
            cid = await manager.get_curr_conversation_id(event.unified_msg_origin)
            if cid:
                conversation = await manager.get_conversation(
                    event.unified_msg_origin,
                    cid,
                )
        (
            persona_id,
            persona,
            _,
            _,
        ) = await self.context.persona_manager.resolve_selected_persona(
            umo=event.unified_msg_origin,
            conversation_persona_id=(conversation.persona_id if conversation else None),
            platform_name=event.get_platform_name(),
            provider_settings=self.context.get_config(event.unified_msg_origin).get(
                "provider_settings", {}
            ),
        )
        return persona_id if persona is not None else None

    def _append_history(
        self,
        event: AstrMessageEvent,
        persona_id: str,
        role: str,
        text: str,
    ) -> None:
        if not text.strip():
            return
        message_id = event.get_extra("self_prompt_message_id")
        if not message_id:
            message_id = str(
                getattr(event.message_obj, "message_id", "") or uuid.uuid4()
            )
            event.set_extra("self_prompt_message_id", message_id)
        self.memory_queue.append(
            event.unified_msg_origin,
            persona_id,
            f"{message_id}:{role}",
            {
                "role": role,
                "sender_id": event.get_sender_id()
                if role == "user"
                else event.get_self_id(),
                "sender_name": event.get_sender_name() if role == "user" else "bot",
                "time": datetime.now(timezone.utc).isoformat(),
                "text": text,
            },
            str(event.get_extra("selected_provider", "") or ""),
            event.get_extra("self_prompt_model"),
        )
        self._start_worker()

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE | filter.EventMessageType.PRIVATE_MESSAGE,
        priority=90_000,
    )
    async def collect_history(self, event: AstrMessageEvent) -> None:
        """Count incoming messages before reply admission, including silent ones."""
        if not self.config.get("auto_memory_enabled", True):
            return
        if event.get_sender_id() == event.get_self_id():
            return
        try:
            persona_id = await self._selected_persona(event)
            if persona_id:
                event.set_extra("self_prompt_persona_id", persona_id)
                self._append_history(
                    event, persona_id, "user", event.get_message_outline()
                )
        except Exception as exc:
            self.logger.warning("Memory history capture failed: %s", type(exc).__name__)

    @filter.on_llm_response()
    async def collect_reply(
        self, event: AstrMessageEvent, response: LLMResponse
    ) -> None:
        """Count one completed assistant reply, excluding tool calls and chunks."""
        if not self.config.get("auto_memory_enabled", True):
            return
        if (
            response.role != "assistant"
            or response.is_chunk
            or response.tools_call_name
        ):
            return
        text = response.completion_text or ""
        if response.result_chain:
            media = [
                f"[{part.type.value}]"
                for part in response.result_chain.chain
                if part.type.value != "Plain"
            ]
            text = " ".join(value for value in [text, *media] if value)
        persona_id = event.get_extra("self_prompt_persona_id")
        if persona_id:
            try:
                self._append_history(event, persona_id, "assistant", text)
            except Exception as exc:
                self.logger.warning(
                    "Memory reply capture failed: %s", type(exc).__name__
                )

    async def _process_queue(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            try:
                await self._process_ready()
            except Exception as exc:
                self.logger.warning("Memory queue failed: %s", type(exc).__name__)

    async def _process_ready(self) -> None:
        while self.config.get("auto_memory_enabled", True):
            progressed = False
            for scope in self.memory_queue.ready(BATCH_SIZE):
                if self._retry_after.get(scope["id"], 0) > time.monotonic():
                    continue
                try:
                    await self._summarize(scope)
                    self._retry_after.pop(scope["id"], None)
                    progressed = True
                except Exception as exc:
                    self._retry_after[scope["id"]] = time.monotonic() + 60
                    self.logger.warning(
                        "Automatic memory batch failed: %s", type(exc).__name__
                    )
            if not progressed:
                return

    async def _summarize(self, scope: dict) -> None:
        records = self.memory_queue.batch(scope["id"], BATCH_SIZE)
        if len(records) != BATCH_SIZE:
            return
        persona_id = scope["persona_id"]
        persona = await self.context.persona_manager.get_persona(persona_id)
        original = persona.system_prompt
        staged = original
        provider = (
            self.context.get_provider_by_id(scope["provider_id"])
            if scope["provider_id"]
            else await self.context.get_using_provider_async(scope["origin"])
        )
        if provider is None:
            raise ValueError("model_not_configured")
        pending = [records]
        while pending:
            sections = read_sections(staged)
            header = {
                "origin": scope["origin"],
                "persona_id": persona_id,
                "sections": sections,
            }
            overhead = (
                len(json.dumps(header, ensure_ascii=False)) + len(SUMMARY_PROMPT) + 1000
            )
            chunks = history_chunks(pending.pop(0), INPUT_CHAR_LIMIT - overhead)
            history, *remaining = chunks
            pending = [*remaining, *pending]
            response = await asyncio.wait_for(
                provider.text_chat(
                    prompt=json.dumps(
                        {**header, "history": history}, ensure_ascii=False
                    ),
                    system_prompt=SUMMARY_PROMPT,
                    contexts=[],
                    func_tool=None,
                    **({"model": scope["model"]} if scope["model"] else {}),
                ),
                timeout=max(1, int(self.config.get("auto_memory_timeout", 60))),
            )
            if response.role != "assistant" or response.tools_call_name:
                raise ValueError("invalid_model_response")
            staged = apply_summary(staged, response.completion_text)
        if not self.config.get("auto_memory_enabled", True):
            return
        async with self._locks.setdefault(persona_id, asyncio.Lock()):
            current = await self.context.persona_manager.get_persona(persona_id)
            if revision(current.system_prompt) != revision(original):
                raise ValueError("revision_conflict")
            if staged != original and not await self._persist(
                persona_id, original, staged
            ):
                raise ValueError("save_not_confirmed")
            self.memory_queue.complete(scope["id"], records[-1]["id"])
        self.logger.info(
            "Automatic memory batch completed: messages=%s changed=%s remaining=%s",
            len(records),
            staged != original,
            self.memory_queue.pending_count(),
        )

    async def _persist(self, persona_id: str, original: str, updated: str) -> bool:
        directory = self._backup_dir / hashlib.sha256(persona_id.encode()).hexdigest()
        directory.mkdir(parents=True, exist_ok=True)
        backup = directory / f"{revision(original)}.txt"
        if not backup.exists():
            backup.write_text(original, encoding="utf-8")
        saved = await self.context.persona_manager.update_persona(
            persona_id,
            system_prompt=updated,
        )
        return saved is not None and saved.system_prompt == updated

    async def terminate(self) -> None:
        """Cancel background work before plugin reload or shutdown."""
        if self._worker:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)

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
        persona_id = await self._selected_persona(event, req.conversation)
        if not persona_id:
            return
        event.set_extra("self_prompt_persona_id", persona_id)
        event.set_extra("self_prompt_model", req.model)
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
                if not await self._persist(persona_id, prompt, updated):
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
