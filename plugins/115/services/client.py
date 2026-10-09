from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import time
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from app.plugins.secrets import plugin_secret_store
from . import directory_changes

PLUGIN_ID = "115"
API_BASE = "https://proapi.115.com"
AUTH_BASE = "https://passportapi.115.com"
QR_STATUS_URL = "https://qrcodeapi.115.com/get/status/"
AUTH_CODES = {99, 401, 40140116}
ACCESS_LIMIT_CODES = {770004}
RISK_CODES = {40140117, 40140118, 403, 429, *ACCESS_LIMIT_CODES}


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
            result = self._unwrap(body, raw=raw)
            if method.upper() == "POST":
                directory_changes.notify_write(path, kwargs.get("data") or {})
            return result
        except Error115 as exc:
            message = str(exc).lower()
            if exc.risk_control:
                directory_changes.activate_cooldown()
            auth_failure = exc.code in AUTH_CODES or any(
                marker in message
                for marker in (
                    "access_token 无效",
                    "access_token 校验失败",
                    "access token invalid",
                    "invalid access token",
                    "token expired",
                    "token 已失效",
                )
            )
            if retry_auth and auth_failure and self.refresh_token:
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

    async def copy_file(self, file_id: str, parent_id: str, *, no_duplicate: bool = True) -> Any:
        """Copy a remote file/folder without downloading it locally."""
        return await self.request(
            "POST",
            "/open/ufile/copy",
            data={
                "pid": str(parent_id or "0"),
                "file_id": str(file_id),
                "no_dupli": "1" if no_duplicate else "0",
            },
        )

    async def move_file(self, file_id: str, parent_id: str) -> Any:
        return await self.request(
            "POST",
            "/open/ufile/move",
            data={"file_ids": str(file_id), "to_cid": str(parent_id or "0")},
        )

    async def rename_file(self, file_id: str, name: str) -> dict[str, Any]:
        safe_name = str(name or "").strip()
        if not safe_name or "/" in safe_name or "\\" in safe_name:
            raise ValueError("invalid 115 file name")
        value = await self.request(
            "POST",
            "/open/ufile/update",
            data={"file_id": str(file_id), "file_name": safe_name},
        )
        return value if isinstance(value, dict) else {"file_name": safe_name}

    async def delete_file(self, file_id: str, parent_id: str) -> Any:
        return await self.request(
            "POST",
            "/open/ufile/delete",
            data={"file_ids": str(file_id), "parent_id": str(parent_id or "0")},
        )

    async def upload_token(self) -> dict[str, Any]:
        value = await self.request("GET", "/open/upload/get_token")
        if not isinstance(value, dict):
            raise Error115(0, "115 did not return an upload token")
        return value

    async def upload_init(
        self,
        *,
        folder_id: str,
        name: str,
        content: bytes,
        sign_key: str = "",
        sign_val: str = "",
    ) -> dict[str, Any]:
        digest = hashlib.sha1(content).hexdigest().upper()
        value = await self.request(
            "POST",
            "/open/upload/init",
            data={
                "file_name": str(name),
                "file_size": str(len(content)),
                "target": f"U_1_{str(folder_id or '0')}",
                "fileid": digest,
                "topupload": "1",
                "sign_key": str(sign_key or ""),
                "sign_val": str(sign_val or ""),
            },
        )
        if not isinstance(value, dict):
            raise Error115(0, "115 returned an invalid upload initialization")
        value.setdefault("file_sha1", digest)
        value.setdefault("file_size", len(content))
        return value

    async def upload_bytes(self, folder_id: str, name: str, content: bytes) -> dict[str, Any]:
        """Upload a small sidecar through the same 115 Open API used by the plugin.

        Featured metadata is intentionally limited to sidecars and artwork.  A
        single OSS PUT is enough for these files and avoids introducing a
        second cloud-storage abstraction into the plugin layer.
        """
        if not content:
            raise ValueError("cannot upload an empty 115 file")
        if len(content) > 50 * 1024 * 1024:
            raise ValueError("115 sidecar upload exceeds 50 MiB")
        initialized = await self.upload_init(folder_id=folder_id, name=name, content=content)
        status = int(initialized.get("status") or 0)
        reused_status = status == 2 or (
            status == 8
            and bool(initialized.get("file_id") or initialized.get("pick_code"))
        )
        if reused_status:
            directory_changes.notify_write("upload_completed", {"folder_id": folder_id})
            return {**initialized, "uploaded": False, "reused": True}
        # The Open API normally uses 7 for a range challenge. Some accounts
        # return 8 without a completed file identity on the first request with
        # the same usable challenge fields. Accept that challenge once. A
        # status-8 response carrying file_id/pick_code is content reuse instead
        # and is handled above/below; the caller still confirms the remote name.
        if status in {7, 8} and initialized.get("sign_check"):
            try:
                start_text, end_text = str(initialized["sign_check"]).split("-", 1)
                start, end = int(start_text), int(end_text)
            except (TypeError, ValueError) as exc:
                raise Error115(0, "115 upload signature range is invalid") from exc
            # sign_check follows HTTP Range semantics, so both endpoints are
            # inclusive (for example, 1-4 hashes four bytes).
            sign_val = hashlib.sha1(content[start : end + 1]).hexdigest().upper()
            initialized = await self.upload_init(
                folder_id=folder_id,
                name=name,
                content=content,
                sign_key=str(initialized.get("sign_key") or ""),
                sign_val=sign_val,
            )
            status = int(initialized.get("status") or 0)
        reused_status = status == 2 or (
            status == 8
            and bool(initialized.get("file_id") or initialized.get("pick_code"))
        )
        if reused_status:
            directory_changes.notify_write("upload_completed", {"folder_id": folder_id})
            return {**initialized, "uploaded": False, "reused": True}
        if status != 1:
            code = str(initialized.get("code") or "").strip()
            suffix = f", code={code}" if code else ""
            raise Error115(0, f"115 upload initialization failed (status={status}{suffix})")
        token = await self.upload_token()
        endpoint = str(token.get("endpoint") or "").strip()
        if not endpoint:
            raise Error115(0, "115 upload endpoint is empty")
        bucket = str(initialized.get("bucket") or "").strip()
        object_name = str(initialized.get("object") or "").lstrip("/")
        callback = initialized.get("callback")
        if isinstance(callback, list):
            callback = callback[0] if callback else {}
        if not isinstance(callback, dict):
            callback = {}
        if isinstance(callback.get("value"), dict):
            callback = callback["value"]
        if not bucket or not object_name:
            safe_keys = ",".join(sorted(str(key) for key in initialized)[:20])
            raise Error115(0, f"115 upload initialization is incomplete (status={status}, fields={safe_keys})")
        endpoint = endpoint.rstrip("/")
        if not endpoint.startswith("http://") and not endpoint.startswith("https://"):
            endpoint = f"https://{endpoint}"
        # 115 returns the regional OSS endpoint (for example
        # oss-cn-shenzhen.aliyuncs.com). OSS rejects path-style bucket URLs and
        # requires the bucket as a third-level host name.
        endpoint_parts = urlsplit(endpoint)
        endpoint_host = endpoint_parts.hostname or ""
        if not endpoint_host:
            raise Error115(0, "115 upload endpoint is invalid")
        virtual_host = endpoint_host if endpoint_host.startswith(f"{bucket}.") else f"{bucket}.{endpoint_host}"
        if endpoint_parts.port:
            virtual_host = f"{virtual_host}:{endpoint_parts.port}"

        # The HTTP URL needs percent encoding, while OSS Signature V1 defines
        # CanonicalizedResource with the original bucket/object names.
        encoded_object_name = quote(object_name, safe="/~")
        endpoint_path = endpoint_parts.path.rstrip("/")
        url = urlunsplit((endpoint_parts.scheme, virtual_host, f"{endpoint_path}/{encoded_object_name}", "", ""))
        date_value = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())
        content_type = "application/octet-stream"
        callback_value = str(callback.get("callback") or "")
        callback_var = str(callback.get("callback_var") or "")
        headers = {
            "Date": date_value,
            "Content-Type": content_type,
            "Content-Length": str(len(content)),
            "x-oss-security-token": str(token.get("SecurityToken") or ""),
        }
        if callback_value:
            headers["x-oss-callback"] = base64.b64encode(callback_value.encode()).decode()
        if callback_var:
            headers["x-oss-callback-var"] = base64.b64encode(callback_var.encode()).decode()
        canonical_headers = "".join(
            f"{key.lower()}:{value.strip()}\n"
            for key, value in sorted(headers.items())
            if key.lower().startswith("x-oss-") and value
        )
        resource = f"/{bucket}/{object_name}"
        string_to_sign = f"PUT\n\n{content_type}\n{date_value}\n{canonical_headers}{resource}"
        secret = str(token.get("AccessKeySecret") or token.get("AccessKeySecrett") or "")
        access_key = str(token.get("AccessKeyId") or "")
        if not secret or not access_key:
            raise Error115(0, "115 upload token is incomplete")
        signature = base64.b64encode(
            hmac.new(secret.encode(), string_to_sign.encode(), hashlib.sha1).digest()
        ).decode()
        headers["Authorization"] = f"OSS {access_key}:{signature}"
        async with httpx.AsyncClient(timeout=max(self.timeout, 60.0), follow_redirects=True, trust_env=False) as client:
            response = await client.put(url, content=content, headers=headers)
        if response.status_code >= 300:
            detail = response.text.strip()
            if len(detail) > 300:
                detail = detail[:300] + "…"
            suffix = f": {detail}" if detail else ""
            raise Error115(response.status_code, f"115 sidecar upload failed ({response.status_code}){suffix}")
        directory_changes.notify_write("upload_completed", {"folder_id": folder_id})
        return {**initialized, "uploaded": True, "reused": False, "size": len(content)}

    async def download_bytes(self, item: dict[str, Any], *, max_bytes: int) -> bytes:
        size = int(item.get("size") or 0)
        if size and size > max_bytes:
            raise ValueError("115 sidecar exceeds the configured read limit")
        resolved = await self.download_url(str(item.get("pick_code") or ""), user_agent=self.user_agent)
        async with httpx.AsyncClient(timeout=max(self.timeout, 60.0), follow_redirects=True, trust_env=False) as client:
            async with client.stream("GET", resolved["url"], headers={"User-Agent": self.user_agent}) as response:
                response.raise_for_status()
                length = int(response.headers.get("content-length") or 0)
                if length and length > max_bytes:
                    raise ValueError("115 sidecar exceeds the configured read limit")
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes(1024 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError("115 sidecar exceeds the configured read limit")
                    chunks.append(chunk)
        return b"".join(chunks)

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
        "parent_id": str(item.get("parent_id") or item.get("pid") or ""),
        "name": str(item.get("file_name") or ""),
        "sha1": str(item.get("sha1") or "").upper(),
        "size": int(item.get("size_byte") or item.get("file_size") or 0),
        "pick_code": str(item.get("pick_code") or ""),
        "is_directory": str(item.get("file_category") or "") == "0",
        "updated_at": int(item.get("user_utime") or item.get("utime") or 0),
        "paths": list(item.get("paths") or []),
    }
