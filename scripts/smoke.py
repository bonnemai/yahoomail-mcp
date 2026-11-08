"""Check MCP tool discovery, optionally testing Yahoo with a read-only call."""
import argparse
import asyncio
import os

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main(url, search, read_only):
    token = os.environ.get("YAHOO_MCP_TOKEN")
    if token and httpx.URL(url).scheme != "https" and httpx.URL(url).host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("Use HTTPS when sending a bearer token to a remote server.")
    headers = {"Authorization": f"Bearer {token}"} if token else None
    async with (
        httpx.AsyncClient(trust_env=False, timeout=45, headers=headers) as http_client,
        streamable_http_client(url, http_client=http_client) as (read, write, _),
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = {tool.name for tool in (await session.list_tools()).tools}
            expected = {"search_mail", "read_message", "read_thread"}
            if not read_only:
                expected.update({"save_draft", "trash_messages"})
            if names != expected:
                raise SystemExit(f"Unexpected tools: {sorted(names)}")
            print("MCP ready:", ", ".join(sorted(names)))
            if search:
                result = await session.call_tool("search_mail", {"limit": 1})
                if result.isError:
                    raise SystemExit("Yahoo search failed. Check server logs and Yahoo credentials.")
                print("Yahoo search OK. No email content printed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000/mcp")
    parser.add_argument("--search", action="store_true")
    parser.add_argument("--read-only", action="store_true", help="Expect only the three read tools.")
    args = parser.parse_args()
    asyncio.run(main(args.url, args.search, args.read_only))
