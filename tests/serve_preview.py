"""Serve an isolated UI harness backed by the real plugin API and SQLite."""

import asyncio
import json
import socket
from pathlib import Path

import uvicorn
from fastapi.staticfiles import StaticFiles

from .support import Sandbox, app_for


async def main():
    sandbox = await Sandbox().open()
    sandbox.plugin._data = sandbox.root / "data/plugin_data/astrbot_plugin_self_prompt"
    sandbox.plugin._data.mkdir(parents=True)
    gate = (
        sandbox.plugin._data.parent.parent
        / "config/astrbot_plugin_reply_gate_config.json"
    )
    gate.parent.mkdir(exist_ok=True)
    gate.write_text(json.dumps({"context_budget": 60000}))
    sandbox.config["auto_memory_message_interval"] = 2
    await sandbox.plugin.collect_history(
        sandbox.message(11, text="这是本次整理的第一条原文。")
    )
    await sandbox.plugin.collect_history(sandbox.message(12, text="没有新增长期约定。"))
    await sandbox.plugin._process_ready()
    app = app_for(sandbox)
    app.mount(
        "/preview",
        StaticFiles(
            directory=Path(__file__).resolve().parents[1] / "views/manage", html=True
        ),
    )
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    print(f"PREVIEW_PORT={port}", flush=True)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    try:
        await server.serve(sockets=[sock])
    finally:
        await sandbox.close()


if __name__ == "__main__":
    asyncio.run(main())
