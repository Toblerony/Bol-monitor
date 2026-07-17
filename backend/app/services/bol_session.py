"""Bol.com session — Playwright cookies in file + database (shared with Render)."""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
from pathlib import Path

from playwright.async_api import BrowserContext, Page

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

SESSION_DB_KEY = "bol_session_storage"
BOL_HOME_URL = "https://www.bol.com/nl/nl/"
PASSIVE_LOGIN_POLL_SECONDS = 2
POST_LOGIN_SETTLE_SECONDS = 3

_ACCEPT_BUTTON = re.compile(
    r"^(Accept\s+all|Accept|Allow\s+all|Agree|OK|Alles\s+accepteren|"
    r"Alle\s+cookies\s+accepteren|Ik\s+ga\s+akkoord)$",
    re.I,
)
_INLOGGEN = re.compile(r"^\s*Inloggen\s*$", re.I)
_LOG_IN = re.compile(r"^\s*Log\s+in\s*$", re.I)


def session_file(cfg: Settings | None = None) -> Path:
    cfg = cfg or get_settings()
    path = cfg.session_file
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def profile_dir(cfg: Settings | None = None) -> Path:
    cfg = cfg or get_settings()
    path = cfg.profile_dir
    path.mkdir(parents=True, exist_ok=True)
    return path


def clear_bol_browser_data(cfg: Settings | None = None) -> dict:
    cfg = cfg or get_settings()
    removed_session = clear_session_file(cfg)
    profile = profile_dir(cfg)
    removed_profile = False
    profile_still_exists = False

    if profile.exists():
        for attempt in range(5):
            try:
                shutil.rmtree(profile)
                removed_profile = True
                break
            except Exception as exc:
                logger.warning("Profile delete attempt %s/5 failed: %s", attempt + 1, exc)
                time.sleep(0.8 * (attempt + 1))
        profile_still_exists = profile.exists()

    profile.mkdir(parents=True, exist_ok=True)
    return {
        "session_file": removed_session,
        "profile_dir": removed_profile and not profile_still_exists,
        "profile_cleared": not profile_still_exists,
    }


def clear_session_file(cfg: Settings | None = None) -> bool:
    cfg = cfg or get_settings()
    path = session_file(cfg)
    removed = False
    if path.exists():
        path.unlink()
        removed = True
    clear_session_in_db()
    return removed


def clear_session_in_db() -> None:
    from app.database import SessionLocal
    from app.models import ApplicationSetting

    db = SessionLocal()
    try:
        row = db.query(ApplicationSetting).filter(ApplicationSetting.key == SESSION_DB_KEY).first()
        if row:
            db.delete(row)
            db.commit()
    finally:
        db.close()


def has_bol_session_saved(cfg: Settings | None = None) -> bool:
    cfg = cfg or get_settings()
    path = session_file(cfg)
    if path.exists() and path.stat().st_size > 20:
        return True
    from app.database import SessionLocal
    from app.models import ApplicationSetting

    db = SessionLocal()
    try:
        row = db.query(ApplicationSetting).filter(ApplicationSetting.key == SESSION_DB_KEY).first()
        return bool(row and row.value and len(row.value.strip()) > 20)
    finally:
        db.close()


def persist_session_file_to_db(cfg: Settings | None = None) -> bool:
    cfg = cfg or get_settings()
    path = session_file(cfg)
    if not path.exists() or path.stat().st_size < 20:
        return False
    from app.database import SessionLocal
    from app.models import ApplicationSetting

    content = path.read_text(encoding="utf-8")
    db = SessionLocal()
    try:
        row = db.query(ApplicationSetting).filter(ApplicationSetting.key == SESSION_DB_KEY).first()
        if row:
            row.value = content
            row.category = "bol"
        else:
            db.add(ApplicationSetting(key=SESSION_DB_KEY, value=content, category="bol"))
        db.commit()
        logger.info("Bol session saved to database")
        return True
    finally:
        db.close()


def restore_session_file_from_db(cfg: Settings | None = None) -> bool:
    """Copy DB session to local file so Playwright can load cookies on Render."""
    cfg = cfg or get_settings()
    path = session_file(cfg)
    from app.database import SessionLocal
    from app.models import ApplicationSetting

    db = SessionLocal()
    try:
        row = db.query(ApplicationSetting).filter(ApplicationSetting.key == SESSION_DB_KEY).first()
        if not row or not row.value or len(row.value.strip()) < 20:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(row.value, encoding="utf-8")
        return True
    finally:
        db.close()


def get_bol_session_status() -> dict:
    cfg = get_settings()
    path = session_file(cfg)
    file_ok = path.exists() and path.stat().st_size > 20
    db_ok = False
    from app.database import SessionLocal
    from app.models import ApplicationSetting

    db = SessionLocal()
    try:
        row = db.query(ApplicationSetting).filter(ApplicationSetting.key == SESSION_DB_KEY).first()
        db_ok = bool(row and row.value and len(row.value.strip()) > 20)
    finally:
        db.close()
    has_session = file_ok or db_ok
    if has_session:
        message = "Bol session OK — saved in database" if db_ok else "Bol session OK — local file only"
    else:
        message = "No Bol session — run login-bol.bat on your PC (same DATABASE_URL as Render)"
    return {
        "has_session": has_session,
        "has_file": file_ok,
        "has_database": db_ok,
        "logged_in": has_session,
        "message": message,
    }


SESSION_REQUIRED_MSG = (
    "No Bol session saved. Run login-bol.bat on your PC, log in to bol.com, "
    "then click Start again. (Use the same DATABASE_URL as Render.)"
)

SESSION_DB_REQUIRED_MSG = (
    "Bol session is not in the database. Run login-bol.bat on your PC, log in to bol.com, "
    "then click Start again. (Use the same DATABASE_URL as Render.)"
)


def session_status_message(cfg: Settings | None = None) -> tuple[bool, str]:
    status = get_bol_session_status()
    ok = bool(status["has_session"] and status["has_database"])
    if ok:
        return True, status["message"]
    if status["has_session"] and not status["has_database"]:
        return False, SESSION_DB_REQUIRED_MSG
    return False, SESSION_REQUIRED_MSG


def assert_bol_session_for_start() -> None:
    """Raise HTTPException if monitoring cannot run without a saved Bol session."""
    from fastapi import HTTPException

    status = get_bol_session_status()
    if not status["has_session"]:
        raise HTTPException(status_code=400, detail=SESSION_REQUIRED_MSG)
    if not status["has_database"]:
        raise HTTPException(status_code=400, detail=SESSION_DB_REQUIRED_MSG)


async def dismiss_bol_cookie_popup(page: Page) -> bool:
    """Dismiss bol.com cookie/CMP overlay so Inloggen is reachable."""
    try:
        url = (page.url or "").lower()
        on_login_host = "login.bol.com" in url or "/account/login" in url
    except Exception:
        on_login_host = False

    selectors = (
        'button:has-text("Alles accepteren")',
        'button:has-text("Accept all")',
        'button:has-text("Accept All")',
        '[data-test="consent-accept"]',
        'button[id*="onetrust-accept"]',
    )
    if not on_login_host:
        selectors += ('button:has-text("Accepteren")',)

    contexts: list = [page]
    try:
        contexts.extend([f for f in page.frames if f != page.main_frame])
    except Exception:
        pass

    for ctx in contexts:
        for sel in selectors:
            try:
                btn = ctx.locator(sel).first
                if await btn.is_visible(timeout=1200):
                    await btn.scroll_into_view_if_needed(timeout=3000)
                    await btn.click(timeout=5000)
                    await asyncio.sleep(0.35)
                    return True
            except Exception:
                continue
        try:
            btn = ctx.get_by_role("button", name=_ACCEPT_BUTTON).first
            if await btn.is_visible(timeout=800):
                await btn.scroll_into_view_if_needed(timeout=3000)
                await btn.click(timeout=5000)
                await asyncio.sleep(0.35)
                return True
        except Exception:
            pass
    return False


async def header_shows_guest_login(page: Page) -> bool:
    """Header shows Inloggen — user is NOT logged in."""
    try:
        hdr = page.locator("header").first
        if not await hdr.is_visible(timeout=600):
            return False
        for pattern in (_INLOGGEN, _LOG_IN):
            for role in ("link", "button"):
                try:
                    loc = hdr.get_by_role(role, name=pattern).first
                    if await loc.is_visible(timeout=450):
                        return True
                except Exception:
                    pass
    except Exception:
        pass
    return False


async def header_shows_welkom_account(page: Page) -> bool:
    """Logged-in chip: Welkom <name> in header (not guest Inloggen)."""
    try:
        hdr = page.locator("header").first
        if not await hdr.is_visible(timeout=1500):
            return False
        if await header_shows_guest_login(page):
            return False
        blob = re.sub(r"\s+", " ", await hdr.inner_text() or "").strip()
        if re.search(r"\bwelkom\s+(?!bij\b)(\S+)", blob, re.I):
            return True
        if re.search(r"\bwelcome\s*,?\s+(?!to\b|back\b)([a-z]{2,40})", blob, re.I):
            return True
    except Exception:
        pass
    return False


async def auth_from_header(page: Page) -> str | None:
    """'guest', 'logged_in', or None if header not ready."""
    try:
        hdr = page.locator("header").first
        if not await hdr.is_visible(timeout=900):
            return None
    except Exception:
        return None
    if await header_shows_guest_login(page):
        return "guest"
    if await header_shows_welkom_account(page):
        return "logged_in"
    return None


async def has_bol_login_cookies(context: BrowserContext) -> bool:
    """Tracking cookies alone do NOT mean logged in — use is_bol_logged_in instead."""
    try:
        names = {c.get("name") for c in await context.cookies() if c.get("value")}
        return bool(names & {"XSC", "BUI", "TMA", "sessionid"})
    except Exception:
        return False


async def is_bol_logged_in(context: BrowserContext, page: Page) -> bool:
    """Strict: Welkom in header or account page — guest/tracking cookies do not count."""
    if "login.bol.com" in (page.url or "").lower():
        return False
    if await header_shows_welkom_account(page):
        return True
    auth = await auth_from_header(page)
    if auth == "logged_in":
        return True
    if auth == "guest":
        return False
    try:
        url = (page.url or "").lower()
        if any(x in url for x in ("account/overzicht", "account/bestellingen", "account/instellingen")):
            return not await header_shows_guest_login(page)
    except Exception:
        pass
    return False


async def wait_passive_for_bol_login(
    context: BrowserContext,
    page: Page,
    *,
    timeout_seconds: int = 900,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        await dismiss_bol_cookie_popup(page)
        if await is_bol_logged_in(context, page):
            return True
        await asyncio.sleep(PASSIVE_LOGIN_POLL_SECONDS)
    return False


async def save_session(context: BrowserContext, cfg: Settings | None = None) -> None:
    cfg = cfg or get_settings()
    try:
        path = session_file(cfg)
        await context.storage_state(path=str(path))
        persist_session_file_to_db(cfg)
    except Exception as exc:
        logger.warning("Could not save Bol session: %s", exc)


async def _maximize_browser_window(page: Page) -> None:
    """Full-screen maximized window on Windows (not a small floating viewport)."""
    try:
        cdp = await page.context.new_cdp_session(page)
        info = await cdp.send("Browser.getWindowForTarget")
        await cdp.send(
            "Browser.setWindowBounds",
            {"windowId": info["windowId"], "bounds": {"windowState": "maximized"}},
        )
    except Exception as exc:
        logger.debug("CDP maximize failed: %s", exc)
    try:
        await page.bring_to_front()
    except Exception:
        pass


async def open_bol_login_browser(cfg: Settings | None = None) -> bool:
    """Visible Chromium for login-bol.bat — opens bol.com immediately, user logs in manually.

    Uses Proxies page settings when enabled (same pool as monitor) so login works if
    the home IP is blocked by bol.com.
    """
    from playwright.async_api import async_playwright

    from app.database import SessionLocal
    from app.playwright_browsers import configure_playwright_browsers_path, ensure_chromium_installed
    from app.services.proxy_client import ParsedProxy
    from app.services.proxy_service import load_pool_from_db

    cfg = cfg or get_settings()
    configure_playwright_browsers_path()
    exe = ensure_chromium_installed(quiet=True)

    pool = None
    db = SessionLocal()
    try:
        pool = load_pool_from_db(db)
    finally:
        db.close()

    proxy_attempts: list[ParsedProxy | None]
    if pool is not None and pool.enabled:
        proxy_attempts = []
        seen: set[str] = set()
        for _ in range(min(pool.count, 5)):
            p = pool.next()
            if p is None or p.raw in seen:
                continue
            seen.add(p.raw)
            proxy_attempts.append(p)
        if not proxy_attempts:
            proxy_attempts = [None]
        print(f"\nProxies ON — will try up to {len(proxy_attempts)} proxy(ies) for login.")
    else:
        proxy_attempts = [None]
        print("\nProxies OFF — direct connection (enable Proxies page if IP blocked).")

    async with async_playwright() as p:
        for attempt_i, proxy in enumerate(proxy_attempts, start=1):
            launch_kwargs: dict = {
                "headless": False,
                "executable_path": str(exe),
                "args": ["--start-maximized", "--window-position=0,0"],
            }
            if proxy is not None:
                launch_kwargs["proxy"] = proxy.playwright_proxy
                print(
                    f"\n[{attempt_i}/{len(proxy_attempts)}] Opening browser via proxy "
                    f"{proxy.label or proxy.host}:{proxy.port} …"
                )
            else:
                print(f"\n[{attempt_i}/{len(proxy_attempts)}] Opening browser (direct) …")

            browser = await p.chromium.launch(**launch_kwargs)
            try:
                # Fresh login window — do NOT reload old storage_state.
                # Old DB session is separate; clear only if you want to wipe it after a new login.
                context_kwargs: dict = {"locale": "nl-NL", "no_viewport": True}

                context = await browser.new_context(**context_kwargs)
                page = await context.new_page()
                await _maximize_browser_window(page)

                print(f"Opening {BOL_HOME_URL} …")
                try:
                    await page.goto(BOL_HOME_URL, wait_until="domcontentloaded", timeout=120000)
                except Exception as exc:
                    print(f"Navigate failed: {exc}")
                    if proxy is not None and pool is not None:
                        pool.mark_failed(proxy, reason=str(exc)[:80])
                    await context.close()
                    await browser.close()
                    continue

                await _maximize_browser_window(page)
                await asyncio.sleep(1.0)

                body = ""
                try:
                    body = (await page.content())[:80000].lower()
                except Exception:
                    pass
                title = ""
                try:
                    title = (await page.title() or "").lower()
                except Exception:
                    pass
                blocked = any(
                    x in body or x in title
                    for x in ("ip-geblokkeerd", "access denied", "geblokkeerd")
                )
                if blocked:
                    print("Bol shows IP blocked on this connection.")
                    if proxy is not None and pool is not None:
                        pool.mark_failed(proxy, reason="ip-geblokkeerd")
                        print("Rotating to next proxy…")
                        await context.close()
                        await browser.close()
                        continue
                    print("Enable proxies in the dashboard (Proxies page) and run login-bol.bat again.")
                    await context.close()
                    await browser.close()
                    return False

                if proxy is not None and pool is not None:
                    pool.mark_ok(proxy)

                for _ in range(6):
                    if await dismiss_bol_cookie_popup(page):
                        print("Cookie popup dismissed — click Inloggen in the header to log in.")
                        break
                    await asyncio.sleep(0.5)

                print("\nChromium is open (maximized) — complete these steps in the browser:")
                print("  1. Accept cookies if still shown")
                print("  2. Click Inloggen (top right)")
                print("  3. Log in with your bol.com account")
                print("  4. Wait until header shows Welkom <your name>")
                print("\nBot is paused — waiting up to 15 minutes for login…\n")

                ok = await wait_passive_for_bol_login(context, page, timeout_seconds=900)
                if ok:
                    await asyncio.sleep(POST_LOGIN_SETTLE_SECONDS)
                    await save_session(context, cfg)
                    print("\nLogin detected (Welkom in header) — session saved.")
                else:
                    print("\nTimed out — complete bol.com login and run login-bol.bat again.")

                await context.close()
                await browser.close()
                return ok
            except Exception as exc:
                logger.warning("Login browser attempt failed: %s", exc)
                print(f"Browser attempt failed: {exc}")
                try:
                    await browser.close()
                except Exception:
                    pass
                if proxy is not None and pool is not None:
                    pool.mark_failed(proxy, reason=str(exc)[:80])
                continue

        print("\nAll proxy/direct attempts failed (IP blocked or network error).")
        print("Check Proxies page — enable Use proxies and add working residential lines.")
        return False


def check_bol_session_still_valid_sync(cfg: Settings | None = None) -> bool:
    """Headless check — session cookies still show Welkom (no auto-login)."""
    cfg = cfg or get_settings()
    if not has_bol_session_saved(cfg):
        return False
    restore_session_file_from_db(cfg)
    sf = session_file(cfg)
    if not sf.is_file():
        return False
    try:
        return asyncio.run(_verify_stored_session_async(cfg, sf))
    except Exception as exc:
        logger.warning("Bol session verify failed: %s", exc)
        return False


async def _verify_stored_session_async(cfg: Settings, sf: Path) -> bool:
    from playwright.async_api import async_playwright

    from app.database import SessionLocal
    from app.playwright_browsers import configure_playwright_browsers_path, ensure_chromium_installed
    from app.services.browser_settings import get_playwright_headless
    from app.services.proxy_service import load_pool_from_db

    configure_playwright_browsers_path()
    exe = ensure_chromium_installed(quiet=True)

    launch_kwargs: dict = {
        "headless": get_playwright_headless(),
        "executable_path": str(exe),
    }
    db = SessionLocal()
    try:
        pool = load_pool_from_db(db)
        if pool is not None and pool.enabled:
            proxy = pool.next()
            if proxy is not None:
                launch_kwargs["proxy"] = proxy.playwright_proxy
    finally:
        db.close()

    async with async_playwright() as p:
        browser = await p.chromium.launch(**launch_kwargs)
        try:
            context = await browser.new_context(storage_state=str(sf), locale="nl-NL")
            page = await context.new_page()
            await page.goto(BOL_HOME_URL, wait_until="domcontentloaded", timeout=90000)
            await dismiss_bol_cookie_popup(page)
            return await is_bol_logged_in(context, page)
        finally:
            await browser.close()


def update_monitoring_session_flag(db) -> None:
    from app.models import MonitoringSetting

    mon = db.query(MonitoringSetting).first()
    if not mon:
        return
    ok, msg = session_status_message()
    mon.bol_session_ok = ok
    mon.bol_session_message = msg
