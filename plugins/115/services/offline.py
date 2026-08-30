from __future__ import annotations

from typing import Any

from .client import Client115


STATUS_MAP = {-1: "failed", 0: "queued", 1: "downloading", 2: "completed"}


def normalize_task(item: dict[str, Any]) -> dict[str, Any]:
    status_code = int(item.get("status") or 0)
    return {
        "info_hash": str(item.get("info_hash") or ""),
        "name": str(item.get("name") or ""),
        "status": STATUS_MAP.get(status_code, "failed"),
        "progress": max(0, min(int(item.get("percentDone") or 0), 100)),
        "size": int(item.get("size") or 0),
        "file_id": str(item.get("file_id") or ""),
        "target_directory_id": str(item.get("wp_path_id") or ""),
        "updated_at": int(item.get("last_update") or 0),
        "error": "" if status_code in {0, 1, 2} else "115 离线任务失败",
    }


async def add_urls(client: Client115, urls: list[str], directory_id: str) -> list[str]:
    value = await client.request("POST", "/open/offline/add_task_urls", data={"urls": "\n".join(urls), "wp_path_id": directory_id})
    rows = value if isinstance(value, list) else []
    hashes = [str(row.get("info_hash") or "") for row in rows if isinstance(row, dict) and row.get("state") and row.get("info_hash")]
    if not hashes:
        message = next((str(row.get("message") or "") for row in rows if isinstance(row, dict) and row.get("message")), "")
        raise ValueError(message or "115 未创建离线任务")
    return hashes


async def list_remote_tasks(client: Client115, *, max_pages: int = 5) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for page in range(1, max(1, min(max_pages, 20)) + 1):
        value = await client.request("GET", "/open/offline/get_task_list", params={"page": page})
        rows = value.get("tasks") if isinstance(value, dict) else []
        out.extend(normalize_task(row) for row in rows or [] if isinstance(row, dict))
        if not isinstance(value, dict) or page >= int(value.get("page_count") or 1):
            break
    return out
