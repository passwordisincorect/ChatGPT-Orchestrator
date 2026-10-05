import asyncio, json
from chatgpt_orchestrator import server

async def main():
    tools = await server.mcp.list_tools()
    rows = []
    for tool in tools:
        rows.append({
            "name": tool.name,
            "properties": sorted((tool.input_schema or {}).get("properties", {}).keys()),
        })
    print(json.dumps(rows, ensure_ascii=False, indent=2))

asyncio.run(main())
