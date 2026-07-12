"""Telegram alert delivery."""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


def send_telegram_message_sync(
    bot_token: str,
    chat_id: str,
    text: str,
    *,
    parse_mode: str = "HTML",
) -> tuple[bool, str]:
    token = (bot_token or "").strip()
    chat = (chat_id or "").strip()
    if not token or not chat:
        return False, "Telegram bot token or chat ID not configured"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat, "text": text, "parse_mode": parse_mode, "disable_web_page_preview": False}
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(url, json=payload)
            data = resp.json()
            if resp.status_code == 200 and data.get("ok"):
                return True, ""
            return False, str(data.get("description") or resp.text)
    except Exception as exc:
        logger.warning("Telegram send failed: %s", exc)
        return False, str(exc)


async def send_telegram_message(
    bot_token: str,
    chat_id: str,
    text: str,
    *,
    parse_mode: str = "HTML",
) -> tuple[bool, str]:
    return send_telegram_message_sync(bot_token, chat_id, text, parse_mode=parse_mode)


def format_new_online_alert(title: str, url: str, price: str | None, profile_name: str) -> str:
    price_line = f"\n💰 <b>Price:</b> €{price}" if price else ""
    return (
        f"🟢 <b>NEW ONLINE</b>\n\n"
        f"📦 <b>{_esc(title or 'Unknown product')}</b>\n"
        f"🏷 Profile: {_esc(profile_name)}{price_line}\n\n"
        f"🔗 <a href=\"{_esc(url)}\">Open on bol.com</a>\n"
        f"<i>Product page is live (may not be in stock yet)</i>"
    )


def format_in_stock_alert(title: str, url: str, price: str | None, profile_name: str) -> str:
    price_line = f"\n💰 <b>Price:</b> €{price}" if price else ""
    return (
        f"📦 <b>IN STOCK</b>\n\n"
        f"✅ <b>{_esc(title or 'Unknown product')}</b>\n"
        f"🏷 Profile: {_esc(profile_name)}{price_line}\n\n"
        f"🔗 <a href=\"{_esc(url)}\">Open on bol.com</a>"
    )


def format_session_logout_alert() -> str:
    return (
        "⚠️ <b>Bol session expired</b>\n\n"
        "You are logged out of bol.com on the server.\n"
        "The bot does <b>not</b> auto-login.\n\n"
        "On your PC run <b>login-bol.bat</b>, log in manually, then Start monitoring again."
    )


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
