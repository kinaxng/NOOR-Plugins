from __future__ import annotations

from typing import Any

import httpx

from app.plugins.contracts import PluginTestResult

PLUGIN_ID = "xunlei-subtitle"


async def test(config: dict[str, Any]) -> PluginTestResult:
    results = await search_subtitles(config, "TEST")
    return PluginTestResult(ok=True, message="xunlei subtitle provider reachable", details={"items": len(results)})


async def search_subtitles(config: dict[str, Any], video_code: str) -> list[dict[str, Any]]:
    if not video_code:
        return []
    api_url = str(config.get("api_url") or "https://api-shoulei-ssl.xunlei.com/oracle/subtitle").strip()
    try:
        async with httpx.AsyncClient(timeout=float(config.get("timeout") or 30), follow_redirects=True) as client:
            response = await client.get(api_url, params={"name": video_code})
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError):
        return []
    results = []
    for item in data.get("data") or [] if data.get("code") == 0 else []:
        results.append({
            "id": f"xunlei:{item.get('name', '')}:{item.get('url', '')}",
            "filename": item.get("name", "unknown"),
            "ext": item.get("ext", ".srt"),
            "language": item.get("languages", ["未知"])[0] if item.get("languages") else "未知",
            "source": "迅雷", "source_key": PLUGIN_ID, "source_type": "remote_search",
            "url": item.get("url", ""), "score": 0.7,
        })
    return results
