from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from app.plugins.secrets import plugin_secret_store

PLUGIN_ID = "115"
API_BASE = "https://proapi.115.com"
AUTH_BASE = "https://passportapi.115.com"
QR_STATUS_URL = "https://qrcodeapi.115.com/get/status/"
AUTH_CODES = {99, 401, 40140116}
RISK_CODES = {40140117, 40140118, 403, 429}


class Error115(RuntimeError):
    def __init__(self, code: int, message: str, *, risk_control: bool = False):
        super().__init__(message or f"115 API error {code}")
        self.code = int(code or 0)
        self.risk_control = risk_control


class Client115:
    _refresh_lock = asyncio.Lock()
    _request_lock = asyncio.Lock()
    _last_request_at = 0.0

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.access_token = str(config.get("access_token") or "")
        self.refresh_token = str(config.get("refresh_token") or "")
        self.timeout = max(5.0, min(float(config.get("timeout") or 20), 60.0))
        self.minimum_interval = max(0.2, min(float(config.get("api_min_interval") or 1.0), 10.0))
        self.user_agent = "NOOR/1.0 (115-open)"

    async def _throttle(self) -> None:
        async with self._request_lock:
            delay = self.minimum_interval - (time.monotonic() - self.__class__._last_request_at)
            if delay > 0:
                await asyncio.sleep(delay)
            self.__class__._last_request_at = time.monotonic()

    async def _http(self, method: str, url: str, *, auth: bool = False, **kwargs: Any) -> dict[str, Any]:
        await self._throttle()
        headers = {"Accept": "application/json", "User-Agent": self.user_agent, **kwargs.pop("headers", {})}
        if auth and self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False, trust_env=False) as client:
            response = await client.request(method, url, headers=headers, **kwargs)
        try:
            body = response.json()
        except ValueError as exc:
            raise Error115(response.status_code, "115 returned an invalid response") from exc
        if response.status_code >= 400:
            raise Error115(response.status_code, str(body.get("message") or body.get("error") or "115 request failed"), risk_control=response.status_code in RISK_CODES)
        if not isinstance(body, dict):
            raise Error115(0, "115 returned an unexpected response")
        return body

    @staticmethod
    def _unwrap(body: dict[str, Any], *, raw: bool = False) -> Any:
        code = int(body.get("code") or body.get("errno") or 0)
        failed = body.get("state") in {False, 0} and code != 0
        if failed or body.get("error"):
            message = str(body.get("message") or body.get("error") or "115 request failed")
            raise Error115(code, message, risk_control=code in RISK_CODES)
        return body if raw else body.get("data", body)

    async def refresh(self) -> None:
        if not self.refresh_token:
            raise Error115(401, "115 refresh token is missing")
        async with self._refresh_lock:
            latest = plugin_secret_store.get_all(PLUGIN_ID)
            if latest.get("access_token") and latest.get("access_token") != self.access_token:
                self.access_token = latest["access_token"]
                self.refresh_token = latest.get("refresh_token", self.refresh_token)
                return
            body = await self._http("POST", f"{AUTH_BASE}/open/refreshToken", data={"refresh_token": self.refresh_token})
            data = self._unwrap(body)
            access = str(data.get("access_token") or "")
            refresh = str(data.get("refresh_token") or "")
            if not access or not refresh:
                raise Error115(401, "115 token refresh returned incomplete credentials")
            plugin_secret_store.set(PLUGIN_ID, "access_token", access)
            plugin_secret_store.set(PLUGIN_ID, "refresh_token", refresh)
            self.access_token, self.refresh_token = access, refresh

    async def request(self, method: str, path: str, *, retry_auth: bool = True, raw: bool = False, **kwargs: Any) -> Any:
        try:
            body = await self._http(method, f"{API_BASE}{path}", auth=True, **kwargs)
            return self._unwrap(body, raw=raw)
        except Error115 as exc:
            if retry_auth and exc.code in AUTH_CODES and self.refresh_token:
                await self.refresh()
                return await self.request(method, path, retry_auth=False, raw=raw, **kwargs)
            raise

    async def user_info(self) -> dict[str, Any]:
        value = await self.request("GET", "/open/user/info")
        return value if isinstance(value, dict) else {}

    async def list_folder(self, folder_id: str, *, offset: int = 0, limit: int = 200) -> dict[str, Any]:
        body = await self.request("GET", "/open/ufile/files", raw=True, params={
            "cid": str(folder_id or "0"), "offset": max(0, offset), "limit": max(1, min(limit, 500)),
            "cur": 1, "show_dir": 1, "stdir": 1, "o": "user_utime", "asc": 0,
        })
        items = body.get("data") if isinstance(body.get("data"), list) else []
        return {"items": [normalize_file(item) for item in items if isinstance(item, dict)], "count": int(body.get("count") or len(items)), "offset": int(body.get("offset") or offset)}

    async def file_info(self, file_id: str) -> dict[str, Any]:
        value = await self.request("GET", "/open/folder/get_info", params={"file_id": file_id})
        if isinstance(value, list):
            value = value[0] if value else {}
        return normalize_file_info(value if isinstance(value, dict) else {})

    async def create_folder(self, parent_id: str, name: str) -> dict[str, str]:
        folder_name = str(name or "").strip()
        if not folder_name or "/" in folder_name or "\\" in folder_name:
            raise ValueError("invalid 115 folder name")
        value = await self.request("POST", "/open/folder/add", data={"pid": str(parent_id or "0"), "file_name": folder_name})
        if not isinstance(value, dict) or not value.get("file_id"):
            raise Error115(0, "115 did not return the created folder ID")
        return {"file_id": str(value.get("file_id") or ""), "name": str(value.get("file_name") or folder_name)}

    async def download_url(self, pick_code: str, *, user_agent: str = "") -> dict[str, Any]:
        effective_user_agent = str(user_agent or self.user_agent)[:512]
        value = await self.request("POST", "/open/ufile/downurl", data={"pick_code": pick_code}, headers={"User-Agent": effective_user_agent})
        if not isinstance(value, dict) or not value:
            raise Error115(0, "115 did not return a download URL")
        item = next(iter(value.values()))
        url = ((item or {}).get("url") or {}).get("url") if isinstance(item, dict) else ""
        if not url:
            raise Error115(0, "115 download URL is empty")
        return {"url": str(url), "file_name": str(item.get("file_name") or ""), "file_size": int(item.get("file_size") or 0), "sha1": str(item.get("sha1") or "")}


def normalize_file(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "file_id": str(item.get("fid") or item.get("file_id") or ""),
        "parent_id": str(item.get("pid") or item.get("parent_id") or ""),
        "name": str(item.get("fn") or item.get("file_name") or ""),
        "sha1": str(item.get("sha1") or "").upper(),
        "size": int(item.get("fs") or item.get("file_size") or 0),
        "pick_code": str(item.get("pc") or item.get("pick_code") or ""),
        "is_directory": str(item.get("fc") if item.get("fc") is not None else item.get("file_category")) == "0",
        "extension": str(item.get("ico") or "").lower().lstrip("."),
        "updated_at": int(item.get("uet") or item.get("upt") or item.get("user_utime") or 0),
    }


def normalize_file_info(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "file_id": str(item.get("file_id") or ""),
        "name": str(item.get("file_name") or ""),
        "sha1": str(item.get("sha1") or "").upper(),
        "size": int(item.get("size_byte") or item.get("file_size") or 0),
        "pick_code": str(item.get("pick_code") or ""),
        "paths": list(item.get("paths") or []),
    }
