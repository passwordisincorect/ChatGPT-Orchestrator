from __future__ import annotations

import unittest

from chatgpt_orchestrator import server


class RoutingMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_autonomous_entrypoints_advertise_complex_role(self):
        tools = await server.mcp.list_tools()
        by_name = {tool.name: tool for tool in tools}

        auto = by_name["task_auto_start"].description or ""
        plan = by_name["task_plan"].description or ""

        self.assertIn("independent workers", auto.lower())
        self.assertIn("ChatGPT-Actuator", auto)
        self.assertIn("complex goal", plan.lower())
        self.assertIn("ChatGPT-Actuator", plan)

    async def test_project_state_tools_are_exposed(self):
        tools = await server.mcp.list_tools()
        by_name = {tool.name: tool for tool in tools}

        for name in (
            "project_create",
            "project_list",
            "project_get",
            "project_update",
            "project_attach_task",
        ):
            self.assertIn(name, by_name)

        auto_schema = by_name["task_auto_start"].input_schema
        self.assertIn("project_id", auto_schema.get("properties", {}))


if __name__ == "__main__":
    unittest.main()
