"""Use native AstrBot persona persistence and transport in isolated test directories."""

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.api import AstrBotConfig
from astrbot.core.db.sqlite import SQLiteDatabase
from astrbot.core.persona_mgr import PersonaManager
from astrbot.core.provider.entities import LLMResponse, ProviderRequest
from astrbot.core.provider.register import llm_tools
from astrbot_plugin_self_prompt.main import SelfPrompt


def legacy_prompt(
    persona="Original persona",
    preferences="Short",
    memory="",
    nicknames="",
    fixed="Owner notes",
    include_nicknames=True,
):
    """Build a genuine v2 document for future upgrade checks."""
    values = {"persona": persona, "output_preferences": preferences, "memory": memory}
    if include_nicknames:
        values["nicknames"] = (
            json.dumps(nicknames, ensure_ascii=False)
            if isinstance(nicknames, list)
            else nicknames
        )
    body = "\n\n".join(
        f"<!-- self_prompt:{key} -->\n{value}\n<!-- /self_prompt:{key} -->"
        for key, value in values.items()
    )
    return f"# 你维护的固定设定\n<!-- self_prompt:fixed:v2 -->\n{fixed}\n<!-- /self_prompt:fixed:v2 -->\n\n# 机器人可维护内容\n<!-- self_prompt:editable:v2 -->\n{body}\n<!-- /self_prompt:editable:v2 -->"


class CompletionInput:
    """Control only completion output; run capture, validation and saving normally."""

    def __init__(self, text='{"updates":{}}'):
        self.text = text
        self.calls = []

    async def text_chat(self, **kwargs):
        self.calls.append(kwargs)
        if callable(self.text):
            return await self.text(**kwargs)
        return LLMResponse("assistant", completion_text=self.text)


class Sandbox:
    async def open(self):
        self.temp = tempfile.TemporaryDirectory(prefix="astrbot-memory-test-")
        self.root = Path(self.temp.name)
        self.db = SQLiteDatabase(str(self.root / "personas.db"))
        self.conf = {
            "agent_runner": {
                "runner_type": "local",
                "config": {"persona": {"persona_id": "default"}},
            },
            "provider_settings": {},
            "timezone": "Asia/Shanghai",
        }
        self.acm = SimpleNamespace(
            default_conf=self.conf, get_conf=lambda *args, **kwargs: self.conf
        )
        self.manager = PersonaManager(self.db, self.acm)
        await self.manager.initialize()
        self.overrides = {}
        self.preference_patch = patch(
            "astrbot.core.persona_mgr.sp.get_async",
            AsyncMock(side_effect=lambda **kwargs: self.overrides),
        )
        self.preference_patch.start()
        self.provider = CompletionInput()

        async def using_provider(*args):
            return self.provider

        async def current_conversation(*args):
            return None

        self.context = SimpleNamespace(
            persona_manager=self.manager,
            get_config=lambda *args, **kwargs: self.conf,
            get_llm_tool_manager=lambda: llm_tools,
            subagent_orchestrator=None,
            registered_web_apis=[],
            get_using_provider_async=using_provider,
            get_provider_by_id=lambda *args: self.provider,
            conversation_manager=SimpleNamespace(
                get_curr_conversation_id=current_conversation
            ),
        )
        self.context.register_web_api = lambda path, fn, methods, desc: (
            self.context.registered_web_apis.append((path, fn, methods, desc))
        )
        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text()
        )
        self.config = AstrBotConfig(str(self.root / "config.json"), schema=schema)
        self.data = self.root / "plugin_data"
        with patch(
            "astrbot_plugin_self_prompt.main.StarTools.get_data_dir",
            return_value=self.data,
        ):
            self.plugin = SelfPrompt(self.context, self.config)
        self.no_worker = patch.object(self.plugin, "_start_worker")
        self.no_worker.start()
        await self.plugin.initialize()
        self.event = self.message(1)
        self.request = await self.prepare(self.event)
        return self

    def message(self, number, text=None, origin="test:GroupMessage:room"):
        extra = {}
        return SimpleNamespace(
            unified_msg_origin=origin,
            message_obj=SimpleNamespace(message_id=str(number)),
            get_platform_name=lambda: "qq_official",
            get_sender_id=lambda: "user-1",
            get_self_id=lambda: "bot-1",
            get_sender_name=lambda: "Alice",
            get_message_outline=lambda: text or f"message {number}",
            get_messages=lambda: [],
            get_extra=lambda key, default=None: extra.get(key, default),
            set_extra=lambda key, value: extra.__setitem__(key, value),
        )

    async def prepare(self, event):
        persona = await self.manager.get_persona("default")
        request = ProviderRequest(
            prompt="hello",
            system_prompt="# Persona Instructions\n" + persona.system_prompt,
            conversation=SimpleNamespace(persona_id=None),
        )
        await self.plugin.prepare_prompt_tools(event, request)
        return request

    async def close(self):
        await self.plugin.terminate()
        self.no_worker.stop()
        self.preference_patch.stop()
        await self.db.engine.dispose()
        self.temp.cleanup()


def app_for(sandbox):
    """Run the actual native plugin-extension dispatcher without starting adapters."""
    from astrbot.dashboard.api.plugins import _call_plugin_extension
    from fastapi import FastAPI, Request

    app = FastAPI()
    app.state.core_lifecycle = SimpleNamespace(star_context=sandbox.context)

    @app.api_route("/api/v1/plugins/extensions/{path:path}", methods=["GET", "POST"])
    async def dispatch(path: str, request: Request):
        # This test credential is local to the isolated harness.
        username = (
            "test-owner"
            if request.headers.get("authorization") == "Bearer test-owner"
            else None
        )
        return await _call_plugin_extension(path, request, username)

    return app
