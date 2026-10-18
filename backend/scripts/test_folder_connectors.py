import asyncio
from backend.services.api.routers.news import _huntly_request, _unwrap

async def run():
    r = await _huntly_request("GET", "/api/connector/folder-connectors")
    print(f"Status: {r.status_code}")
    print(f"Raw body: {r.text}")

if __name__ == '__main__':
    asyncio.run(run())
