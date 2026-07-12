"""Dispatch monitor alerts to Discord and/or Telegram based on user settings."""
from __future__ import annotations

from app.config import Settings, get_settings
from app.models import MonitoringSetting
from app.services.discord_service import (
    embed_in_stock,
    embed_new_online,
    embed_session_logout,
    send_discord_message_sync,
)
from app.services.telegram_service import (
    format_in_stock_alert,
    format_new_online_alert,
    format_session_logout_alert,
    send_telegram_message_sync,
)


def _discord_webhook(mon: MonitoringSetting, settings: Settings | None = None) -> str:
    s = settings or get_settings()
    return (mon.discord_webhook_url or s.DISCORD_WEBHOOK_URL or "").strip()


def _telegram_config(mon: MonitoringSetting, settings: Settings | None = None) -> tuple[str, str]:
    s = settings or get_settings()
    token = (mon.telegram_bot_token or s.TELEGRAM_BOT_TOKEN or "").strip()
    chat = (mon.telegram_chat_id or s.TELEGRAM_CHAT_ID or "").strip()
    return token, chat


def send_new_online_alert(
    mon: MonitoringSetting,
    *,
    title: str,
    url: str,
    price: str | None,
    profile_name: str,
    settings: Settings | None = None,
) -> tuple[bool, bool, str | None]:
    """Returns (telegram_ok, discord_ok, combined_error)."""
    s = settings or get_settings()
    tg_ok = False
    dc_ok = False
    errors: list[str] = []

    if mon.alerts_discord:
        webhook = _discord_webhook(mon, s)
        if webhook:
            content, embed = embed_new_online(title, url, price, profile_name)
            dc_ok, err = send_discord_message_sync(webhook, content, embed=embed)
            if err:
                errors.append(f"Discord: {err}")
        elif mon.alerts_discord:
            errors.append("Discord: webhook URL not configured")

    if mon.alerts_telegram:
        token, chat = _telegram_config(mon, s)
        if token and chat:
            text = format_new_online_alert(title, url, price, profile_name)
            tg_ok, err = send_telegram_message_sync(token, chat, text)
            if err:
                errors.append(f"Telegram: {err}")
        elif mon.alerts_telegram:
            errors.append("Telegram: bot token or chat ID not configured")

    err_msg = "; ".join(errors) if errors else None
    return tg_ok, dc_ok, err_msg


def send_in_stock_alert(
    mon: MonitoringSetting,
    *,
    title: str,
    url: str,
    price: str | None,
    profile_name: str,
    settings: Settings | None = None,
) -> tuple[bool, bool, str | None]:
    s = settings or get_settings()
    tg_ok = False
    dc_ok = False
    errors: list[str] = []

    if mon.alerts_discord:
        webhook = _discord_webhook(mon, s)
        if webhook:
            content, embed = embed_in_stock(title, url, price, profile_name)
            dc_ok, err = send_discord_message_sync(webhook, content, embed=embed)
            if err:
                errors.append(f"Discord: {err}")
        elif mon.alerts_discord:
            errors.append("Discord: webhook URL not configured")

    if mon.alerts_telegram:
        token, chat = _telegram_config(mon, s)
        if token and chat:
            text = format_in_stock_alert(title, url, price, profile_name)
            tg_ok, err = send_telegram_message_sync(token, chat, text)
            if err:
                errors.append(f"Telegram: {err}")
        elif mon.alerts_telegram:
            errors.append("Telegram: bot token or chat ID not configured")

    err_msg = "; ".join(errors) if errors else None
    return tg_ok, dc_ok, err_msg


def send_session_logout_alert(
    mon: MonitoringSetting,
    settings: Settings | None = None,
) -> tuple[bool, bool, str | None]:
    s = settings or get_settings()
    tg_ok = False
    dc_ok = False
    errors: list[str] = []

    if mon.alerts_discord:
        webhook = _discord_webhook(mon, s)
        if webhook:
            content, embed = embed_session_logout()
            dc_ok, err = send_discord_message_sync(webhook, content, embed=embed)
            if err:
                errors.append(f"Discord: {err}")

    if mon.alerts_telegram:
        token, chat = _telegram_config(mon, s)
        if token and chat:
            tg_ok, err = send_telegram_message_sync(token, chat, format_session_logout_alert())
            if err:
                errors.append(f"Telegram: {err}")

    err_msg = "; ".join(errors) if errors else None
    return tg_ok, dc_ok, err_msg
