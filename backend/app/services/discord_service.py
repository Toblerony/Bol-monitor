"""Discord webhook alerts for Bol Monitor."""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


def send_discord_message_sync(webhook_url: str, content: str, *, embed: dict | None = None) -> tuple[bool, str]:
    url = (webhook_url or "").strip()
    if not url:
        return False, "Discord webhook URL not configured"
    payload: dict = {"content": content[:2000]}
    if embed:
        payload["embeds"] = [embed]
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(url, json=payload)
            if resp.status_code in (200, 204):
                return True, ""
            return False, resp.text[:300]
    except Exception as exc:
        logger.warning("Discord send failed: %s", exc)
        return False, str(exc)


def embed_new_online(title: str, url: str, price: str | None, profile_name: str) -> tuple[str, dict]:
    content = f"🟢 **NEW ONLINE** — {title or 'Product'}"
    embed = {
        "title": title or "Unknown product",
        "url": url,
        "color": 3066993,
        "fields": [
            {"name": "Profile", "value": profile_name, "inline": True},
            {"name": "Price", "value": f"€{price}" if price else "—", "inline": True},
        ],
        "footer": {"text": "Bol Monitor — page is live (may not be in stock yet)"},
    }
    return content, embed


def embed_in_stock(title: str, url: str, price: str | None, profile_name: str) -> tuple[str, dict]:
    content = f"📦 **IN STOCK** — {title or 'Product'}"
    embed = {
        "title": title or "Unknown product",
        "url": url,
        "color": 5763719,
        "fields": [
            {"name": "Profile", "value": profile_name, "inline": True},
            {"name": "Price", "value": f"€{price}" if price else "—", "inline": True},
        ],
        "footer": {"text": "Bol Monitor — add to cart / op voorraad"},
    }
    return content, embed


def embed_session_logout() -> tuple[str, dict]:
    content = "⚠️ **Bol session expired** — open Settings → Login to Bol on your PC and sign in again."
    embed = {
        "title": "Bol session expired",
        "color": 16776960,
        "description": "Monitoring needs a fresh bol.com login saved to the database.",
    }
    return content, embed
