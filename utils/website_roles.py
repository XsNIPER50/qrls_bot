"""Authenticated client for the QRLS website Discord role-sync API."""

import os
from dataclasses import dataclass
from typing import Any, Optional

import aiohttp


@dataclass
class RoleSyncAPIError(Exception):
    message: str
    status: Optional[int] = None

    def __str__(self) -> str:
        return self.message


class WebsiteRolesClient:
    def __init__(self, *, session: Any = None):
        self.base_url = os.getenv("QRLS_WEBSITE_BASE_URL", "").strip().rstrip("/")
        self.secret = os.getenv("QRLS_BOT_API_SECRET", "").strip()
        self.session = session

    def validate_config(self) -> None:
        if not self.base_url or not self.secret:
            raise RoleSyncAPIError("Website role synchronization is not configured on the bot.")

    async def _request(self, method: str, *, params: dict | None = None, json: dict | None = None) -> dict:
        self.validate_config()
        headers = {"Authorization": f"Bearer {self.secret}", "Accept": "application/json"}
        timeout = aiohttp.ClientTimeout(total=15)

        async def perform(session: Any) -> dict:
            async with session.request(
                method,
                f"{self.base_url}/api/bot/roles",
                params=params,
                json=json,
                headers=headers,
                timeout=timeout,
            ) as response:
                try:
                    body = await response.json()
                except (aiohttp.ContentTypeError, ValueError):
                    body = {}
                if response.status >= 400:
                    message = body.get("error") if isinstance(body, dict) else None
                    raise RoleSyncAPIError(message or "Website role-sync request failed.", response.status)
                if not isinstance(body, dict):
                    raise RoleSyncAPIError("Website role-sync returned an invalid response.")
                return body

        if self.session is not None:
            return await perform(self.session)
        async with aiohttp.ClientSession() as session:
            return await perform(session)

    async def claim(self) -> dict | None:
        body = await self._request("GET", params={"claim": "role_sync"})
        job = body.get("job")
        if job is not None and not isinstance(job, dict):
            raise RoleSyncAPIError("Website role-sync returned an invalid job.")
        return job

    async def complete(self, job_id: str) -> None:
        body = await self._request("POST", json={"action": "complete", "jobId": job_id})
        if body.get("ok") is not True:
            raise RoleSyncAPIError("Website did not acknowledge role-sync completion.")

    async def fail(self, job_id: str, error: str) -> None:
        safe_error = " ".join(error.split())[:1000] or "Discord role update failed."
        body = await self._request("POST", json={"action": "fail", "jobId": job_id, "error": safe_error})
        if body.get("ok") is not True:
            raise RoleSyncAPIError("Website did not acknowledge the role-sync failure.")
