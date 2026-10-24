"""Provide current memory, a writable notebook and owner-only source records."""

from __future__ import annotations

import asyncio
import contextvars
import json
import time
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools

from .auto_memory import SUMMARY_PROMPT, apply_summary, history_chunks
from .core_memory import current_memory, editable_bytes, project_history, render_current
from .nicknames import nickname_text, read_nicknames, validate_nicknames
from .prompt_document import (
    assert_editable_change,
    ensure_layout,
    read_sections,
    replace_section,
)
from .settings import Settings
from .store import Store, catalog_text, digest, page_stamp
from .upgrade_backup import backup_legacy

WRITE_CONTEXT = contextvars.ContextVar("self_prompt_write", default=None)
INPUT_CHAR_LIMIT = 60_000

ERRORS = {
    "no_persona": "当前没有可用的人格。",
    "page_not_found": "页面不存在。",
    "page_changed": "内容已变化，请重新读取后再操作。",
    "read_page_first": "请先读取当前页面，替换正文前需读完全文。",
    "invalid_page_content": "请检查页面内容和长度。",
    "invalid_pagination": "请从头读取并检查分页范围。",
    "notebook_full": "记事本页数已满，请整理已有页面。",
    "catalog_budget_exceeded": "目录过长，请缩短名称和描述或合并主题。",
    "invalid_settings": "插件配置无效，请联系管理员检查。",
    "storage_error": "操作未确认成功，请稍后重试。",
    "invalid_nicknames": "昵称必须是非空字符串组成的列表。",
    "nickname_action_required": "至少提供要添加或移除的昵称。",
    "content_changed": "内容已变化，请重新读取后重试。",
    "memory_budget_exceeded": "常驻内容已超过字节上限，请精简后重试。",
}


class SelfPrompt(Star):
    """Keep memory writes separate from ordinary notebook tools."""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context, config)
        self.config = config if config is not None else {}
        self._data = StarTools.get_data_dir("astrbot_plugin_self_prompt")
        self._data.mkdir(parents=True, exist_ok=True)
        backup_legacy(self._data, self.config)
        self.store = Store(self._data / "auto_memory.sqlite3")
        self.memory_queue = self.store
        self._locks = {}
        self._wake = asyncio.Event()
        self._worker = None
        self._retry_after = {}
        self._active = True
        self._manager = context.persona_manager
        self._original_update = self._manager.update_persona
        self._installed_update = self._tracked_update
        self._manager.update_persona = self._installed_update
        manager = context.get_llm_tool_manager()
        for name, required in {
            "get_self_prompt": [],
            "nickname_update": [],
            "notebook_read": ["page_id"],
            "notebook_create": ["title", "description", "content"],
            "notebook_update": ["page_id"],
            "notebook_delete": ["page_id"],
        }.items():
            tool = manager.get_func(name)
            if tool is not None:
                tool.parameters["required"] = required
                if name == "notebook_read":
                    for key in ("offset", "limit"):
                        tool.parameters["properties"][key]["type"] = "integer"
                    tool.parameters["properties"]["offset"]["minimum"] = 0
                    tool.parameters["properties"]["limit"].update(
                        minimum=1, maximum=4000
                    )
                elif name == "nickname_update":
                    for key in ("add", "remove"):
                        tool.parameters["properties"][key].update(
                            type="array", items={"type": "string"}
                        )
        # Older tool registrations can remain during a hot reload.
        for name in ("update_self_prompt", "notebook_list"):
            old = manager.get_func(name)
            if old is not None:
                old.active = False
        from .dashboard import Dashboard

        self.dashboard = Dashboard(self)

    def settings(self):
        return Settings.read(self.config)

    def _lcm_managed(self):
        lookup = getattr(self.context, "get_registered_star", None)
        star = lookup("astrbot_plugin_lcm") if callable(lookup) else None
        plugin = getattr(star, "star_cls", None)
        return bool(
            plugin
            and getattr(star, "activated", True)
            and getattr(plugin, "config", {}).get("enabled", True)
            and callable(getattr(plugin, "context_rules", None))
        )

    def lcm_context_rules(self):
        def catalog(event):
            persona_id = event.get_extra("self_prompt_persona_id")
            return (
                catalog_text(self.store.catalog(persona_id, self.settings())["pages"])
                if persona_id
                else ""
            )

        return {
            "owner": "astrbot_plugin_self_prompt",
            "system": lambda event: event.get_extra("self_prompt_system_text", ""),
            "temporary": catalog,
            "history_tools": {
                "get_self_prompt": "keep",
                "nickname_update": "keep",
                "notebook_read": "keep",
                "notebook_create": "keep",
                "notebook_update": "keep",
                "notebook_delete": "keep",
            },
            "history_projector": lambda messages: list(
                enumerate(project_history(messages))
            ),
        }

    def _backup(self, persona_id, text):
        folder = self._data / "backups" / digest(persona_id)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = folder / (digest(text) + ".txt")
        if not path.exists():
            path.write_text(text, encoding="utf-8")
            path.chmod(0o600)

    async def _tracked_update(self, persona_id, *args, **kwargs):
        """Observe the same save entry used by the native persona editor."""
        if not self._active:
            return await self._original_update(persona_id, *args, **kwargs)
        requested = kwargs.get("system_prompt", args[0] if args else None)
        if requested is None:
            return await self._original_update(persona_id, *args, **kwargs)
        async with self._locks.setdefault(persona_id, asyncio.Lock()):
            current = await self._manager.get_persona(persona_id)
            original = current.system_prompt
            meta = WRITE_CONTEXT.get() or {"kind": "manual"}
            if "expected" in meta and original != meta["expected"]:
                raise ValueError("content_changed")
            normalized = ensure_layout(requested)
            if meta["kind"] == "nickname_tool":
                assert_editable_change(original, normalized)
                expected_target = replace_section(
                    original, "nicknames", read_sections(normalized)["nicknames"]
                )
                if normalized != expected_target:
                    raise ValueError("invalid_sections")
                notes, target = "", normalized
            else:
                notes = read_sections(normalized)["memory"]
                target = (
                    replace_section(normalized, "memory", "") if notes else normalized
                )
            limits = self.settings()
            if meta["kind"] in {"automatic", "nickname_tool"}:
                assert_editable_change(original, target)
                size = editable_bytes(target)
                if target != original and size > limits.memory_bytes:
                    raise ValueError("memory_budget_exceeded")
            self._backup(persona_id, original)
            if requested != original:
                self._backup(persona_id, requested)
            if notes:
                record_id = self.store.stage_migration(
                    persona_id,
                    original,
                    target,
                    notes,
                    limits,
                    meta=meta,
                )
            else:
                record_id = self.store.stage(
                    persona_id,
                    original,
                    target,
                    meta["kind"],
                    meta.get("sources"),
                    meta.get("scope"),
                    meta.get("last_id"),
                )
            arguments = list(args)
            if arguments:
                arguments[0] = target
                kwargs.pop("system_prompt", None)
            else:
                kwargs["system_prompt"] = target
            # Leave the prepared record intact if a save or readback is interrupted.
            saved = (
                current
                if meta["kind"] == "automatic" and target == original
                else await self._original_update(persona_id, *arguments, **kwargs)
            )
            actual = await self._manager.get_persona(persona_id)
            if saved is None or actual.system_prompt != target:
                raise ValueError("save_not_confirmed")
            self.store.finish(record_id)
            return saved

    async def _recover(self, persona_id):
        async with self._locks.setdefault(persona_id, asyncio.Lock()):
            for record in self.store.prepared(persona_id):
                persona = await self._manager.get_persona(persona_id)
                if persona.system_prompt == record["content"]:
                    self.store.finish(record["id"])
                elif persona.system_prompt == record["original"]:
                    saved = await self._original_update(
                        persona_id, system_prompt=record["content"]
                    )
                    actual = await self._manager.get_persona(persona_id)
                    if saved is None or actual.system_prompt != record["content"]:
                        raise ValueError("save_not_confirmed")
                    self.store.finish(record["id"])
                else:
                    self.store.fail(record["id"], "content_changed")

    async def _ensure_layout(self, persona_id):
        await self._recover(persona_id)
        persona = await self._manager.get_persona(persona_id)
        original = persona.system_prompt
        layout = ensure_layout(original)
        if layout != original or read_sections(layout)["memory"]:
            token = WRITE_CONTEXT.set({"kind": "migration", "expected": original})
            try:
                await self._manager.update_persona(persona_id, system_prompt=original)
            finally:
                WRITE_CONTEXT.reset(token)
        else:
            latest = self.store.latest_content(persona_id)
            if latest != original:
                record = self.store.stage(
                    persona_id,
                    original if latest is None else latest,
                    original,
                    "baseline" if latest is None else "external",
                )
                self.store.finish(record)

    async def initialize(self):
        for persona in list(self._manager.personas):
            try:
                await self._ensure_layout(persona.persona_id)
                folder = self._data / "backups" / digest(persona.persona_id)
                for path in folder.glob("*.txt"):
                    text = path.read_text(encoding="utf-8")
                    current_memory(text)
                    self.store.import_backup(persona.persona_id, text, path.name)
            except Exception as exc:
                self.logger.warning("记忆迁移失败：%s", type(exc).__name__)
        if self.settings().enabled:
            self._start_worker()

    def _start_worker(self):
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._process_queue())
        self._wake.set()

    async def _selected_persona(self, event, conversation=None):
        if conversation is None:
            manager = self.context.conversation_manager
            cid = await manager.get_curr_conversation_id(event.unified_msg_origin)
            if cid:
                conversation = await manager.get_conversation(
                    event.unified_msg_origin, cid
                )
        persona_id, persona, _, _ = await self._manager.resolve_selected_persona(
            umo=event.unified_msg_origin,
            conversation_persona_id=conversation.persona_id if conversation else None,
            platform_name=event.get_platform_name(),
            provider_settings=self.context.get_config(event.unified_msg_origin).get(
                "provider_settings", {}
            ),
        )
        return persona_id if persona is not None else None

    def _append_history(self, event, persona_id, role, text):
        if not text.strip():
            return
        message_id = event.get_extra("self_prompt_message_id")
        if not message_id:
            message_id = str(
                getattr(event.message_obj, "message_id", "") or uuid.uuid4()
            )
            event.set_extra("self_prompt_message_id", message_id)
        message = getattr(event, "message_obj", None)
        raw_time = getattr(message, "timestamp", None) if role == "user" else None
        timestamp = (
            datetime.fromtimestamp(raw_time, timezone.utc).isoformat()
            if isinstance(raw_time, (int, float))
            else datetime.now(timezone.utc).isoformat()
        )
        parts = []
        if role == "user" and hasattr(event, "get_messages"):
            for part in event.get_messages():
                try:
                    value = part.model_dump(mode="json", exclude_none=True)
                    # Do not embed binary data in source records.
                    value = {
                        key: val
                        for key, val in value.items()
                        if key not in {"base64", "data"}
                    }
                    parts.append(value)
                except (AttributeError, TypeError, ValueError):
                    parts.append({"type": str(getattr(part, "type", "unknown"))})
        self.store.append(
            event.unified_msg_origin,
            persona_id,
            f"{message_id}:{role}",
            {
                "role": role,
                "message_id": message_id,
                "origin": event.unified_msg_origin,
                "sender_id": event.get_sender_id()
                if role == "user"
                else event.get_self_id(),
                "sender_name": event.get_sender_name() if role == "user" else "bot",
                "time": timestamp,
                "text": text,
                "parts": parts,
            },
            str(event.get_extra("selected_provider", "") or ""),
            event.get_extra("self_prompt_model"),
        )
        self._start_worker()

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE | filter.EventMessageType.PRIVATE_MESSAGE,
        priority=90_000,
    )
    async def collect_history(self, event: AstrMessageEvent):
        """Capture original messages even when the reply gate remains silent."""
        if not self.settings().enabled or event.get_sender_id() == event.get_self_id():
            return
        try:
            persona_id = await self._selected_persona(event)
            if persona_id:
                event.set_extra("self_prompt_persona_id", persona_id)
                self._append_history(
                    event, persona_id, "user", event.get_message_outline()
                )
        except Exception as exc:
            self.logger.warning("记忆整理原文收集失败：%s", type(exc).__name__)

    @filter.on_llm_response()
    async def collect_reply(self, event: AstrMessageEvent, response: LLMResponse):
        """Count completed assistant output once, excluding tool calls and chunks."""
        if not self.settings().enabled:
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
            self._append_history(event, persona_id, "assistant", text)

    async def _process_queue(self):
        while True:
            await self._wake.wait()
            self._wake.clear()
            try:
                await self._process_ready()
            except Exception as exc:
                self.logger.warning("记忆整理队列处理失败：%s", type(exc).__name__)

    async def _process_ready(self):
        while self.settings().enabled:
            progressed = False
            for scope in self.store.ready(self.settings().interval):
                if self._retry_after.get(scope["id"], 0) > time.monotonic():
                    continue
                try:
                    await self._summarize(scope)
                    self._retry_after.pop(scope["id"], None)
                    progressed = True
                except Exception as exc:
                    self._retry_after[scope["id"]] = time.monotonic() + 60
                    self.logger.warning("自动记忆整理失败：%s", type(exc).__name__)
            if not progressed:
                return

    async def _summarize(self, scope):
        limits = self.settings()
        persona_id = scope["persona_id"]
        await self._ensure_layout(persona_id)
        records = self.store.batch(scope["id"], limits.interval)
        if len(records) < limits.interval:
            return
        sources = [*records, *self.store.notebook_sources(persona_id)]
        original = (await self._manager.get_persona(persona_id)).system_prompt
        staged = original
        provider = (
            self.context.get_provider_by_id(scope["provider_id"])
            if scope["provider_id"]
            else await self.context.get_using_provider_async(scope["origin"])
        )
        if provider is None:
            raise ValueError("model_not_configured")
        # Media references are retained for the owner, but only text went to this model.
        inputs = [
            {
                key: value
                for key, value in row.items()
                if key not in {"parts", "page_stamp"}
            }
            for row in sources
        ]
        zone = ZoneInfo(
            self.context.get_config(umo=scope["origin"]).get(
                "timezone", "Asia/Shanghai"
            )
            or "Asia/Shanghai"
        )
        for row in inputs:
            if row.get("time"):
                row["time"] = (
                    datetime.fromisoformat(row["time"].replace("Z", "+00:00"))
                    .astimezone(zone)
                    .strftime("%Y-%m-%d %H:%M:%S")
                )
        pending = [inputs]
        try:
            while pending:
                header = current_memory(staged)
                overhead = (
                    len(json.dumps(header, ensure_ascii=False))
                    + len(SUMMARY_PROMPT)
                    + 1000
                )
                history, *remaining = history_chunks(
                    pending.pop(0), INPUT_CHAR_LIMIT - overhead
                )
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
                    timeout=limits.timeout,
                )
                if response.role != "assistant" or response.tools_call_name:
                    raise ValueError("invalid_model_response")
                staged = apply_summary(staged, response.completion_text)
            if not self.settings().enabled:
                return
            token = WRITE_CONTEXT.set(
                {
                    "kind": "automatic",
                    "expected": original,
                    "sources": sources,
                    "scope": scope["id"],
                    "last_id": records[-1]["id"],
                }
            )
            try:
                await self._manager.update_persona(persona_id, system_prompt=staged)
            finally:
                WRITE_CONTEXT.reset(token)
        except Exception as exc:
            # Prepared saves are recovered separately; this entry reports the failed attempt.
            failed = self.store.stage(
                persona_id, original, original, "automatic", sources
            )
            safe = {
                "invalid_summary",
                "invalid_sections",
                "invalid_content",
                "invalid_section_content",
                "invalid_transfer",
                "invalid_model_response",
                "content_changed",
                "memory_budget_exceeded",
                "save_not_confirmed",
                "protected_prompt_too_large",
            }
            code = (
                str(exc)
                if isinstance(exc, ValueError) and str(exc) in safe
                else type(exc).__name__
            )
            self.store.fail(failed, code)
            raise
        self.logger.info(
            "自动记忆整理完成：结果=%s，消息数=%s，剩余消息数=%s",
            "已更新" if staged != original else "无变化",
            len(records),
            self.store.pending_count(),
        )

    async def terminate(self):
        self._active = False
        if self._worker:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        if self._manager.update_persona is self._installed_update:
            self._manager.update_persona = self._original_update
        self.dashboard.unregister()

    @filter.on_llm_request(priority=100_000)
    async def prepare_prompt_tools(self, event: AstrMessageEvent, req: ProviderRequest):
        """Provide current memory and the full notebook catalog before reply admission."""
        event.set_extra("self_prompt_persona_id", None)
        if req.conversation is None:
            return
        persona_id = await self._selected_persona(event, req.conversation)
        if not persona_id:
            return
        before = (await self._manager.get_persona(persona_id)).system_prompt
        await self._ensure_layout(persona_id)
        after = (await self._manager.get_persona(persona_id)).system_prompt
        rendered = render_current(after)
        event.set_extra("reply_gate_memory", rendered)
        event.set_extra("self_prompt_system_text", rendered)
        managed = self._lcm_managed()
        system = req.system_prompt or ""
        if before and before in system:
            system = system.replace(before, "" if managed else rendered, 1)
        elif after and after in system:
            system = system.replace(after, "" if managed else rendered, 1)
        elif not managed and rendered not in system:
            system += "\n\n" + rendered
        req.system_prompt = system
        if not managed:
            projected = project_history(req.contexts)
            self.store.archive_tool_projection(
                persona_id, event.unified_msg_origin, req.contexts, projected
            )
            req.contexts = projected
        event.set_extra("self_prompt_persona_id", persona_id)
        event.set_extra("self_prompt_model", req.model)
        if event.get_extra("self_prompt_request") != id(req):
            if not managed:
                catalog = self.store.catalog(persona_id, self.settings())
                req.extra_user_content_parts.append(
                    {
                        "type": "text",
                        "text": catalog_text(catalog["pages"]),
                        "_no_save": True,
                    }
                )
            event.set_extra("self_prompt_request", id(req))
            event.set_extra("self_prompt_reads", {})
            event.set_extra("self_prompt_creates", {})

    def _persona(self, event):
        persona_id = event.get_extra("self_prompt_persona_id")
        if not persona_id:
            raise ValueError("no_persona")
        return persona_id

    def _error(self, exc):
        code = str(exc) if isinstance(exc, ValueError) else "storage_error"
        if code not in ERRORS:
            code = "storage_error"
        return json.dumps(
            {"ok": False, "error": code, "message": ERRORS[code]}, ensure_ascii=False
        )

    @filter.llm_tool(name="get_self_prompt")
    async def get_self_prompt(self, event: AstrMessageEvent) -> str:
        """读取当前固定设定、人设、回复偏好和昵称列表。

        昵称可用 nickname_update 增删；
        黑话、事实、约定和流程通过记事本工具维护。
        """
        try:
            persona = await self._manager.get_persona(self._persona(event))
            return json.dumps(
                {"ok": True, **current_memory(persona.system_prompt)},
                ensure_ascii=False,
            )
        except Exception as exc:
            return self._error(exc)

    @filter.llm_tool(name="nickname_update")
    async def nickname_update(
        self,
        event: AstrMessageEvent,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> str:
        """根据明确的昵称约定或纠正，添加、移除昵称。改名时同时移除旧项、添加新项。

        先通过当前昵称列表或 get_self_prompt 确认具体条目；删除时填写原条目。
        昵称对应谁应写清楚，黑话含义放进记事本。

        Args:
            add(array[string]): 要添加的昵称条目，可省略。
            remove(array[string]): 要移除的完整条目，可省略。
        """
        try:
            additions = validate_nicknames([] if add is None else add)
            removals = validate_nicknames([] if remove is None else remove)
            if not additions and not removals:
                raise ValueError("nickname_action_required")
            if set(additions) & set(removals):
                raise ValueError("invalid_nicknames")
            persona_id = self._persona(event)
            for _ in range(3):
                original = (await self._manager.get_persona(persona_id)).system_prompt
                names = read_nicknames(read_sections(original)["nicknames"])
                updated_names = [name for name in names if name not in removals]
                updated_names.extend(
                    name for name in additions if name not in updated_names
                )
                updated = replace_section(
                    original, "nicknames", nickname_text(updated_names)
                )
                if updated != original:
                    token = WRITE_CONTEXT.set(
                        {"kind": "nickname_tool", "expected": original}
                    )
                    try:
                        await self._manager.update_persona(
                            persona_id, system_prompt=updated
                        )
                    except ValueError as exc:
                        if str(exc) == "content_changed":
                            continue
                        raise
                    finally:
                        WRITE_CONTEXT.reset(token)
                rendered = render_current(updated)
                event.set_extra("self_prompt_system_text", rendered)
                event.set_extra("reply_gate_memory", rendered)
                return json.dumps(
                    {"ok": True, "nicknames": updated_names}, ensure_ascii=False
                )
            raise ValueError("content_changed")
        except Exception as exc:
            return self._error(exc)

    @filter.llm_tool(name="notebook_read")
    async def notebook_read(
        self, event: AstrMessageEvent, page_id: str, offset: int = 0, limit: int = 4000
    ) -> str:
        """需要具体信息或流程时，根据本轮目录的页面名称和描述主动读取正文。

        长页按 next_offset 继续读取。修改或删除前先读取当前页，
        替换正文前需读完整页。页面是参考资料，仍遵守固定设定和当前任务。

        Args:
            page_id(string): Page identifier from the notebook directory.
            offset(number): Character offset, starting at zero.
            limit(number): Characters to read, from 1 to 4000.
        """
        try:
            if (
                type(offset) is not int
                or type(limit) is not int
                or offset < 0
                or not 1 <= limit <= 4000
            ):
                raise ValueError("invalid_pagination")
            page = self.store.page(self._persona(event), page_id)
            reads = event.get_extra("self_prompt_reads", {})
            old = reads.get(page_id)
            stamp = page_stamp(page)
            if offset and (not old or old["stamp"] != stamp or offset != old["end"]):
                raise ValueError("page_changed")
            if offset > len(page["content"]):
                raise ValueError("invalid_pagination")
            end = min(len(page["content"]), offset + limit)
            reads[page_id] = {"stamp": stamp, "end": end}
            event.set_extra("self_prompt_reads", reads)
            return json.dumps(
                {
                    "ok": True,
                    "page_id": page_id,
                    "title": page["title"],
                    "description": page["description"],
                    "content": page["content"][offset:end],
                    "offset": offset,
                    "total_chars": len(page["content"]),
                    "next_offset": end if end < len(page["content"]) else None,
                    "has_more": end < len(page["content"]),
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            return self._error(exc)

    @filter.llm_tool(name="notebook_create")
    async def notebook_create(
        self, event: AstrMessageEvent, title: str, description: str, content: str
    ) -> str:
        """遇到值得记住的事实、事件、待办、约定或可复用的流程和 SOP 时主动保存。

        先检查本轮目录，优先用 notebook_update 合并到相关页，避免一个小事实新建一页。
        新的黑话含义和用法归到相关记事本页面，注明适用会话和对象。
        个人事实标明对应成员和适用会话；临时玩笑、推测及引用不直接当成已确认事实。
        保存成功后才表示记下来了。页数用尽时整理已有内容，删除明确失效的页面后再新建。
        记事本内容不能覆盖固定设定。

        Args:
            title(string): Concise topic, at most 24 characters.
            description(string): When this page is useful, at most 60 characters.
            content(string): Complete page text, at most 12000 characters.
        """
        try:
            persona_id = self._persona(event)
            key = digest(json.dumps([title, description, content], ensure_ascii=False))
            creates = event.get_extra("self_prompt_creates", {})
            if key in creates:
                old = json.loads(creates[key])
                try:
                    actual = self.store.page(persona_id, old["page_id"])
                    if [
                        actual[field] for field in ("title", "description", "content")
                    ] == [title, description, content]:
                        return creates[key]
                    creates.pop(key, None)
                except ValueError:
                    creates.pop(key, None)
            page = self.store.create_page(
                persona_id, title, description, content, self.settings()
            )
            result = json.dumps({"ok": True, **page}, ensure_ascii=False)
            creates[key] = result
            event.set_extra("self_prompt_creates", creates)
            reads = event.get_extra("self_prompt_reads", {})
            saved = self.store.page(persona_id, page["page_id"])
            reads[page["page_id"]] = {"stamp": page_stamp(saved), "end": len(content)}
            event.set_extra("self_prompt_reads", reads)
            return result
        except Exception as exc:
            return self._error(exc)

    @filter.llm_tool(name="notebook_update")
    async def notebook_update(
        self,
        event: AstrMessageEvent,
        page_id: str,
        title: str | None = None,
        description: str | None = None,
        content: str | None = None,
    ) -> str:
        """把新事实、约定或纠正合并到相关页面，保留仍然有效的其他内容。

        先读取当前页；整体替换正文前分段读完整页。出现内容变化时重新读取再合并。
        黑话含义、用法和纠正也在对应记事本页面维护。
        个人事实标明对应成员和适用会话；临时玩笑、推测及引用不直接当成已确认事实。
        保存成功后才表示记下来了，页面内容仍遵守固定设定。

        Args:
            page_id(string): Page to edit. Read its current content first.
            title(string): New title, or omit to keep it.
            description(string): New description, or omit to keep it.
            content(string): Complete new body, or omit to keep it. Read all parts first.
        """
        try:
            persona_id = self._persona(event)
            reads = event.get_extra("self_prompt_reads", {})
            seen = reads.get(page_id)
            page = self.store.page(persona_id, page_id)
            if not seen or (content is not None and seen["end"] < len(page["content"])):
                raise ValueError("read_page_first")
            fields = {
                key: value
                for key, value in (
                    ("title", title),
                    ("description", description),
                    ("content", content),
                )
                if value is not None
            }
            saved = self.store.change_page(
                persona_id, page_id, seen["stamp"], fields, self.settings()
            )
            latest = self.store.page(persona_id, page_id)
            seen["stamp"] = page_stamp(latest)
            if content is not None:
                seen["end"] = len(content)
            return json.dumps({"ok": True, **saved}, ensure_ascii=False)
        except Exception as exc:
            return self._error(exc)

    @filter.llm_tool(name="notebook_delete")
    async def notebook_delete(self, event: AstrMessageEvent, page_id: str) -> str:
        """先读取，再删除明确失效或已经合并到其他页的页面，释放页数。

        仍然有效的信息先合并保存，成功返回后才表示删除完成。

        Args:
            page_id(string): Identifier of the current page to delete.
        """
        try:
            persona_id = self._persona(event)
            reads = event.get_extra("self_prompt_reads", {})
            seen = reads.get(page_id)
            if not seen:
                raise ValueError("read_page_first")
            result = self.store.change_page(
                persona_id, page_id, seen["stamp"], {}, self.settings(), delete=True
            )
            reads.pop(page_id, None)
            return json.dumps({"ok": True, **result}, ensure_ascii=False)
        except Exception as exc:
            return self._error(exc)
