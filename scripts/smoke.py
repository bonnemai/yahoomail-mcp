"""Check MCP tool discovery, optionally testing Yahoo with a read-only call."""
import argparse
import asyncio

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main(url, search):
    async with (
        httpx.AsyncClient(trust_env=False, timeout=45) as http_client,
        streamable_http_client(url, http_client=http_client) as (read, write, _),
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = {tool.name for tool in (await session.list_tools()).tools}
            expected = {"search_mail", "read_message", "read_thread", "save_draft", "trash_messages"}
            if names != expected:
                raise SystemExit(f"Unexpected tools: {sorted(names)}")
            print("MCP ready:", ", ".join(sorted(names)))
            if search:
                result = await session.call_tool("search_mail", {"limit": 1})
                if result.isError:
                    raise SystemExit(f"Yahoo search failed: {result.content}")
                print("Yahoo search OK. No email content printed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000/mcp")
    parser.add_argument("--search", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.url, args.search))
