"""Check the installed SDK and process protocol without cluster or model credentials."""
import asyncio
from types import SimpleNamespace

from web_app.workswarm import process_client


async def main():
    client = process_client(SimpleNamespace(api_key="", base_url="", model=""))
    response = await client.query("protocol.capabilities", deadline_seconds=15)
    if not response.get("data", {}).get("features", {}).get("host_tools"):
        raise RuntimeError("WorkSwarm process did not advertise host tool support")
    print("WorkSwarm offline SDK and host tool protocol OK")


if __name__ == "__main__":
    asyncio.run(main())
