"""Authenticated client for the QRLS website bot roster API."""

import os
from dataclasses import dataclass
from typing import Any, Optional

import aiohttp


@dataclass
class RosterAPIError(Exception):
    message: str
    status: Optional[int] = None

    def __str__(self) -> str:
        return self.message


class WebsiteRosterClient:
    def __init__(self, *, session: Any = None):
        self.base_url = os.getenv("QRLS_WEBSITE_BASE_URL", "").strip().rstrip("/")
        self.secret = os.getenv("QRLS_BOT_API_SECRET", "").strip()
        self.session = session

    def validate_config(self) -> None:
        if not self.base_url or not self.secret:
            raise RosterAPIError("Website roster integration is not configured on the bot.")

    async def _request(self, payload: dict) -> dict:
        self.validate_config()
        headers = {"Authorization": f"Bearer {self.secret}", "Accept": "application/json"}
        timeout = aiohttp.ClientTimeout(total=20)

        async def perform(session: Any) -> dict:
            async with session.post(
                f"{self.base_url}/api/bot/roster",
                json=payload,
                headers=headers,
                timeout=timeout,
            ) as response:
                try:
                    body = await response.json()
                except (aiohttp.ContentTypeError, ValueError):
                    body = {}
                if response.status >= 400:
                    message = body.get("error") if isinstance(body, dict) else None
                    raise RosterAPIError(message or "Website roster request failed.", response.status)
                if not isinstance(body, dict) or not isinstance(body.get("transaction"), dict):
                    raise RosterAPIError("Website roster returned an invalid response.")
                return body

        if self.session is not None:
            return await perform(self.session)
        async with aiohttp.ClientSession() as session:
            return await perform(session)

    async def add_or_drop(self, action: str, *, actor_id: int, player_id: int, request_key: str) -> dict:
        if action not in {"add", "drop"}:
            raise ValueError("action must be add or drop")
        return await self._request({
            "action": action,
            "actorDiscordId": str(actor_id),
            "playerDiscordId": str(player_id),
            "requestKey": request_key,
        })

    async def create_trade(self, *, actor_id: int, own_player_id: int, other_player_id: int, request_key: str) -> dict:
        return await self._request({
            "action": "trade_create",
            "actorDiscordId": str(actor_id),
            "ownPlayerDiscordId": str(own_player_id),
            "otherPlayerDiscordId": str(other_player_id),
            "requestKey": request_key,
        })

    async def decide_trade(
        self,
        stage: str,
        *,
        actor_id: int,
        transaction_id: str,
        decision: str,
        request_key: str,
        reason: str = "",
    ) -> dict:
        if stage not in {"captain", "admin"}:
            raise ValueError("stage must be captain or admin")
        if decision not in {"approve", "decline"}:
            raise ValueError("decision must be approve or decline")
        return await self._request({
            "action": f"trade_{stage}_decision",
            "actorDiscordId": str(actor_id),
            "transactionId": transaction_id,
            "decision": decision,
            "reason": reason,
            "requestKey": request_key,
        })
