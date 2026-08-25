"""Authenticated client for the QRLS website bot scheduling API."""

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import aiohttp


@dataclass
class ScheduleAPIError(Exception):
    message: str
    status: Optional[int] = None

    def __str__(self) -> str:
        return self.message

    @property
    def is_conflict(self) -> bool:
        return self.status == 409


class WebsiteScheduleClient:
    def __init__(self, *, session: Any = None):
        self.base_url = os.getenv("QRLS_WEBSITE_BASE_URL", "").strip().rstrip("/")
        self.secret = os.getenv("QRLS_BOT_API_SECRET", "").strip()
        self.session = session

    def validate_config(self) -> None:
        if not self.base_url or not self.secret:
            raise ScheduleAPIError("Website scheduling is not configured on the bot.")

    async def _request(self, method: str, *, params: dict | None = None, json: dict | None = None) -> dict:
        self.validate_config()
        headers = {"Authorization": f"Bearer {self.secret}", "Accept": "application/json"}
        timeout = aiohttp.ClientTimeout(total=15)

        async def perform(session: Any) -> dict:
            async with session.request(
                method,
                f"{self.base_url}/api/bot/schedule",
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
                    raise ScheduleAPIError(message or "Website scheduling request failed.", response.status)
                if not isinstance(body, dict):
                    raise ScheduleAPIError("Website scheduling returned an invalid response.")
                return body

        if self.session is not None:
            return await perform(self.session)
        async with aiohttp.ClientSession() as session:
            return await perform(session)

    async def get_week(self, week: int) -> dict:
        body = await self._request("GET", params={"week": str(week)})
        if not isinstance(body.get("week"), dict) or not isinstance(body.get("series"), list):
            raise ScheduleAPIError("Website scheduling returned incomplete week data.")
        return body

    async def link_channel(self, series_id: str, channel_id: int) -> dict:
        return await self._request("POST", json={
            "action": "link_channel",
            "seriesId": series_id,
            "channelId": str(channel_id),
        })

    async def update_time(
        self,
        action: str,
        series_id: str,
        scheduled_at: datetime,
        actor_discord_id: int,
        expected_version: int,
    ) -> dict:
        if action not in {"propose", "confirm"}:
            raise ValueError("action must be propose or confirm")
        body = await self._request("POST", json={
            "action": action,
            "seriesId": series_id,
            "scheduledAt": scheduled_at.isoformat(),
            "actorDiscordId": str(actor_discord_id),
            "expectedVersion": expected_version,
        })
        series = body.get("series")
        if not isinstance(series, dict) or not isinstance(series.get("version"), int):
            raise ScheduleAPIError("Website scheduling returned incomplete series data.")
        return series
