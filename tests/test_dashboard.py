"""Exercise owner endpoints through AstrBot's native extension dispatcher."""

import json
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx
from astrbot_plugin_self_prompt.core_memory import render_current
from astrbot_plugin_self_prompt.tests.support import Sandbox, app_for


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.s = await Sandbox().open()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_for(self.s)),
            base_url="http://isolated.test",
            headers={"Authorization": "Bearer test-owner"},
        )
        self.prefix = "/api/v1/plugins/extensions/astrbot_plugin_self_prompt/"

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.s.close()

    async def get(self, endpoint, **params):
        return await self.client.get(self.prefix + endpoint, params=params)

    async def post(self, endpoint, **body):
        return await self.client.post(self.prefix + endpoint, json=body)

    async def test_native_transport_authentication_and_initial_selection(self):
        response = await self.client.get(
            self.prefix + "state", headers={"Authorization": ""}
        )
        self.assertEqual(response.status_code, 401)
        response = await self.get("state", persona_id="")
        self.assertEqual(response.status_code, 200)
        value = response.json()
        self.assertEqual(value["persona_id"], "default")
        self.assertEqual(value["time_zone"], "Asia/Shanghai")
        self.assertIn("stamp", value)
        persona = await self.s.manager.get_persona("default")
        self.assertEqual(
            value["protected_bytes"],
            len(render_current(persona.system_prompt).encode())
            + value["catalog"]["catalog_bytes"],
        )

    async def test_native_page_service_discovers_the_view(self):
        from astrbot.core.star.star import StarMetadata
        from astrbot.dashboard.services.plugin_page_service import PluginPageService

        folder = Path(__file__).resolve().parents[2]
        service = PluginPageService(
            SimpleNamespace(plugin_store_path=folder, reserved_plugin_path=folder)
        )
        pages = await service.discover_plugin_pages(
            StarMetadata(
                name="astrbot_plugin_self_prompt",
                root_dir_name="astrbot_plugin_self_prompt",
            )
        )
        self.assertEqual([page.name for page in pages], ["manage"])

    async def test_owner_edits_create_readable_memory_and_source_records(self):
        value = (await self.get("state")).json()
        response = await self.post(
            "memory",
            persona_id="default",
            stamp=value["stamp"],
            fixed_prompt="Owner fixed",
            sections={"persona": "Name: Echo", "output_preferences": "Short"},
        )
        self.assertEqual(response.status_code, 200)
        rows = (await self.get("records", persona_id="default")).json()["records"]
        row = next(row for row in rows if row["kind"] == "manual")
        detail = (
            await self.get("records", persona_id="default", record_id=row["id"])
        ).json()
        self.assertEqual(detail["memory"]["sections"]["persona"], "Name: Echo")
        self.assertEqual(detail["sources"], [])
        self.assertNotIn("diff", detail)

    async def test_page_crud_and_stale_save(self):
        response = await self.post(
            "page",
            persona_id="default",
            action="create",
            title="SOP",
            description="When handling errors",
            content="step one",
        )
        self.assertEqual(response.status_code, 200)
        page_id = response.json()["page_id"]
        full = (await self.get("page", persona_id="default", page_id=page_id)).json()
        self.assertEqual(
            (
                await self.post(
                    "page",
                    persona_id="default",
                    action="update",
                    page_id=page_id,
                    stamp=full["stamp"],
                    content="step two",
                )
            ).status_code,
            200,
        )
        stale = await self.post(
            "page",
            persona_id="default",
            action="delete",
            page_id=page_id,
            stamp=full["stamp"],
        )
        self.assertEqual(stale.status_code, 409)
        latest = (await self.get("page", persona_id="default", page_id=page_id)).json()
        self.assertEqual(
            (
                await self.post(
                    "page",
                    persona_id="default",
                    action="delete",
                    page_id=page_id,
                    stamp=latest["stamp"],
                )
            ).status_code,
            200,
        )

    async def test_config_is_persisted_and_interval_can_change(self):
        response = await self.post(
            "settings", auto_memory_message_interval=7, notebook_max_pages=9
        )
        self.assertEqual(response.status_code, 200)
        config = json.loads(
            (self.s.root / "config.json").read_text(encoding="utf-8-sig")
        )
        self.assertEqual(config["auto_memory_message_interval"], 7)
        self.assertEqual(config["notebook_max_pages"], 9)
        self.assertEqual(
            (await self.post("settings", notebook_max_pages=0)).status_code, 409
        )

    async def test_unchanged_result_has_source_and_owner_can_restore(self):
        self.s.config["auto_memory_message_interval"] = 1
        await self.s.plugin.collect_history(self.s.message(10, text="No new agreement"))
        await self.s.plugin._process_ready()
        rows = (await self.get("records", persona_id="default")).json()["records"]
        automatic = next(row for row in rows if row["kind"] == "automatic")
        self.assertFalse(automatic["changed"])
        detail = (
            await self.get("records", persona_id="default", record_id=automatic["id"])
        ).json()
        self.assertEqual(detail["sources"][0]["text"], "No new agreement")
        current = (await self.get("state")).json()
        await self.post(
            "memory",
            persona_id="default",
            stamp=current["stamp"],
            fixed_prompt="Latest fixed",
            sections={"persona": "New persona", "output_preferences": "short"},
        )
        latest = (await self.get("state")).json()
        response = await self.post(
            "restore",
            persona_id="default",
            stamp=latest["stamp"],
            record_id=automatic["id"],
            include_fixed=False,
        )
        self.assertEqual(response.status_code, 200)
        restored = (await self.get("state")).json()
        self.assertEqual(restored["memory"]["fixed_prompt"], "Latest fixed")
        self.assertEqual(restored["memory"]["sections"], detail["memory"]["sections"])
