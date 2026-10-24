"""Authenticated owner endpoints, separate from all model tools."""

import json

from astrbot.api.web import error_response, json_response, request

from .core_memory import current_memory, editable_bytes, owner_edit, render_current
from .main import WRITE_CONTEXT
from .settings import CONFIG_KEYS, Settings
from .store import catalog_text, digest, page_stamp


class Dashboard:
    def __init__(self, plugin):
        self.plugin = plugin
        self.routes = []
        for route, handler, methods in (
            ("state", self.state, ["GET"]),
            ("page", self.page, ["GET", "POST"]),
            ("memory", self.memory, ["POST"]),
            ("records", self.records, ["GET"]),
            ("restore", self.restore, ["POST"]),
            ("settings", self.settings, ["POST"]),
        ):
            path = "/astrbot_plugin_self_prompt/" + route
            plugin.context.register_web_api(
                path, handler, methods, "Memory and notebook management"
            )
            self.routes.append((path, handler))

    def unregister(self):
        routes = getattr(self.plugin.context, "registered_web_apis", None)
        if isinstance(routes, list):
            self.plugin.context.registered_web_apis[:] = [
                row
                for row in routes
                if not any(
                    row[0] == path and row[1] == handler
                    for path, handler in self.routes
                )
            ]

    async def _run(self, operation):
        if not request.username:
            return error_response("请先登录 AstrBot", status_code=401)
        try:
            return json_response(await operation())
        except (ValueError, KeyError, TypeError) as exc:
            code = str(exc) if isinstance(exc, ValueError) else "invalid_request"
            messages = {
                "content_changed": "内容已被其他操作修改，请刷新后重试。",
                "page_changed": "页面内容已变化，请重新打开。",
                "notebook_full": "记事本页数已满。",
                "catalog_budget_exceeded": "完整目录超过字节预算，请缩短名称或描述。",
                "invalid_settings": "配置必须为有效的正整数，开关必须为布尔值。",
                "invalid_page_content": "页面名称、描述或正文不符合长度要求。",
                "record_not_found": "记忆记录不存在。",
                "page_not_found": "页面不存在。",
            }
            return error_response(
                messages.get(code, "保存未完成，请检查内容并刷新重试。"),
                status_code=409,
            )

    async def _persona(self, persona_id):
        if not isinstance(persona_id, str) or not persona_id:
            raise ValueError("no_persona")
        persona = await self.plugin._manager.get_persona(persona_id)
        if persona is None:
            raise ValueError("no_persona")
        await self.plugin._ensure_layout(persona_id)
        return await self.plugin._manager.get_persona(persona_id)

    async def state(self):
        async def operation():
            personas = [row.persona_id for row in self.plugin._manager.personas]
            persona_id = request.query.get("persona_id") or (
                personas[0] if personas else ""
            )
            persona = await self._persona(persona_id)
            limits = self.plugin.settings()
            catalog = self.plugin.store.catalog(persona_id, limits)
            memory = current_memory(persona.system_prompt)
            value = {
                "personas": personas,
                "persona_id": persona_id,
                "memory": memory,
                "stamp": digest(persona.system_prompt),
                "catalog": catalog,
                "progress": self.plugin.store.progress(persona_id),
                "settings": {
                    key: self.plugin.config.get(key, default)
                    for key, default in (
                        ("auto_memory_enabled", True),
                        ("auto_memory_message_interval", 100),
                        ("notebook_max_pages", 100),
                        ("auto_memory_timeout", 60),
                        ("long_term_memory_max_bytes", 8000),
                        ("notebook_catalog_max_bytes", 20000),
                    )
                },
                "decision_memory_bytes": len(
                    render_current(persona.system_prompt).encode()
                ),
                "memory_bytes": editable_bytes(persona.system_prompt),
                "protected_bytes": len(
                    (
                        render_current(persona.system_prompt)
                        + catalog_text(catalog["pages"])
                    ).encode()
                ),
                "time_zone": self.plugin.context.get_config().get(
                    "timezone", "Asia/Shanghai"
                ),
            }
            gate_path = (
                self.plugin._data.parent.parent
                / "config/astrbot_plugin_reply_gate_config.json"
            )
            if gate_path.is_file():
                try:
                    gate = json.loads(gate_path.read_text(encoding="utf-8-sig"))
                    value["decision_budget"] = int(gate.get("context_budget", 60000))
                except (ValueError, OSError, TypeError):
                    pass
            return value

        return await self._run(operation)

    async def page(self):
        async def operation():
            if request.method == "GET":
                persona = await self._persona(request.query.get("persona_id", ""))
                page = self.plugin.store.page(
                    persona.persona_id, request.query.get("page_id", "")
                )
                return {**page, "stamp": page_stamp(page)}
            body = await request.json(default={})
            if not isinstance(body, dict):
                raise ValueError("invalid_request")
            persona = await self._persona(body.get("persona_id"))
            limits = self.plugin.settings()
            if body.get("action") == "create":
                return self.plugin.store.create_page(
                    persona.persona_id,
                    body["title"],
                    body["description"],
                    body["content"],
                    limits,
                )
            return self.plugin.store.change_page(
                persona.persona_id,
                body["page_id"],
                body["stamp"],
                {
                    key: body[key]
                    for key in ("title", "description", "content")
                    if key in body
                },
                limits,
                delete=body.get("action") == "delete",
            )

        return await self._run(operation)

    async def memory(self):
        async def operation():
            body = await request.json(default={})
            if not isinstance(body, dict):
                raise ValueError("invalid_request")
            persona = await self._persona(body.get("persona_id"))
            if digest(persona.system_prompt) != body.get("stamp"):
                raise ValueError("content_changed")
            updated = owner_edit(persona.system_prompt, body["sections"])
            token = WRITE_CONTEXT.set(
                {"kind": "manual", "expected": persona.system_prompt}
            )
            try:
                await self.plugin._manager.update_persona(
                    persona.persona_id, system_prompt=updated
                )
            finally:
                WRITE_CONTEXT.reset(token)
            return {"ok": True}

        return await self._run(operation)

    async def records(self):
        async def operation():
            persona = await self._persona(request.query.get("persona_id", ""))
            record_id = request.query.get("record_id")
            if record_id:
                row = self.plugin.store.record(persona.persona_id, record_id)
                return {
                    key: row[key]
                    for key in (
                        "id",
                        "created_at",
                        "kind",
                        "state",
                        "changed",
                        "sources",
                        "error",
                    )
                } | {
                    "memory": current_memory(row["content"]),
                    "legacy_text": row["content"]
                    if row["kind"] == "legacy_backup"
                    else None,
                }
            offset = request.query.get("offset", 0, type=int)
            limit = request.query.get("limit", 30, type=int)
            if (
                not isinstance(offset, int)
                or offset < 0
                or not isinstance(limit, int)
                or not 1 <= limit <= 100
            ):
                raise ValueError("invalid_request")
            return {
                "records": self.plugin.store.records(persona.persona_id, offset, limit),
                "offset": offset,
            }

        return await self._run(operation)

    async def restore(self):
        async def operation():
            body = await request.json(default={})
            if not isinstance(body, dict):
                raise ValueError("invalid_request")
            persona = await self._persona(body.get("persona_id"))
            if digest(persona.system_prompt) != body.get("stamp"):
                raise ValueError("content_changed")
            record = self.plugin.store.record(persona.persona_id, body["record_id"])
            if record["state"] != "committed":
                raise ValueError("invalid_request")
            old = current_memory(record["content"])
            sections = old["sections"]
            if body.get("include_owner") is not True:
                current = current_memory(persona.system_prompt)["sections"]
                sections = {
                    **sections,
                    **{key: current[key] for key in ("persona", "output_preferences")},
                }
            updated = owner_edit(persona.system_prompt, sections)
            token = WRITE_CONTEXT.set(
                {
                    "kind": "restore",
                    "expected": persona.system_prompt,
                    "sources": record["sources"],
                }
            )
            try:
                await self.plugin._manager.update_persona(
                    persona.persona_id, system_prompt=updated
                )
            finally:
                WRITE_CONTEXT.reset(token)
            return {"ok": True}

        return await self._run(operation)

    async def settings(self):
        async def operation():
            body = await request.json(default={})
            if not isinstance(body, dict) or not set(body).issubset(CONFIG_KEYS):
                raise ValueError("invalid_settings")
            candidate = {**self.plugin.config, **body}
            limits = Settings.read(candidate)
            for persona in self.plugin._manager.personas:
                if (
                    self.plugin.store.catalog(persona.persona_id, limits)[
                        "catalog_bytes"
                    ]
                    > limits.catalog_bytes
                ):
                    raise ValueError("catalog_budget_exceeded")
            previous = dict(self.plugin.config)
            save = getattr(self.plugin.config, "save_config", None)
            if save is None:
                raise ValueError("config_persistence_unavailable")
            try:
                self.plugin.config.update(body)
                save()
            except Exception:
                self.plugin.config.clear()
                self.plugin.config.update(previous)
                raise
            if limits.enabled:
                self.plugin._start_worker()
            return {"ok": True}

        return await self._run(operation)
