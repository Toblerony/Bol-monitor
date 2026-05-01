"""
Browser Mode (proxy-bound Playwright sessions).
Up to **3** Chromium slots (``BOL_BROWSER_MAX_PARALLEL``, default 3); **each slot runs in its own thread with its own
Chromium + Playwright instance** so long PDP/checkout work on one slot does not block others.

CSV http URL order ↔ proxy.txt line index ↔ cookies/session_<n>.json + session_<n>.fingerprint.txt.
Absolute slot index ``n``: S1=row1/session_1/Email1; enabling only S2 still uses ``session_2.json`` (see ``[BIND]`` logs).
Failover pool ``proxies.txt``: extra identities when login/session breaks (cooldown + full reset) —
not used for offline PDP / IP-shell monitoring.
(sha256 hex + readable comment; legacy session_<n>.proxy.fp still read). Stale JSON removed when proxy line changes.

Up to 3 Chromium slots run **in parallel** — **one code path**: every enabled slot runs the same
monitor → ATC → checkout flow as S1 (only credentials, proxy line, CSV row index, and cookie file differ).
Credentials: slot 1 uses ``Email1``/``Password1`` (else legacy ``Email``/``Password``), slot 2 uses ``Email2``/``Password2``,
slot 3 uses ``Email3``/``Password3`` (also ``BOL_EMAILn`` / ``BOL_PASSWORDn`` variants).

By default **every** slot index ``0..n_resource-1`` starts (same code path). Set ``BOL_BROWSER_USE_SLOT_ENABLE_FLAGS=1`` to honor legacy ``BOL_BROWSER_ENABLE_SLOT_1``…``_3``.

Each slot keeps the **product URL** captured at launch (``BOL_BROWSER_STICKY_SLOT_PRODUCT_URL`` default on) so later CSV edits do not retarget S2/S3. Proxy for slot N remains ``proxy.txt`` line N for the run (failover excepted). Set ``BOL_BROWSER_STICKY_SLOT_PRODUCT_URL=0`` to follow live CSV row order again.
"""
from __future__ import annotations

import hashlib
import os
import random
import re
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import urlparse


class FailoverIdentityRestart(Exception):
    """Close Chromium context and retry with another proxy / clean cookies (browser mode only)."""


# NL storefront — human-like login starts here (guest → Inloggen), not a fast goto(login.bol.com).
STOREFRONT_NL_URL = "https://www.bol.com/nl/nl/"

# During PDP checkout, GUI routes logs by ``[Sn]`` prefix (parallel threads each set their slot).
_checkout_log_tls = threading.local()


def _checkout_log_slot_num() -> int:
    return int(getattr(_checkout_log_tls, "slot", 0) or 0)


def _set_checkout_log_slot(n: int) -> None:
    _checkout_log_tls.slot = int(n)


def _cprint(*args: object, flush: bool = False) -> None:
    """Like ``print`` but prefixes ``[Sn]`` for this thread's checkout slot (unless already prefixed)."""
    sid = _checkout_log_slot_num()
    if sid and args and isinstance(args[0], str):
        first = args[0]
        if not re.match(r"^\[S\d+\]\s", first):
            pref = f"[S{sid}] "
            if not first.startswith(pref):
                args = (pref + first,) + tuple(args[1:])
    print(*args, flush=flush)


def _lprint(msg: str, *, flush: bool = True) -> None:
    """Login-path logs: same ``[Sn]`` prefix as ``_cprint`` when a slot worker set ``_set_checkout_log_slot``."""
    sid = _checkout_log_slot_num()
    if sid and isinstance(msg, str):
        if not re.match(r"^\[S\d+\]\s", msg):
            pref = f"[S{sid}] "
            if not msg.startswith(pref):
                msg = pref + msg
    print(msg, flush=flush)


def _load_dotenv_simple(env_path: str) -> None:
    if not os.path.isfile(env_path):
        return
    try:
        with open(env_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except OSError:
        pass


def _ensure_playwright() -> None:
    try:
        import playwright  # noqa: F401
    except ImportError:
        print("[BROWSER] Installing Playwright (pip)…", flush=True)
        subprocess.check_call(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--quiet",
                "--disable-pip-version-check",
                "playwright",
            ]
        )
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError as e:
        print(f"[BROWSER] Playwright import failed: {e}", flush=True)
        raise
    marker = Path(__file__).resolve().parent / ".playwright_chromium_ok"
    if not marker.is_file():
        print("[BROWSER] Installing Chromium for Playwright (one-time)…", flush=True)
        subprocess.check_call(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        try:
            marker.write_text("ok", encoding="utf-8")
        except OSError:
            pass


def _load_bol_module():
    import importlib.util

    root = Path(__file__).resolve().parent
    one_py = root / "1.py"
    spec = importlib.util.spec_from_file_location("bol_app", one_py)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load 1.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _bol_credentials() -> tuple[str, str]:
    """Legacy single-account helper — same as slot 0 in `_bol_credentials_for_slot`."""
    return _bol_credentials_for_slot(0)


def _bol_credentials_for_slot(slot: int) -> tuple[str, str]:
    """
    Slot index 0 → Chromium S1: Email1/Password1, else BOL_EMAIL1, else legacy Email/Password.
    Slot 1 → S2: Email2/Password2. Slot 2 → S3: Email3/Password3.
    """

    def pick_first(*keys: str) -> str:
        for k in keys:
            v = (os.getenv(k) or "").strip()
            if v:
                return v
        return ""

    if slot == 0:
        e = pick_first(
            "Email1",
            "BOL_EMAIL1",
            "email1",
            "BOL_EMAIL",
            "Email",
            "email",
        )
        p = pick_first(
            "Password1",
            "BOL_PASSWORD1",
            "password1",
            "BOL_PASSWORD",
            "Password",
            "password",
        )
        return e, p
    if slot == 1:
        return pick_first("Email2", "BOL_EMAIL2", "email2"), pick_first(
            "Password2", "BOL_PASSWORD2", "password2"
        )
    if slot == 2:
        return pick_first("Email3", "BOL_EMAIL3", "email3"), pick_first(
            "Password3", "BOL_PASSWORD3", "password3"
        )
    return "", ""


def _mask_login_hint(email: str) -> str:
    """Short masked hint for setup logs (not for security — avoids dumping full addresses)."""
    e = (email or "").strip()
    if not e:
        return "(missing)"
    if "@" in e:
        local, _, domain = e.partition("@")
        if len(local) <= 1:
            masked = f"{local}***" if local else "***"
        else:
            masked = f"{local[:2]}***"
        return f"{masked}@{domain}"
    if len(e) <= 2:
        return f"{e[0]}***" if e else "***"
    return f"{e[:2]}***"


def _scrub_proxy_txt_line_raw(line: str) -> str:
    """Inline copy of 1.py scrub to avoid import-before-bol load."""
    s = (line or "").strip()
    if not s or s.startswith("#"):
        return ""
    s = re.sub(r"\{%[\s\S]*?%\}\s*", "", s)
    s = re.sub(r"<!--[\s\S]*?-->\s*", "", s)
    return s.strip()


def _parse_first_proxy_line(proxy_file: Path) -> tuple[str, str, str, str] | None:
    return _parse_proxy_line_at_index(proxy_file, 1)


def _parse_proxy_line_at_index(
    proxy_file: Path, line_index_1based: int
) -> tuple[str, str, str, str] | None:
    """Nth valid host:port:user:pass line in proxy.txt (1-based)."""
    if line_index_1based < 1:
        return None
    try:
        raw_lines = proxy_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    n = 0
    for line in raw_lines:
        line = _scrub_proxy_txt_line_raw(line)
        if not line:
            continue
        parts = line.split(":")
        if len(parts) != 4:
            continue
        n += 1
        if n == line_index_1based:
            return parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3].strip()
    return None


_FP_DIRECT = hashlib.sha256(b"__bol_browser_use_proxies_0__").hexdigest()


def _proxy_tuple_fingerprint(host: str, port: str, user: str, password: str) -> str:
    raw = f"{host}:{port}:{user}:{password}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _proxy_hint_log(host: str, port: str, user: str) -> str:
    u = user or ""
    if len(u) <= 2:
        um = "***"
    else:
        um = f"{u[:2]}***{u[-1:]}"
    return f"{host}:{port} user={um}"


def _session_fingerprint_txt_path(cookies_dir: Path, session_id: int) -> Path:
    """Human-visible sidecar next to session_<id>.json (open in Notepad)."""
    return cookies_dir / f"session_{session_id}.fingerprint.txt"


def _session_fingerprint_legacy_fp_path(cookies_dir: Path, session_id: int) -> Path:
    """Older builds wrote only this file (hex only). Still honored when reading."""
    return cookies_dir / f"session_{session_id}.proxy.fp"


def _read_stored_proxy_fingerprint_hex(fp_txt: Path, legacy_fp: Path) -> str:
    """First sha256 line from .fingerprint.txt, else one-line hex from legacy .proxy.fp."""
    for p in (fp_txt, legacy_fp):
        if not p.is_file():
            continue
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in raw.splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            low = s.lower()
            if len(low) == 64 and all(c in "0123456789abcdef" for c in low):
                return low
        compact = "".join(raw.split())
        if "#" in compact:
            compact = compact.split("#", 1)[0].strip()
        low = compact.lower()
        if len(low) == 64 and all(c in "0123456789abcdef" for c in low):
            return low
    return ""


def _sync_session_file_with_proxy_fingerprint(
    state_path: Path,
    fp_txt: Path,
    legacy_fp: Path,
    current_fp: str,
    *,
    session_id: int,
    hint: str,
    announce: bool = True,
) -> None:
    """
    Drop storage_state JSON if it was saved under a different proxy (or legacy without sidecar).
    Logs go to [SETUP] when ``announce`` is True (slots that launch Chromium this run).
    """
    for p in (fp_txt, legacy_fp):
        if p.is_file() and not state_path.is_file():
            try:
                p.unlink()
            except OSError:
                pass

    if not state_path.is_file():
        if announce:
            print(
                f"[SETUP] No saved session yet — will create {state_path.name} after login.",
                flush=True,
            )
        return

    stored = _read_stored_proxy_fingerprint_hex(fp_txt, legacy_fp)

    if not stored:
        try:
            state_path.unlink()
        except OSError:
            pass
        for p in (fp_txt, legacy_fp):
            try:
                p.unlink()
            except OSError:
                pass
        if announce:
            print(
                f"[SETUP] Removed legacy {state_path.name} (no fingerprint sidecar) — "
                f"re-login required ({hint}).",
                flush=True,
            )
        return

    if stored == current_fp:
        if announce:
            print(
                f"[SETUP] Session file matches current proxy fingerprint "
                f"(session_{session_id}: {hint}).",
                flush=True,
            )
        return

    try:
        state_path.unlink()
    except OSError:
        pass
    for p in (fp_txt, legacy_fp):
        try:
            p.unlink()
        except OSError:
            pass
    short = f"{stored[:12]}…→{current_fp[:12]}…"
    if announce:
        print(
            f"[SETUP] Proxy changed — deleted stale session_{session_id}.json "
            f"(fingerprint {short}). Now: {hint}.",
            flush=True,
        )


def _persist_storage_state(
    context,
    state_path: Path,
    fp_bundle: tuple[Path, str, str] | None,
) -> None:
    try:
        context.storage_state(path=str(state_path))
    except Exception as e:
        print(f"[SESSION] save cookies: {e}", flush=True)
        return
    if not fp_bundle:
        return
    fp_txt, fp_hex, hint = fp_bundle
    legacy_fp = _session_fingerprint_legacy_fp_path(fp_txt.parent, _session_id_from_session_json_path(state_path))
    body = (
        f"{fp_hex}\n"
        f"# sha256(host:port:user:pass) for this session's proxy.txt line — {hint}\n"
    )
    try:
        fp_txt.write_text(body, encoding="utf-8")
        if legacy_fp.is_file():
            try:
                legacy_fp.unlink()
            except OSError:
                pass
    except OSError as e:
        print(f"[SESSION] fingerprint file save failed: {e}", flush=True)


def _session_id_from_session_json_path(state_path: Path) -> int:
    """session_3.json → 3."""
    m = re.search(r"session_(\d+)\.json$", state_path.name, re.I)
    return int(m.group(1)) if m else 1


def _pause_online_duration() -> float:
    lo = float(os.getenv("BOL_BROWSER_POLL_ONLINE_MIN", "2"))
    hi = float(os.getenv("BOL_BROWSER_POLL_ONLINE_MAX", "5"))
    if hi < lo:
        lo, hi = hi, lo
    return random.uniform(lo, hi)


def _pause_offline_duration() -> float:
    lo = float(os.getenv("BOL_BROWSER_POLL_OFFLINE_MIN", "40"))
    hi = float(os.getenv("BOL_BROWSER_POLL_OFFLINE_MAX", "60"))
    if hi < lo:
        lo, hi = hi, lo
    return random.uniform(lo, hi)


def _pause_online() -> None:
    time.sleep(_pause_online_duration())


def _pause_offline() -> None:
    time.sleep(_pause_offline_duration())


def _parse_all_proxy_lines(proxy_file: Path) -> list[tuple[str, str, str, str]]:
    """All valid host:port:user:pass lines in file order."""
    out: list[tuple[str, str, str, str]] = []
    try:
        raw_lines = proxy_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for line in raw_lines:
        line = _scrub_proxy_txt_line_raw(line)
        if not line:
            continue
        parts = line.split(":")
        if len(parts) != 4:
            continue
        out.append(
            (parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3].strip())
        )
    return out


DEFAULT_FAILOVER_POOL_FILE = "proxies.txt"


class ProxyCooldownRegistry:
    """Shared across slot threads: mark dead proxies until cooldown expires."""

    _lock = threading.Lock()
    _until: dict[str, float] = {}

    @classmethod
    def mark(cls, host: str, port: str, user: str, password: str, seconds: float) -> None:
        fp = _proxy_tuple_fingerprint(host, port, user, password)
        until = time.time() + max(1.0, float(seconds))
        with cls._lock:
            cls._until[fp] = max(cls._until.get(fp, 0.0), until)

    @classmethod
    def cooling_remaining(cls, host: str, port: str, user: str, password: str) -> float:
        fp = _proxy_tuple_fingerprint(host, port, user, password)
        with cls._lock:
            u = cls._until.get(fp, 0.0)
        return max(0.0, u - time.time())

    @classmethod
    def is_cooling(cls, host: str, port: str, user: str, password: str) -> bool:
        return cls.cooling_remaining(host, port, user, password) > 0.0

    @classmethod
    def min_remaining_chain(cls, chain: list[tuple[str, str, str, str]]) -> float:
        if not chain:
            return 0.0
        return min(
            (cls.cooling_remaining(*t) for t in chain),
            default=0.0,
        )


def _ensure_failover_pool_file(path: Path) -> None:
    if path.is_file():
        return
    try:
        path.write_text(
            "# Extra proxies for browser-mode failover (same format as proxy.txt: host:port:user:pass).\n"
            "# On login/session break the bot clears cookies, picks the next non-cooling line here,\n"
            "# waits a few seconds, and logs in again — not used for normal offline PDP / IP-shell pages.\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def _unique_proxy_chain(
    primary: tuple[str, str, str, str], pool: list[tuple[str, str, str, str]]
) -> list[tuple[str, str, str, str]]:
    seen: set[str] = set()
    out: list[tuple[str, str, str, str]] = []
    for t in (primary, *pool):
        fp = _proxy_tuple_fingerprint(*t)
        if fp in seen:
            continue
        seen.add(fp)
        out.append(t)
    return out if out else [primary]


def _pick_live_proxy_index(
    chain: list[tuple[str, str, str, str]], start_idx: int
) -> tuple[int, tuple[str, str, str, str]] | None:
    n = len(chain)
    for step in range(n):
        idx = (start_idx + step) % n
        t = chain[idx]
        if not ProxyCooldownRegistry.is_cooling(*t):
            return idx, t
    return None


def _clear_slot_session_storage(
    state_path: Path, fp_txt: Path, legacy_fp: Path, sp: str
) -> None:
    for p in (state_path, fp_txt, legacy_fp):
        try:
            if p.is_file():
                p.unlink()
        except OSError as e:
            print(f"{sp}[FAILOVER] Could not remove {p.name}: {e}", flush=True)


def _failover_relogin_delay_sec() -> float:
    lo = float(os.getenv("BOL_BROWSER_FAILOVER_RELOGIN_MIN_SEC", "10"))
    hi = float(os.getenv("BOL_BROWSER_FAILOVER_RELOGIN_MAX_SEC", "30"))
    if hi < lo:
        lo, hi = hi, lo
    return random.uniform(lo, hi)


def _failover_proxy_cooldown_sec() -> float:
    return float(os.getenv("BOL_BROWSER_PROXY_FAIL_COOLDOWN_SEC", "900"))


def _nl_storefront_modal_like_visible(page, *, timeout_ms: int | None = None) -> bool:
    """
    Any blocking modal/banner shell — even before privacy copy hydrates or regex filters match.
    Used so we do not early-return out of dismiss while a sheet still covers clicks.
    """
    if timeout_ms is None:
        timeout_ms = max(280, int(os.getenv("BOL_BROWSER_CMP_MODAL_DETECT_MS", "780")))
    for dlg_sel in ('[aria-modal="true"]', '[role="dialog"]'):
        try:
            if page.locator(dlg_sel).first.is_visible(timeout=timeout_ms):
                return True
        except Exception:
            pass
    for sel in (
        "#onetrust-banner-sdk",
        '[id^="onetrust"]',
        '[class*="cookie-banner"]',
        '[data-test="cookie-banner"]',
    ):
        try:
            if page.locator(sel).first.is_visible(timeout=min(550, timeout_ms)):
                return True
        except Exception:
            pass
    return False


def _nl_storefront_cmp_accept_button_visible(page) -> bool:
    """Accept-all button visible anywhere (main DOM) — counts as blocking until clicked."""
    ac = re.compile(r"^(Alles\s+accepteren|Accept\s+all)$", re.I)
    ms = max(400, int(os.getenv("BOL_BROWSER_CMP_ACCEPT_DETECT_MS", "850")))
    try:
        if page.get_by_role("button", name=ac).first.is_visible(timeout=ms):
            return True
    except Exception:
        pass
    return False


def _nl_storefront_cmp_still_blocking(page) -> bool:
    """True while any heuristic says the cookie/CMP layer still needs dismissing."""
    return (
        _nl_storefront_privacy_dialog_visible(page)
        or _nl_storefront_modal_like_visible(page)
        or _nl_storefront_cmp_accept_button_visible(page)
    )


def _nl_storefront_privacy_dialog_visible(page) -> bool:
    """True when the NL storefront CMP / cookie sheet is likely open (needs Accept)."""
    priv_pat = re.compile(r"privacy|cookie|voorkeuren|preferences|consent", re.I)
    ac = re.compile(r"^(Alles\s+accepteren|Accept\s+all)$", re.I)
    quick = max(320, int(os.getenv("BOL_BROWSER_CMP_QUICK_DETECT_MS", "900")))
    try:
        if page.get_by_role("button", name=ac).first.is_visible(timeout=quick):
            return True
    except Exception:
        pass
    for dlg_sel in ('[role="dialog"]', '[aria-modal="true"]'):
        try:
            dlg_f = page.locator(dlg_sel).filter(has_text=priv_pat).first
            if dlg_f.is_visible(timeout=quick):
                return True
        except Exception:
            pass
        try:
            dlg_any = page.locator(dlg_sel).first
            if dlg_any.is_visible(timeout=quick):
                try:
                    if dlg_any.get_by_role("button", name=ac).first.is_visible(timeout=280):
                        return True
                except Exception:
                    pass
        except Exception:
            pass
    return False


def _login_cmp_verbose() -> bool:
    return os.getenv("BOL_BROWSER_LOGIN_CMP_VERBOSE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _login_home_header_verbose() -> bool:
    """Full `[LOGIN] … header snippet` dumps — off by default (was spam + felt like a hang)."""
    return os.getenv("BOL_BROWSER_LOGIN_HOME_VERBOSE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _header_auth_poll_ms() -> int:
    """Short waits inside polling loops — never stack multi‑second locator timeouts per tick."""
    return max(120, int(os.getenv("BOL_BROWSER_HEADER_AUTH_POLL_MS", "320")))


def _verify_browser_login_visible(page) -> bool:
    """
    After ensure_logged_in — confirm cookies/session without an automatic second homepage load.

    The old path always did goto(NL home) + cookie dismiss; on slow proxies / slot 3 that raced
    the header and triggered false [FAILOVER] → cleared cookies → endless homepage/CMP spam.
    """
    force_home = os.getenv("BOL_BROWSER_LOGIN_VERIFY_FORCE_HOME_GOTO", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    settle = max(0.2, float(os.getenv("BOL_BROWSER_LOGIN_VERIFY_SETTLE_SEC", "1.05")))
    try:
        page.bring_to_front()
    except Exception:
        pass

    def _session_evident() -> bool:
        if _header_shows_welkom_account(page):
            return True
        try:
            u = (page.url or "").lower()
        except Exception:
            u = ""
        if not _page_likely_nl_storefront_home(page) and "bol.com" in u:
            return bool(_is_logged_in(page))
        return False

    if not force_home:
        time.sleep(min(settle, 2.5))
        if _session_evident():
            return True
        try:
            u = (page.url or "").lower()
        except Exception:
            u = ""
        if "www.bol.com" in u and "login.bol.com" not in u:
            auth = _wait_nl_home_auth_after_cmp(page)
            return auth == "logged_in"

    try:
        if not _goto_with_retries(page, STOREFRONT_NL_URL, label="NL homepage (login verify)"):
            return False
        _ensure_nl_storefront_dom_ready(page)
        _micro_pause_after_nl_shell_ready()
        pq = _header_auth_poll_ms()
        if _nl_storefront_privacy_dialog_visible(page) or _auth_from_header_visible(
            page, header_wait_ms=pq
        ) is None:
            _dismiss_nl_storefront_privacy(page)
        _ensure_nl_storefront_shell_ready(page)
        time.sleep(min(settle, 1.4))
    except Exception:
        return False

    if _header_shows_welkom_account(page):
        return True
    auth = _wait_nl_home_auth_after_cmp(page)
    return auth == "logged_in"


def _ordered_http_products(products: list) -> list[tuple[dict, str]]:
    rows: list[tuple[dict, str]] = []
    for p in products or []:
        u = (p.get("product_url") or "").strip()
        if u.startswith("http"):
            rows.append((p, u))
    return rows


def _load_ordered_http_products_retry(mod, *, phase: str) -> tuple[list[tuple[dict, str]], str | None]:
    """
    Retry when ``product.csv`` is briefly unreadable (OneDrive sync / Excel lock). Without this,
    ``ordered`` can be empty right after login → every slot hits NL homepage reset and looks \"stuck loading\".
    """
    attempts = max(1, int(os.getenv("BOL_BROWSER_PRODUCT_LOAD_RETRIES", "12")))
    lo = float(os.getenv("BOL_BROWSER_PRODUCT_LOAD_RETRY_MIN_SEC", "0.05"))
    hi = float(os.getenv("BOL_BROWSER_PRODUCT_LOAD_RETRY_MAX_SEC", "0.38"))
    if hi < lo:
        lo, hi = hi, lo
    last_err: str | None = None
    for i in range(attempts):
        products, err = mod.try_load_products_runtime(mod.PRODUCTS_FILE)
        if err:
            last_err = err
            time.sleep(random.uniform(lo, hi))
            continue
        if not products:
            last_err = "empty product list"
            time.sleep(random.uniform(lo, hi))
            continue
        ordered = _ordered_http_products(products)
        if ordered:
            return ordered, None
        last_err = "no rows with http product_url"
        time.sleep(random.uniform(lo, hi))
    return [], last_err or f"{phase}: gave up after {attempts} read attempt(s)"


def _sticky_slot_product_url_default() -> bool:
    """When True (default), each Chromium keeps the product URL bound at process start — CSV edits do not retarget slots."""
    return os.getenv("BOL_BROWSER_STICKY_SLOT_PRODUCT_URL", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _pick_slot_product_url(
    slot: int,
    ordered_rows: list[tuple[dict, str]],
    slot_http_urls: list[str],
    *,
    sticky: bool,
) -> str:
    """
    ``sticky=True``: always the startup snapshot for this slot (proxy line N ↔ URL N at launch).
    ``sticky=False``: follow live CSV row order (legacy — adding rows above shifts slot targets).
    """
    if sticky:
        if slot < len(slot_http_urls) and (slot_http_urls[slot] or "").strip():
            return (slot_http_urls[slot] or "").strip()
        if slot < len(ordered_rows):
            return (ordered_rows[slot][1] or "").strip()
        return ""
    if slot < len(ordered_rows):
        return (ordered_rows[slot][1] or "").strip()
    if slot < len(slot_http_urls):
        return (slot_http_urls[slot] or "").strip()
    return ""


def _html_title_casefold(html: str) -> str:
    m = re.search(r"<title[^>]*>([^<]{0,400})", html or "", re.I | re.DOTALL)
    return ((m.group(1) if m else "") or "").strip().casefold()


def _html_looks_like_bol_oops_not_found(html: str) -> bool:
    """Bol serves HTTP 200 + generic \"Oeps / pagina niet gevonden\" shells on broken routes."""
    lc = (html or "").casefold()
    if "kunnen deze pagina niet meer vinden" in lc:
        return True
    if "oeps" in lc and "pagina niet gevonden" in lc:
        return True
    if "pagina niet gevonden" in lc and "naar de homepage" in lc:
        return True
    return False


def _html_looks_like_bol_access_blocked(html: str) -> bool:
    """
    Bol returns HTTP 200 with normal chrome but body is \"IP adres … is geblokkeerd\" /
    abuse notice — not a PDP. Without this, monitor logs false [MONITOR] online.
    """
    lc = (html or "").casefold()
    if ("ip adres" in lc or "ip-adres" in lc) and "geblokkeerd" in lc:
        return True
    if "toegang tot bol" in lc and "geblokkeerd" in lc:
        return True
    if "mogelijk misbruik" in lc and ("ip" in lc or "toegang" in lc):
        return True
    if "your access to bol" in lc and "block" in lc:
        return True
    return False


def _header_shows_guest_login(page) -> bool:
    """
    If main header shows Inloggen / Log in, user is not logged in — even when HTML still
    contains 'mijn account' in footers or promos (that substring caused false [LOGIN] Reused session).
    """
    try:
        hdr = page.locator("header").first
        if not hdr.is_visible(timeout=500):
            return False
        for name in (
            re.compile(r"^\s*Inloggen\s*$", re.I),
            re.compile(r"^\s*Log\s+in\s*$", re.I),
        ):
            try:
                if hdr.get_by_role("link", name=name).first.is_visible(timeout=450):
                    return True
            except Exception:
                pass
            try:
                if hdr.get_by_role("button", name=name).first.is_visible(timeout=450):
                    return True
            except Exception:
                pass
    except Exception:
        pass
    return False


def _header_shows_welkom_account(page) -> bool:
    """
    Session restored from cookies: header shows \"Welkom <naam>\" instead of Inloggen.
    Uses merged `header.inner_text()` so split spans (Welkom + name) still match — single-node
    get_by_text often misses Bol's chip DOM.
    """
    try:
        hdr = page.locator("header").first
        if not hdr.is_visible(timeout=1500):
            return False
        if _header_shows_guest_login(page):
            return False
        blob = re.sub(r"\s+", " ", (hdr.inner_text() or "")).strip()
        lc = blob.casefold()
        # NL chip — not \"Welkom bij bol\", \"Welkom terug\", etc.
        if re.search(r"\bwelkom\s+(?!bij\b)(\S+)", blob, re.I):
            return True
        # EN storefront sometimes
        if re.search(r"\bwelcome\s*,?\s+(?!to\b|back\b)([a-z]{2,40})", lc):
            return True
    except Exception:
        pass
    return False


def _log_homepage_header_auth(page, *, phase: str) -> None:
    """Visible diagnosis: guest Inloggen vs Welkom session vs unclear (verbose only)."""
    if not _login_home_header_verbose():
        return
    guest = False
    welkom = False
    try:
        guest = _header_shows_guest_login(page)
        welkom = _header_shows_welkom_account(page)
    except Exception:
        pass
    snippet = ""
    try:
        hdr = page.locator("header").first
        snippet = (hdr.inner_text() or "").strip()
        snippet = re.sub(r"\s+", " ", snippet)[:380]
    except Exception:
        snippet = ""
    if not snippet:
        try:
            snippet = re.sub(r"\s+", " ", (page.title() or "").strip())[:380]
        except Exception:
            snippet = ""
    if not snippet:
        try:
            udbg = (page.url or "").strip()
            snippet = f"(no header/title text yet) url={udbg[:200]}"
        except Exception:
            snippet = "(header unreadable)"
    if welkom:
        state = "logged in (Welkom / account chip — not Inloggen)"
    elif guest:
        state = "guest (Inloggen visible)"
    else:
        state = "unclear — inspect snippet"
    _lprint(f"[LOGIN] {phase} — {state}")
    _lprint(f"[LOGIN] {phase} — header snippet: {snippet!r}")


def _auth_from_header_visible(page, *, header_wait_ms: int | None = None) -> str | None:
    """
    Authoritative auth signal from the visible storefront header only.
    Returns 'guest' (Inloggen), 'logged_in' (Welkom chip), or None if header not ready yet.

    Use ``header_wait_ms`` for tight poll loops (default env ``BOL_BROWSER_HEADER_AUTH_POLL_MS``).
    Omit for one-shot checks after CMP (uses ``BOL_BROWSER_HEADER_AUTH_LOCATOR_MS`` — default fast).
    """
    try:
        if header_wait_ms is None:
            ms = max(280, int(os.getenv("BOL_BROWSER_HEADER_AUTH_LOCATOR_MS", "900")))
        else:
            ms = max(120, int(header_wait_ms))
        hdr = page.locator("header").first
        if not hdr.is_visible(timeout=ms):
            return None
    except Exception:
        return None
    if _header_shows_guest_login(page):
        return "guest"
    if _header_shows_welkom_account(page):
        return "logged_in"
    return None


def _wait_nl_home_auth_after_cmp(page) -> str:
    """
    After homepage + CMP: brief polls for header guest/Welkom — default **short** so threads never
    sit on NL home while another slot runs checkout. Set ``BOL_BROWSER_HEADER_AUTH_WAIT_SEC`` higher
    only if you need slower storefronts.
    """
    mx = float(os.getenv("BOL_BROWSER_HEADER_AUTH_WAIT_SEC", "2.2"))
    poll = _header_auth_poll_ms()
    t0 = time.time()
    n = 0
    tick = max(0.08, float(os.getenv("BOL_BROWSER_HEADER_AUTH_POLL_SLEEP_SEC", "0.12")))
    while time.time() - t0 < mx:
        if not _page_likely_nl_storefront_home(page):
            if _is_logged_in(page):
                return "logged_in"
            return "guest"
        a = _auth_from_header_visible(page, header_wait_ms=poll)
        if a == "guest":
            return "guest"
        if a == "logged_in":
            return "logged_in"
        n += 1
        if n % 5 == 0:
            try:
                _dismiss_cookie_wall(page)
            except Exception:
                pass
        time.sleep(tick)

    a_final = _auth_from_header_visible(page)
    if a_final == "guest":
        return "guest"
    if a_final == "logged_in":
        return "logged_in"

    _log_homepage_header_auth(page, phase="Header auth wait exhausted — conservative fallback")
    try:
        if _header_shows_welkom_account(page):
            return "logged_in"
    except Exception:
        pass
    return "guest"


def _logged_in_for_homepage_flow(page) -> bool:
    """Used during homepage login path — Welkom in header or logged-in on non-home bol routes."""
    if _page_likely_nl_storefront_home(page):
        pq = _header_auth_poll_ms()
        a = _auth_from_header_visible(page, header_wait_ms=pq)
        if a == "logged_in":
            return True
        if a == "guest":
            return False
        time.sleep(max(0.0, float(os.getenv("BOL_BROWSER_HOME_AUTH_RETRY_SLEEP_SEC", "0.08"))))
        a = _auth_from_header_visible(page)
        if a == "logged_in":
            return True
        if a == "guest":
            return False
        return False
    return _is_logged_in(page)


def _is_logged_in(page) -> bool:
    u = (page.url or "").lower()
    if "account/login" in u:
        return False

    # NL homepage: never trust footer/title \"logout\" heuristics — only header Welkom vs Inloggen.
    if _page_likely_nl_storefront_home(page):
        auth = _auth_from_header_visible(page, header_wait_ms=_header_auth_poll_ms())
        if auth is None:
            auth = _auth_from_header_visible(page)
        if auth == "guest":
            return False
        if auth == "logged_in":
            return True
        try:
            hdr = page.locator("header").first
            if hdr.is_visible(timeout=400):
                return False
        except Exception:
            pass
        return False

    if _header_shows_guest_login(page):
        return False

    if _header_shows_welkom_account(page):
        return True

    try:
        if page.get_by_text(re.compile(r"Bestellingen", re.I)).first.is_visible(timeout=800):
            return True
    except Exception:
        pass

    # Account hub when logged in: classic /nl/nl/account/... or newer /nl/rnwy/account/... (post-login redirect).
    if "bol.com" in u and "/account/" in u and "account/login" not in u:
        if any(
            x in u
            for x in (
                "account/registr",
                "account/aanmaken",
                "account/wachtwoord-vergeten",
            )
        ):
            return False
        try:
            if _html_looks_like_bol_oops_not_found(page.content()):
                return False
        except Exception:
            pass
        return True

    try:
        content = page.content().lower()
        # Do not use loose "mijn account" — appears for guests (marketing/footer).
        if "account/logout" in content:
            if _header_shows_guest_login(page):
                return False
            return True
    except Exception:
        pass

    # Title on account overview is often "… | Mijn bol | …" while URL can be rnwy (not matched above during navigation).
    try:
        tit = (page.title() or "").casefold()
        if "bol.com" in u and "mijn bol" in tit and "inloggen" not in tit:
            return True
    except Exception:
        pass

    return False


def probe_logged_in_via_account(page, *, announce: bool = True) -> bool:
    """
    Never open /nl/nl/account/ alone — Bol often returns a 404 \"Oeps\" shell there while
    the header still shows \"Welkom\" (session cookies OK but route is dead). Only re-check
    the NL storefront for logged-in chrome.
    """
    try:
        if not _goto_with_retries(page, "https://www.bol.com/nl/nl/", label="probe storefront"):
            return False
        _ensure_nl_storefront_dom_ready(page)
        _micro_pause_after_nl_shell_ready()
        _dismiss_cookie_wall(page)
        time.sleep(0.35)
        _dismiss_cookie_wall(page)
        _ensure_nl_storefront_shell_ready(page)
        ok = _is_logged_in(page)
        if ok and announce:
            _lprint("[LOGIN] Session OK (storefront reload — cookies/session recognized).")
        return ok
    except Exception:
        pass
    return False


def _reset_to_nl_storefront(page, *, sp: str) -> None:
    """After login checks, land on NL home before PDP monitoring. Skips a duplicate goto when already home + Welkom."""
    skip_extra_goto = False
    try:
        if _page_likely_nl_storefront_home(page):
            au = _auth_from_header_visible(page, header_wait_ms=_header_auth_poll_ms())
            if au is None:
                time.sleep(0.55)
                au = _auth_from_header_visible(page)
            if au == "logged_in":
                if _nl_storefront_home_usable(page):
                    skip_extra_goto = True
                else:
                    print(
                        f"{sp}[MONITOR] Welkom in header but homepage still loading / thin shell — "
                        f"forcing NL storefront navigation…",
                        flush=True,
                    )
    except Exception:
        pass

    if skip_extra_goto:
        print(
            f"{sp}[MONITOR] Already on NL storefront (Welkom + usable shell) — skipping extra home navigation.",
            flush=True,
        )
        try:
            page.bring_to_front()
        except Exception:
            pass
        if _nl_storefront_privacy_dialog_visible(page):
            _dismiss_cookie_wall(page)
            time.sleep(0.22)
        return

    print(f"{sp}[MONITOR] Opening NL storefront before product URLs…", flush=True)
    try:
        if not _goto_with_retries(page, "https://www.bol.com/nl/nl/", label="storefront reset"):
            print(
                f"{sp}[MONITOR] storefront reset: navigation failed — PDP steps may still run.",
                flush=True,
            )
            return
        _ensure_nl_storefront_dom_ready(page)
        _micro_pause_after_nl_shell_ready()
        try:
            page.bring_to_front()
        except Exception:
            pass
        _dismiss_cookie_wall(page)
        time.sleep(0.35)
        _dismiss_cookie_wall(page)
        _ensure_nl_storefront_shell_ready(page)
    except Exception as e:
        print(f"{sp}[MONITOR] storefront reset: {e}", flush=True)


def _login_debug_line(page) -> str:
    """Prefer title/URL — avoid page.content() (throws while page is navigating)."""
    try:
        u = page.url or ""
    except Exception as e:
        return f"url unavailable ({e})"
    try:
        t = (page.title() or "").strip()
        if len(t) > 180:
            t = t[:180] + "…"
    except Exception:
        t = "(title unavailable — navigating?)"
    return f"url={u!r} title≈{t!r}"


def _fill_login_in_frame(
    frame,
    email: str,
    password: str,
    *,
    pause_before_submit_sec: float = 0.0,
) -> bool:
    # Same order as _wait_for_bol_login_form_ready email_bundle first → fast match after \"form ready\".
    vw = max(400, int(os.getenv("BOL_BROWSER_LOGIN_FIELD_LOCATOR_WAIT_MS", "2400")))
    email_candidates = (
        'input[type="email"]',
        'input[name="loginFormEmail"]',
        "#loginFormEmail",
        'input[autocomplete="username"]',
        'input[autocomplete="email"]',
        'input[inputmode="email"]',
        'input[id*="email" i]',
        'input[placeholder*="e-mail" i]',
        'input[placeholder*="mail" i]',
        'input[aria-label*="mail" i]',
    )
    pw_candidates = (
        'input[type="password"]',
        "#loginFormPassword",
        'input[name="loginFormPassword"]',
        'input[autocomplete="current-password"]',
        'input[placeholder*="wachtwoord" i]',
        'input[aria-label*="wachtwoord" i]',
    )
    email_loc = None
    for sel in email_candidates:
        try:
            loc = frame.locator(sel).first
            loc.wait_for(state="visible", timeout=vw)
            email_loc = loc
            break
        except Exception:
            continue
    if email_loc is None:
        try:
            ev = frame.locator(
                'input[type="email"]:visible, '
                'input[name*="mail" i]:visible, '
                'input[inputmode="email"]:visible'
            )
            pv = frame.locator('input[type="password"]:visible')
            if ev.count() >= 1 and pv.count() >= 1:
                email_loc = ev.first
                pw_loc_single = pv.first
                email_loc.click(timeout=5000)
                email_loc.fill(email)
                time.sleep(random.uniform(0.05, 0.14))
                pw_loc_single.click(timeout=5000)
                pw_loc_single.fill(password)
                time.sleep(random.uniform(0.1, 0.28))
                if pause_before_submit_sec > 0:
                    time.sleep(max(0.0, float(pause_before_submit_sec)))
                for btn_sel in (
                    'button[type="submit"]',
                    'button:has-text("Inloggen")',
                    '[data-test="login-button"]',
                    'input[type="submit"]',
                ):
                    try:
                        b = frame.locator(btn_sel).first
                        if b.is_visible(timeout=600):
                            b.click(timeout=8000)
                            return True
                    except Exception:
                        continue
                return False
        except Exception:
            pass
    if email_loc is None:
        return False
    pw_loc = None
    for sel in pw_candidates:
        try:
            loc = frame.locator(sel).first
            loc.wait_for(state="visible", timeout=vw)
            pw_loc = loc
            break
        except Exception:
            continue
    if pw_loc is None:
        return False
    try:
        email_loc.click(timeout=5000)
    except Exception:
        pass
    email_loc.fill(email)
    time.sleep(random.uniform(0.05, 0.14))
    try:
        pw_loc.click(timeout=5000)
    except Exception:
        pass
    pw_loc.fill(password)
    time.sleep(random.uniform(0.1, 0.28))
    if pause_before_submit_sec > 0:
        time.sleep(max(0.0, float(pause_before_submit_sec)))
    for btn_sel in (
        'button[type="submit"]',
        'button:has-text("Inloggen")',
        '[data-test="login-button"]',
        'input[type="submit"]',
    ):
        try:
            b = frame.locator(btn_sel).first
            if b.is_visible(timeout=600):
                b.click(timeout=8000)
                return True
        except Exception:
            continue
    return False


def _try_fill_login_via_accessible_labels(
    page,
    email: str,
    password: str,
    *,
    pause_before_submit_sec: float = 0.0,
) -> bool:
    """Bol NL login sometimes exposes Dutch labels only (E-mailadres / Wachtwoord)."""
    try:
        page.get_by_label(re.compile(r"(e-mail|emailadres)", re.I)).first.fill(
            email, timeout=7000
        )
        page.get_by_label(re.compile(r"wachtwoord", re.I)).first.fill(
            password, timeout=7000
        )
        time.sleep(random.uniform(0.25, 0.55))
        if pause_before_submit_sec > 0:
            time.sleep(max(0.0, float(pause_before_submit_sec)))
        for pat in (
            re.compile(r"^\s*Inloggen\s*$", re.I),
            re.compile(r"^\s*Log\s+in\s*$", re.I),
        ):
            try:
                b = page.get_by_role("button", name=pat).first
                if b.is_visible(timeout=900):
                    b.click(timeout=10000)
                    return True
            except Exception:
                continue
        try:
            page.locator('button[type="submit"]:visible').first.click(timeout=10000)
            return True
        except Exception:
            pass
    except Exception:
        pass
    return False


def _try_fill_login_form(
    page,
    email: str,
    password: str,
    *,
    pause_before_submit_sec: float = 0.0,
) -> bool:
    frames_to_try = [page.main_frame]
    try:
        frames_to_try.extend([f for f in page.frames if f is not page.main_frame])
    except Exception:
        pass
    kw = {"pause_before_submit_sec": pause_before_submit_sec}
    for fr in frames_to_try:
        if _fill_login_in_frame(fr, email, password, **kw):
            return True
    return _try_fill_login_via_accessible_labels(
        page, email, password, pause_before_submit_sec=pause_before_submit_sec
    )


def _dismiss_cookie_wall(page) -> None:
    """
    CMP / privacy modal can be EN (\"Accept all\") or NL; buttons are often below the fold.
    Also checks iframes (OneTrust / similar). Safe to call repeatedly.

    On login.bol.com / account/login: do **not** press Escape — Bol closes the login sheet and
    can send the tab back to homepage. Cookie-only selectors there (no bare \"Accepteren\").
    """
    try:
        u = (page.url or "").lower()
        on_login_host = "login.bol.com" in u or "/account/login" in u
    except Exception:
        on_login_host = False

    accept_name = re.compile(
        r"^(Accept\s+all|Accept|Allow\s+all|Agree|OK|Alles\s+accepteren|"
        r"Alle\s+cookies\s+accepteren|Ik\s+ga\s+akkoord)$",
        re.I,
    )

    vw = max(400, int(os.getenv("BOL_BROWSER_COOKIE_VISIBLE_WAIT_MS", "1100")))
    sc = max(600, int(os.getenv("BOL_BROWSER_COOKIE_SCROLL_MS", "2200")))
    ck = max(800, int(os.getenv("BOL_BROWSER_COOKIE_CLICK_MS", "3200")))

    def _try_click(locator) -> bool:
        try:
            if not locator.count():
                return False
            first = locator.first
            try:
                first.wait_for(state="visible", timeout=vw)
            except Exception:
                if not first.is_visible():
                    return False
            first.scroll_into_view_if_needed(timeout=sc)
            time.sleep(0.08)
            first.click(timeout=ck, no_wait_after=True)
            time.sleep(0.22)
            return True
        except Exception:
            return False

    targets: list = [page]
    try:
        targets.extend([f for f in page.frames if f != page.main_frame])
    except Exception:
        pass

    # Avoid bare \"Accepteren\" — matches non-cookie UI; on login pages can mis-click.
    css_selectors = (
        'button:has-text("Accept all")',
        'button:has-text("Accept All")',
        'a:has-text("Accept all")',
        'button:has-text("Alles accepteren")',
        '[data-test="consent-accept"]',
        'button[id*="onetrust-accept"]',
        'button[aria-label*="Accept"]',
        'button[aria-label*="Accepteren"]',
    )
    if not on_login_host:
        css_selectors += ('button:has-text("Accepteren")',)

    for ctx in targets:
        for sel in css_selectors:
            if _try_click(ctx.locator(sel)):
                return
        try:
            if _try_click(ctx.get_by_role("button", name=accept_name)):
                return
        except Exception:
            pass
        for dlg_sel in ('[role="dialog"]', '[aria-modal="true"]'):
            try:
                dlg = ctx.locator(dlg_sel).first
                if dlg.is_visible(timeout=800):
                    for sel in css_selectors:
                        if _try_click(dlg.locator(sel)):
                            return
                    if _try_click(dlg.get_by_role("button", name=accept_name)):
                        return
            except Exception:
                continue

    # Tall CMP on storefront — NL \"Jouw privacyvoorkeuren\" / EN preferences; never on login host.
    if not on_login_host:
        try:
            priv_hint = page.get_by_text(
                re.compile(r"privacy\s*(voorkeuren|preferences)|privacy\s+preferences", re.I)
            )
            if priv_hint.first.is_visible(timeout=900):
                try:
                    page.keyboard.press("End")
                    time.sleep(0.28)
                except Exception:
                    pass
                for sel in (
                    'button:has-text("Alles accepteren")',
                    'button:has-text("Accept all")',
                    'button:has-text("Accept All")',
                    'button:has-text("Accepteren")',
                ):
                    if _try_click(page.locator(sel)):
                        return
                try:
                    if _try_click(
                        page.get_by_role(
                            "button",
                            name=re.compile(r"^(Alles\s+accepteren|Accept\s+all)$", re.I),
                        )
                    ):
                        return
                except Exception:
                    pass
        except Exception:
            pass

    if not on_login_host:
        try:
            page.keyboard.press("Escape")
            time.sleep(0.2)
        except Exception:
            pass


def _dismiss_nl_storefront_privacy(page) -> None:
    """
    NL homepage CMP sits above the header — Inloggen is not clickable until accepted.

    Uses **blocking** heuristics only for the fast path (no guest/header early-return guesswork).
    Several rounds + **iframe** sweep: cold-login slots often stayed under the sheet while a slot
    with ``session_n.json`` skipped straight to Welkom.
    """
    try:
        if "www.bol.com" not in (page.url or "").lower():
            return
    except Exception:
        return

    if not _nl_storefront_cmp_still_blocking(page):
        return

    rounds = max(1, min(12, int(os.getenv("BOL_BROWSER_CMP_DISMISS_ROUNDS", "5"))))
    round_sleep = float(os.getenv("BOL_BROWSER_CMP_DISMISS_ROUND_SLEEP_SEC", "0.18"))

    if _login_cmp_verbose():
        _lprint("[LOGIN] Homepage: clearing privacy/cookie overlay so Inloggen is reachable…")

    ac = re.compile(r"^(Alles\s+accepteren|Accept\s+all)$", re.I)
    dlg_vis = max(600, int(os.getenv("BOL_BROWSER_CMP_DIALOG_VISIBLE_MS", "1200")))
    dlg_scroll = max(600, int(os.getenv("BOL_BROWSER_CMP_SCROLL_MS", "1800")))
    dlg_click = max(800, int(os.getenv("BOL_BROWSER_CMP_ACCEPT_CLICK_MS", "4000")))
    priv_pat = re.compile(r"privacy|cookie|voorkeuren|preferences|consent", re.I)

    def _try_accept_in_dialog(dlg) -> bool:
        try:
            page.keyboard.press("End")
            time.sleep(0.16)
        except Exception:
            pass
        try:
            btn = dlg.get_by_role("button", name=ac).first
            btn.scroll_into_view_if_needed(timeout=dlg_scroll)
            time.sleep(0.1)
            btn.click(timeout=dlg_click, no_wait_after=True)
            time.sleep(0.25)
            return True
        except Exception:
            pass
        for sel in (
            'button:has-text("Alles accepteren")',
            'button:has-text("Accept all")',
            'button:has-text("Accept All")',
        ):
            try:
                loc = dlg.locator(sel).first
                loc.wait_for(state="visible", timeout=1600)
                loc.scroll_into_view_if_needed(timeout=dlg_scroll)
                time.sleep(0.08)
                loc.click(timeout=dlg_click, no_wait_after=True)
                time.sleep(0.25)
                return True
            except Exception:
                continue
        return False

    def _cmp_contexts():
        ctxs: list = [page]
        try:
            ctxs.extend([f for f in page.frames if f is not page.main_frame])
        except Exception:
            pass
        return ctxs

    def _one_round() -> None:
        try:
            page.keyboard.press("End")
            time.sleep(0.14)
        except Exception:
            pass
        _dismiss_cookie_wall(page)
        time.sleep(0.1)
        _dismiss_cookie_wall(page)
        time.sleep(0.1)

        for ctx in _cmp_contexts():
            for dlg_sel in ('[role="dialog"]', '[aria-modal="true"]'):
                try:
                    dlg_f = ctx.locator(dlg_sel).filter(has_text=priv_pat).first
                    if dlg_f.is_visible(timeout=min(dlg_vis, 1600)) and _try_accept_in_dialog(dlg_f):
                        return
                except Exception:
                    pass
                try:
                    dlg_any = ctx.locator(dlg_sel).first
                    if dlg_any.is_visible(timeout=min(dlg_vis, 1000)) and _try_accept_in_dialog(
                        dlg_any
                    ):
                        return
                except Exception:
                    pass
            try:
                btn = ctx.get_by_role("button", name=ac).first
                if btn.is_visible(timeout=900):
                    btn.scroll_into_view_if_needed(timeout=dlg_scroll)
                    time.sleep(0.08)
                    btn.click(timeout=dlg_click, no_wait_after=True)
                    time.sleep(0.22)
                    return
            except Exception:
                pass
            for sel in (
                'button:has-text("Alles accepteren")',
                'button:has-text("Accept all")',
                'button:has-text("Accept All")',
            ):
                try:
                    loc = ctx.locator(sel).first
                    if loc.is_visible(timeout=900):
                        loc.scroll_into_view_if_needed(timeout=dlg_scroll)
                        time.sleep(0.06)
                        loc.click(timeout=dlg_click, no_wait_after=True)
                        time.sleep(0.22)
                        return
                except Exception:
                    pass
        try:
            page.get_by_role("button", name=ac).first.click(
                timeout=dlg_click, no_wait_after=True
            )
            time.sleep(0.2)
        except Exception:
            pass
        try:
            page.keyboard.press("Escape")
            time.sleep(0.12)
        except Exception:
            pass

    for r in range(rounds):
        if not _nl_storefront_cmp_still_blocking(page):
            break
        _one_round()
        if not _nl_storefront_cmp_still_blocking(page):
            break
        if r + 1 < rounds:
            time.sleep(round_sleep)


def _goto_with_retries(
    page,
    url: str,
    *,
    label: str,
    timeout_ms: int = 90000,
) -> bool:
    """
    Proxies often cause net::ERR_ABORTED on first navigation to login.bol.com — retry with
    different wait_until strategies and short backoff (same URL; no flow change).
    """
    attempts = max(1, int(os.getenv("BOL_BROWSER_LOGIN_GOTO_ATTEMPTS", "3")))
    ul = (url or "").lower()
    storefront_nl = "www.bol.com" in ul and "/nl/nl" in ul
    # NL storefront home: **domcontentloaded first** — same usable shell for new proxy (no cookies)
    # and reused session; avoids \"commit\" returning before DOM/header exist (felt like a hang on bol.com/nl/nl).
    if storefront_nl:
        wait_modes = ("domcontentloaded", "commit", "load")
        try:
            cap = int(os.getenv("BOL_BROWSER_STOREFRONT_GOTO_TIMEOUT_MS", "70000"))
            timeout_ms = min(timeout_ms, max(25000, cap))
        except ValueError:
            pass
    elif "login.bol.com" in ul:
        wait_modes = ("commit", "domcontentloaded", "load")
    else:
        wait_modes = ("domcontentloaded", "commit", "load")
    last_err: Exception | None = None
    for i in range(attempts):
        w = wait_modes[i % len(wait_modes)]
        try:
            page.goto(url, wait_until=w, timeout=timeout_ms)
            if i:
                _lprint(f"[LOGIN] Navigation OK ({label}) — attempt {i + 1}/{attempts}, wait_until={w}.")
            return True
        except Exception as e:
            last_err = e
            _lprint(f"[LOGIN] goto {label} attempt {i + 1}/{attempts} ({w}) failed: {e}")
            time.sleep(min(10.0, 1.2 + i * 1.1 + random.uniform(0.25, 1.8)))
    if last_err:
        _lprint(f"[LOGIN] goto {label} gave up after {attempts} attempt(s): {last_err}")
    return False


def _ensure_nl_storefront_dom_ready(page) -> None:
    """
    DOM only — use **before** NL CMP/cookie dismiss. Waiting for ``header`` first can block ~14s while
    the privacy sheet covers the page; parallel slots then look like one is \"stuck on dismiss\".
    """
    try:
        page.wait_for_load_state(
            "domcontentloaded",
            timeout=max(4000, int(os.getenv("BOL_BROWSER_DOMCONTENTLOADED_TIMEOUT_MS", "12000"))),
        )
    except Exception:
        pass


def _ensure_nl_storefront_shell_ready(page) -> None:
    """
    After CMP cleared: DOM ready always; **header wait off by default** so parallel tabs do not block
    on ``header.is_visible``. Enable with ``BOL_BROWSER_LOGIN_WAIT_FOR_HEADER=1``.
    """
    try:
        page.wait_for_load_state(
            "domcontentloaded",
            timeout=max(4000, int(os.getenv("BOL_BROWSER_DOMCONTENTLOADED_TIMEOUT_MS", "12000"))),
        )
    except Exception:
        pass
    if os.getenv("BOL_BROWSER_LOGIN_WAIT_FOR_HEADER", "0").strip().lower() not in (
        "1",
        "true",
        "yes",
    ):
        return
    try:
        ms = max(400, int(os.getenv("BOL_BROWSER_HEADER_VISIBLE_MAX_MS", "1800")))
        page.locator("header").first.wait_for(state="visible", timeout=ms)
    except Exception:
        pass


def _micro_pause_after_nl_shell_ready() -> None:
    """Optional jitter — defaults near-zero so NL home does not artificial-delay threads."""
    lo = float(os.getenv("BOL_BROWSER_LOGIN_POST_HOME_SHELL_MIN_SEC", "0"))
    hi = float(os.getenv("BOL_BROWSER_LOGIN_POST_HOME_SHELL_MAX_SEC", "0.02"))
    if hi < lo:
        lo, hi = hi, lo
    time.sleep(random.uniform(lo, hi))


def _wait_for_bol_login_form_ready(page, *, timeout_ms: int | None = None) -> bool:
    """
    Poll until email + password inputs are visible. No URL-based refresh loops — redirect chains
    can briefly report www.bol.com and caused harmful repeated goto(login).
    """
    if timeout_ms is None:
        timeout_ms = max(8000, int(float(os.getenv("BOL_BROWSER_LOGIN_FORM_WAIT_MS", "45000"))))
    deadline = time.time() + timeout_ms / 1000.0
    email_bundle = (
        'input[type="email"]',
        'input[name="loginFormEmail"]',
        "#loginFormEmail",
        'input[autocomplete="username"]',
        'input[autocomplete="email"]',
        'input[inputmode="email"]',
    )
    pw_sel = 'input[type="password"]'

    while time.time() < deadline:
        frames = [page.main_frame]
        try:
            frames.extend([f for f in page.frames if f is not page.main_frame])
        except Exception:
            pass
        for fr in frames:
            try:
                for esel in email_bundle:
                    em = fr.locator(esel).first
                    pw = fr.locator(pw_sel).first
                    try:
                        if em.is_visible(timeout=1200) and pw.is_visible(timeout=1200):
                            return True
                    except Exception:
                        continue
            except Exception:
                continue
        time.sleep(max(0.12, float(os.getenv("BOL_BROWSER_LOGIN_FORM_POLL_SLEEP_SEC", "0.28"))))
    return False


def _page_likely_nl_storefront_home(page) -> bool:
    """True only for NL storefront root — not PDP, basket, account routes, etc."""
    try:
        u = page.url or ""
        p = urlparse(u)
        host = (p.netloc or "").lower()
        path = (p.path or "").rstrip("/").lower()
    except Exception:
        return False
    if "www.bol.com" not in host:
        return False
    if path in ("/nl/nl", ""):
        return True
    return path == "/nl/nl/index.html"


def _nl_storefront_home_usable(page) -> bool:
    """
    NL ``/nl/nl`` can expose header (Welkom) before the main shell hydrates — third Chromium /
    slow proxy then looked \"stuck\" on an empty storefront. Require ``main`` or a sizable body.
    """
    try:
        page.locator("main").first.wait_for(
            state="visible",
            timeout=max(400, int(os.getenv("BOL_BROWSER_HOME_MAIN_VISIBLE_MS", "1400"))),
        )
        return True
    except Exception:
        pass
    try:
        n = int(os.getenv("BOL_BROWSER_HOME_USABLE_MIN_HTML_CHARS", "32000"))
        if n < 8000:
            n = 8000
        return len(page.content() or "") >= n
    except Exception:
        return False


def _browser_login_pause_home_before_inloggen() -> None:
    lo = float(os.getenv("BOL_BROWSER_LOGIN_HOME_PAUSE_MIN_SEC", "0"))
    hi = float(os.getenv("BOL_BROWSER_LOGIN_HOME_PAUSE_MAX_SEC", "0.06"))
    if hi < lo:
        lo, hi = hi, lo
    t = random.uniform(lo, hi)
    if t > 0.02:
        _lprint(f"[LOGIN] Short pause {t:.2f}s before Inloggen click…")
    time.sleep(t)


def _browser_login_pause_after_inloggen_click() -> None:
    lo = float(os.getenv("BOL_BROWSER_LOGIN_AFTER_INLOGGEN_MIN_SEC", "0.06"))
    hi = float(os.getenv("BOL_BROWSER_LOGIN_AFTER_INLOGGEN_MAX_SEC", "0.22"))
    if hi < lo:
        lo, hi = hi, lo
    t = random.uniform(lo, hi)
    if t > 0.03:
        _lprint(f"[LOGIN] Pause {t:.2f}s after Inloggen — login form…")
    time.sleep(t)


def _click_storefront_inloggen(page) -> bool:
    """Guest header Inloggen — one click; Bol navigates to login (no programmatic reload loop)."""
    _dismiss_nl_storefront_privacy(page)
    time.sleep(
        random.uniform(
            float(os.getenv("BOL_BROWSER_PRE_INLOGGEN_CLICK_MIN_SEC", "0.04")),
            float(os.getenv("BOL_BROWSER_PRE_INLOGGEN_CLICK_MAX_SEC", "0.14")),
        )
    )
    role_link = re.compile(r"^\s*Inloggen\s*$", re.I)
    factories = (
        lambda: page.locator("header").get_by_role("link", name=role_link).first,
        lambda: page.get_by_role("link", name=role_link).first,
        lambda: page.locator(
            'header a[href*="account/login"], header a[href*="login"], header a[href*="wsp/login"]'
        ).filter(has_text=re.compile(r"Inloggen", re.I)).first,
        lambda: page.locator('a[href*="account/login"]').filter(has_text=re.compile(r"Inloggen", re.I)).first,
    )
    inlog_ms = max(2500, int(os.getenv("BOL_BROWSER_INLOGGEN_LOCATOR_TIMEOUT_MS", "8500")))
    for mk in factories:
        try:
            loc = mk()
            loc.wait_for(state="visible", timeout=inlog_ms)
            try:
                loc.scroll_into_view_if_needed(timeout=6000)
            except Exception:
                pass
            time.sleep(random.uniform(0.28, 0.72))
            loc.click(timeout=min(14000, inlog_ms + 4000))
            _lprint("[LOGIN] Clicked Inloggen (homepage).")
            return True
        except Exception:
            continue
    return False


def _browser_login_fill_submit_and_confirm(page, email: str, password: str) -> bool:
    """Shared tail: wait for fields, fill, submit, confirm — no wait_for_load_state."""
    if _is_logged_in(page):
        _lprint("[LOGIN] Already logged in.")
        return True

    _lprint("[LOGIN] Waiting for email/password fields (polling only — no navigation)…")
    form_ready = _wait_for_bol_login_form_ready(page)
    if form_ready:
        pause_after = float(os.getenv("BOL_BROWSER_LOGIN_AFTER_FORM_VISIBLE_SEC", "0.06"))
        time.sleep(max(0.0, pause_after))
        _lprint("[LOGIN] Form ready — typing email/password now.")
    else:
        _lprint(
            "[LOGIN] Fields not detected within timeout — trying fill anyway (slow proxy / layout).",
        )

    ok_fill = _try_fill_login_form(page, email, password)
    if not ok_fill:
        _lprint(f"[LOGIN] Could not find email/password fields. {_login_debug_line(page)}")
        wait_manual = float(os.getenv("BOL_BROWSER_MANUAL_LOGIN_SEC", "90"))
        _lprint(
            f"[LOGIN] Captcha / SSO / layout — complete login in the browser window; "
            f"waiting {wait_manual:.0f}s…",
        )
        time.sleep(max(15.0, wait_manual))
        ok_manual = _is_logged_in(page)
        if not ok_manual:
            time.sleep(2.0)
            ok_manual = _is_logged_in(page)
        if ok_manual:
            _lprint("[LOGIN] success (manual).")
        return ok_manual

    post_submit = float(os.getenv("BOL_BROWSER_LOGIN_POST_SUBMIT_SETTLE_SEC", "0.18"))
    time.sleep(max(0.0, post_submit))

    ok = _is_logged_in(page)
    if not ok:
        time.sleep(max(0.15, float(os.getenv("BOL_BROWSER_LOGIN_CONFIRM_RETRY_SEC_1", "0.35"))))
        ok = _is_logged_in(page)
    if not ok:
        time.sleep(max(0.15, float(os.getenv("BOL_BROWSER_LOGIN_CONFIRM_RETRY_SEC_2", "0.45"))))
        ok = _is_logged_in(page)
    if ok:
        _lprint("[LOGIN] success (form submit).")
    else:
        _lprint(
            f"[LOGIN] Could not confirm login — {_login_debug_line(page)} "
            "(check credentials / captcha).",
        )
    return ok


def _browser_login_via_homepage(
    page,
    *,
    email: str,
    password: str,
    cmp_already_cleared: bool = False,
) -> bool:
    """NL home → random pause → Inloggen → wait for fields → fill (default path)."""
    try:
        page.bring_to_front()
    except Exception:
        pass

    if _logged_in_for_homepage_flow(page):
        _lprint("[LOGIN] Already logged in on storefront.")
        return True

    if not _page_likely_nl_storefront_home(page):
        _lprint("[LOGIN] Opening NL homepage before Inloggen…")
        if not _goto_with_retries(page, STOREFRONT_NL_URL, label="NL homepage"):
            return False
        _ensure_nl_storefront_dom_ready(page)
        _micro_pause_after_nl_shell_ready()
    else:
        _ensure_nl_storefront_dom_ready(page)
        _micro_pause_after_nl_shell_ready()

    # Always run dismiss here: first pass in ``ensure_logged_in`` can race slow CMP/iframes;
    # skipping when ``cmp_already_cleared`` left overlays up and Inloggen looked \"stuck\".
    _dismiss_nl_storefront_privacy(page)

    _ensure_nl_storefront_shell_ready(page)
    _micro_pause_after_nl_shell_ready()

    _log_homepage_header_auth(page, phase="Homepage (before Inloggen pause)")

    if _logged_in_for_homepage_flow(page):
        _lprint("[LOGIN] Session active — Welkom / logged-in (not Inloggen). Skipping login flow.")
        return True

    _browser_login_pause_home_before_inloggen()

    _log_homepage_header_auth(page, phase="After random pause (about to find Inloggen)")

    if _logged_in_for_homepage_flow(page):
        _lprint("[LOGIN] Session recognized after pause — skipping Inloggen / WSP.")
        return True

    clicked = _click_storefront_inloggen(page)
    if not clicked:
        _dismiss_nl_storefront_privacy(page)
        _ensure_nl_storefront_dom_ready(page)
        _micro_pause_after_nl_shell_ready()
        clicked = _click_storefront_inloggen(page)
    # Primary login host is hosted WSP — not www .../account/login (often wrong/deprecated for this flow).
    fallback_url = "https://login.bol.com/wsp/login"
    use_fallback = os.getenv("BOL_BROWSER_LOGIN_INLOGGEN_FALLBACK_WSP", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )
    if not clicked:
        _log_homepage_header_auth(page, phase="Inloggen control not found — checking session before WSP")
        if _logged_in_for_homepage_flow(page) or _header_shows_welkom_account(page):
            _lprint(
                "[LOGIN] Welkom / logged-in state — Inloggen absent by design. "
                "Not opening WSP.",
            )
            return True
        if use_fallback:
            _lprint(
                "[LOGIN] Guest session — opening hosted WSP (login.bol.com/wsp/login) as fallback…",
            )
            if not _goto_with_retries(page, fallback_url, label="wsp/login (fallback)"):
                return False
            time.sleep(random.uniform(0.18, 0.45))
        else:
            _lprint(
                "[LOGIN] Could not click Inloggen — set BOL_BROWSER_LOGIN_INLOGGEN_FALLBACK_WSP=1 or click manually.",
            )
            return False
    else:
        _browser_login_pause_after_inloggen_click()

    return _browser_login_fill_submit_and_confirm(page, email, password)


def _browser_login_direct_hosted(
    page,
    *,
    email: str,
    password: str,
) -> bool:
    """Optional: goto hosted WSP directly (no homepage)."""
    primary_url = "https://login.bol.com/wsp/login"

    _lprint("[LOGIN] Direct mode — opening hosted WSP…")
    ok_nav = _goto_with_retries(page, primary_url, label="wsp/login")
    if not ok_nav:
        return False
    try:
        page.bring_to_front()
    except Exception:
        pass

    post_nav_settle = float(os.getenv("BOL_BROWSER_LOGIN_POST_NAV_SETTLE_SEC", "0.12"))
    if post_nav_settle > 0.05:
        _lprint(f"[LOGIN] Pause {post_nav_settle:.2f}s — login form…")
    time.sleep(max(0.0, post_nav_settle))

    return _browser_login_fill_submit_and_confirm(page, email, password)


def browser_login(
    page,
    *,
    email: str,
    password: str,
    cmp_already_cleared: bool = False,
) -> bool:
    home_first = os.getenv("BOL_BROWSER_LOGIN_HOME_FIRST", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )
    if home_first:
        return _browser_login_via_homepage(
            page,
            email=email,
            password=password,
            cmp_already_cleared=cmp_already_cleared,
        )
    return _browser_login_direct_hosted(page, email=email, password=password)


def ensure_logged_in(
    page,
    context,
    state_path: Path,
    email: str,
    password: str,
    *,
    fp_bundle: tuple[Path, str, str] | None = None,
) -> bool:
    """
    Always open NL homepage first (same flow as a real user). No cookie-wall automation here.
    If cookies already valid → reuse session; else homepage → Inloggen → fields → submit.
    """
    _lprint("[LOGIN] Opening NL homepage (navigating)…")
    if not _goto_with_retries(page, STOREFRONT_NL_URL, label="NL homepage"):
        _lprint("[LOGIN] Could not open bol.com homepage — check proxy / DNS / firewall.")
        return False
    _lprint("[LOGIN] Homepage response OK — CMP then session check (no header wait by default)…")
    try:
        page.bring_to_front()
    except Exception:
        pass

    _ensure_nl_storefront_dom_ready(page)
    _micro_pause_after_nl_shell_ready()
    _dismiss_nl_storefront_privacy(page)
    try:
        settle_ms = int(float(os.getenv("BOL_BROWSER_POST_CMP_SETTLE_SEC", "0")) * 1000)
        if settle_ms > 0:
            page.wait_for_timeout(settle_ms)
    except Exception:
        time.sleep(0.02)

    _ensure_nl_storefront_shell_ready(page)
    _micro_pause_after_nl_shell_ready()

    _lprint("[LOGIN] Quick guest vs Welkom check (short poll)…")
    auth = _wait_nl_home_auth_after_cmp(page)
    if _login_home_header_verbose():
        _log_homepage_header_auth(page, phase="NL homepage header (verbose)")

    if auth == "logged_in":
        _lprint("[LOGIN] Reused session (already logged in — Welkom / account or storefront cookie).")
        _persist_storage_state(context, state_path, fp_bundle)
        return True

    # Guest / unclear path — CMP sometimes paints after the short auth poll (hydrated session slot wins race).
    _dismiss_nl_storefront_privacy(page)

    if not email or not password:
        _lprint("[LOGIN] Not logged in and BOL_EMAIL / BOL_PASSWORD missing.")
        return False

    ok = browser_login(page, email=email, password=password, cmp_already_cleared=True)
    if ok:
        _persist_storage_state(context, state_path, fp_bundle)
    return ok


def _extract_ideal_from_url(url: str) -> str | None:
    u = url or ""
    if "pay.ideal.nl" in u or "ideal.nl" in u:
        return u.split("#")[0]
    return None


def _strip_url_query(u: str) -> str:
    return (u or "").split("#")[0].split("?")[0].rstrip("/")


def _page_on_product_url(page, product_url: str, mod) -> bool:
    """True if current tab matches this PDP (avoid pointless reloads during light polls)."""
    try:
        cur = _strip_url_query(page.url or "")
        want = _strip_url_query(product_url)
        if cur == want:
            return True
        pid = mod.extract_product_id(product_url)
        if pid and pid in (page.url or ""):
            return True
    except Exception:
        pass
    return False


def _settle_pdp_after_goto(page) -> None:
    """Bol PDPs rarely reach `networkidle` — wait for load + product shell so URL bar isn't ahead of DOM."""
    try:
        page.wait_for_load_state("load", timeout=22000)
    except Exception:
        pass
    try:
        page.locator("main, article, body").first.wait_for(state="visible", timeout=14000)
    except Exception:
        pass
    for sel in (
        "main h1",
        '[data-test*="product"]',
        "[itemtype*='Product']",
        'h1[data-test="title"]',
    ):
        try:
            page.locator(sel).first.wait_for(state="visible", timeout=12000)
            break
        except Exception:
            continue
    lo = float(os.getenv("BOL_BROWSER_PDP_POST_SETTLE_MIN_SEC", "0.08"))
    hi = float(os.getenv("BOL_BROWSER_PDP_POST_SETTLE_MAX_SEC", "0.22"))
    if hi < lo:
        lo, hi = hi, lo
    time.sleep(random.uniform(lo, hi))


def _apply_dom_buy_signals(page, out: dict) -> None:
    """
    Bol primary CTA is often labelled \"In winkelwagen\" (yellow) — same as add-to-cart.
    Do not rely on full-page HTML substrings for stock (related items carry false OOS text).
    """
    out.setdefault("has_op_voorraad", False)

    try:
        if page.get_by_text(re.compile(r"^\s*Op\s+voorraad\s*$", re.I)).first.is_visible(
            timeout=2000
        ):
            out["has_op_voorraad"] = True
    except Exception:
        pass
    if not out["has_op_voorraad"]:
        try:
            if page.get_by_text("Op voorraad", exact=False).first.is_visible(timeout=1200):
                tit = (page.title() or "").casefold()
                if "niet op voorraad" not in tit:
                    out["has_op_voorraad"] = True
        except Exception:
            pass

    buy_label = re.compile(
        r"(Toevoegen\s+aan\s+winkelwagen|In\s+winkelwagen)",
        re.I,
    )
    try:
        btn = page.get_by_role("button", name=buy_label)
        if btn.count() > 0 and btn.first.is_visible(timeout=3500):
            out["can_add"] = True
    except Exception:
        pass
    if not out["can_add"]:
        try:
            lk = page.get_by_role("link", name=buy_label)
            if lk.count() > 0 and lk.first.is_visible(timeout=1200):
                out["can_add"] = True
        except Exception:
            pass
    if not out["can_add"]:
        try:
            loc = page.locator(
                'button:visible, a[role="button"]:visible, [data-test*="add"]:visible'
            ).filter(has_text=re.compile(r"In\s+winkelwagen|Toevoegen\s+aan\s+winkelwagen", re.I))
            if loc.count() > 0 and loc.first.is_visible(timeout=2000):
                out["can_add"] = True
        except Exception:
            pass


def _explicit_product_oos_from_html(lc: str, *, has_positive_buy_signal: bool) -> bool:
    """HTML substring OOS is unreliable (carousel/related items); DOM positives override."""
    if has_positive_buy_signal:
        return False
    return bool(re.search(r"niet\s+op\s+voorraad", lc))


def _read_product_title_from_pdp(page) -> str | None:
    """Best-effort product name from PDP DOM / document title (for console logs)."""
    for sel in (
        "main h1",
        '[data-test="title"]',
        "article h1",
        "h1",
    ):
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=1800):
                t = (loc.inner_text() or "").strip()
                t = re.sub(r"\s+", " ", t)
                if len(t) > 2:
                    return t[:420]
        except Exception:
            continue
    try:
        tit = (page.title() or "").strip()
        if " | " in tit:
            tit = tit.split(" | ", 1)[0].strip()
        low = tit.casefold()
        for suffix in (" - bol.com", " – bol.com", " | bol.com"):
            idx = low.find(suffix)
            if idx != -1:
                tit = tit[:idx].strip()
                break
        if len(tit) > 2:
            return tit[:420]
    except Exception:
        pass
    return None


def _extract_price_text_from_pdp_html(html: str) -> str | None:
    """
    Shelf price from embedded JSON — chunk search avoids brittle single-line regex when braces nest.
    """
    if not html:
        return None
    idx = html.find('"sellingPrice"')
    if idx != -1:
        chunk = html[idx : idx + 20000]
        m = re.search(r'"amount"\s*:\s*"([0-9]+[.,][0-9]{2})"', chunk)
        if m:
            return m.group(1).replace(".", ",")
        m = re.search(r'"amount"\s*:\s*([0-9]+[.,][0-9]{2})\s*[,}\]\s]', chunk)
        if m:
            return m.group(1).replace(".", ",")
    for needle in ('"currentPrice"', '"listPrice"', '"offer"', '"retailProduct"'):
        ix = html.find(needle)
        if ix == -1:
            continue
        chunk = html[ix : ix + 12000]
        m = re.search(r'"amount"\s*:\s*"([0-9]+[.,][0-9]{2})"', chunk)
        if m:
            return m.group(1).replace(".", ",")
        m = re.search(r'"amount"\s*:\s*([0-9]+[.,][0-9]{2})\s*[,}\]\s]', chunk)
        if m:
            return m.group(1).replace(".", ",")
    pm = re.search(
        r'"sellingPrice"\s*:\s*\{[^}]*"amount"\s*:\s*"?([0-9]+[.,][0-9]{2})"?',
        html,
    )
    if pm:
        return pm.group(1).replace(".", ",")
    pe = re.search(
        r'(?:content|aria-label)="[^"]*€\s*([0-9]+[.,][0-9]{2})',
        html[:500000],
        re.I,
    )
    if pe:
        return pe.group(1).replace(".", ",")
    for sm in re.finditer(
        r'<script[^>]*type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html[:900000],
        re.I | re.DOTALL,
    ):
        chunk = (sm.group(1) or "")[:120000]
        for rx in (
            r'"price"\s*:\s*"([0-9]+[.,][0-9]{2})"',
            r'"price"\s*:\s*([0-9]+[.,][0-9]{2})\b',
            r'"highPrice"\s*:\s*"([0-9]+[.,][0-9]{2})"',
        ):
            mm = re.search(rx, chunk, re.I)
            if mm:
                return mm.group(1).replace(".", ",")
    return None


def _read_shelf_price_from_pdp_dom(page) -> str | None:
    """
    Visible PDP price (large EUR line next to buy button). Playwright inner_text merges split spans (10 + ,95).
    Skips reference / \"most displayed\" lines when possible.
    """
    skip_sub = (
        "meest getoonde",
        "first 30",
        "adv ",
        "bespaar",
        "save ",
        "gratis verzend",
        "sold by",
        "rating",
    )

    def line_ok(line: str) -> bool:
        lc = line.casefold().strip()
        if len(lc) < 4:
            return False
        if any(s in lc for s in skip_sub):
            return False
        if lc.endswith("%") and "," not in lc:
            return False
        return True

    try:
        for sel in ('meta[itemprop="price"]', 'meta[property="product:price:amount"]'):
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            raw = (loc.get_attribute("content") or "").strip()
            if not raw:
                continue
            raw_clean = raw.replace(" ", "").replace(",", ".")
            m = re.match(r"^([0-9]+\.[0-9]{2})$", raw_clean)
            if m:
                parts = raw_clean.split(".")
                return f"{parts[0]},{parts[1]}"
            m = re.match(r"^([0-9]+,[0-9]{2})$", raw.replace(" ", ""))
            if m:
                return m.group(1)
    except Exception:
        pass

    try:
        for sel in (
            'span[itemprop="price"]',
            'p[itemprop="price"]',
            '[data-test*="sales-price"]',
            '[data-test*="selling-price"]',
        ):
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            try:
                c = loc.get_attribute("content")
                if c and c.strip():
                    mm = re.search(r"([0-9]{1,5}[.,][0-9]{2})", c.strip())
                    if mm:
                        return mm.group(1).replace(".", ",")
            except Exception:
                pass
            try:
                if loc.is_visible(timeout=600):
                    tx = re.sub(r"\s+", " ", (loc.inner_text() or "").strip())
                    mm = re.search(r"(?:€\s*)?([0-9]{1,5}[.,][0-9]{2})", tx)
                    if mm:
                        return mm.group(1).replace(".", ",")
            except Exception:
                continue
    except Exception:
        pass

    for root in (
        '[data-test*="buy-block"]',
        '[data-test*="BuyBlock"]',
        '[class*="BuyBlock"]',
        '[class*="buy-block"]',
        '[data-test="buy-block-slot"]',
    ):
        try:
            block = page.locator(root).first
            if block.count() == 0:
                continue
            if not block.is_visible(timeout=700):
                continue
            txt = block.inner_text() or ""
            for line in txt.split("\n"):
                line = line.strip()
                if not line_ok(line):
                    continue
                m = re.search(r"(?:€\s*)?([0-9]{1,4}[.,][0-9]{2})\s*$", line) or re.search(
                    r"(?:€\s*)([0-9]{1,4}[.,][0-9]{2})",
                    line,
                )
                if m:
                    return m.group(1).replace(".", ",")
        except Exception:
            continue

    try:
        main = page.locator("main").first
        if main.count() and main.is_visible(timeout=500):
            lines = (main.inner_text() or "").split("\n")
            for line in lines[:120]:
                line = line.strip()
                if not line_ok(line):
                    continue
                m = re.search(r"(?:€\s*)?([0-9]{1,4}[.,][0-9]{2})\s*$", line) or re.search(
                    r"^€\s*([0-9]{1,4}[.,][0-9]{2})\s*$",
                    line,
                )
                if m:
                    return m.group(1).replace(".", ",")
    except Exception:
        pass

    return None


def _evaluate_product_snapshot(
    page,
    product_url: str,
    mod,
    *,
    navigate: bool,
) -> dict:
    """
    PDP snapshot for **any** parallel Chromium slot (same logic for all).
    When navigate=False, reuse current DOM (fast poll) — caller must stay on URL.
    Avoid networkidle on PDP (was causing endless 'loading' feeling).
    Checkout only proceeds when **can_add** (visible add-to-cart control); no separate \"already in cart\" path.
    """
    pid = mod.extract_product_id(product_url) or ""
    out: dict = {
        "product_url": product_url,
        "product_id": pid,
        "offer_uid": None,
        "can_add": False,
        "oos": False,
        "price_text": None,
        "offline": False,
        "http_status": 0,
        "navigated": False,
        "has_op_voorraad": False,
        "product_title": None,
    }

    if navigate:
        try:
            response = page.goto(product_url, wait_until="domcontentloaded", timeout=90000)
            out["navigated"] = True
            status = int(response.status) if response is not None else 0
            out["http_status"] = status
            if status == 404:
                out["offline"] = True
        except Exception as e:
            out["offline"] = True
            out["load_error"] = str(e)[:240]
            return out
        _dismiss_cookie_wall(page)
        _settle_pdp_after_goto(page)
    else:
        if not _page_on_product_url(page, product_url, mod):
            try:
                response = page.goto(product_url, wait_until="domcontentloaded", timeout=90000)
                out["navigated"] = True
                status = int(response.status) if response is not None else 0
                out["http_status"] = status
                if status == 404:
                    out["offline"] = True
            except Exception as e:
                out["offline"] = True
                out["load_error"] = str(e)[:240]
                return out
            _dismiss_cookie_wall(page)
            _settle_pdp_after_goto(page)

    html = page.content()
    tit_low = _html_title_casefold(html)

    pu_raw = (page.url or "").replace("\\", "/").lower()
    if "/nl/nl/p/" in pu_raw and _html_looks_like_bol_oops_not_found(html):
        out["offline"] = True

    if any(
        x in tit_low
        for x in ("niet gevonden", "404", "pagina niet", "page not found", "not found")
    ):
        out["offline"] = True
    if re.search(
        r"product\s+niet\s+gevonden|deze\s+pagina\s+bestaat\s+niet",
        html[:80000],
        re.I,
    ):
        out["offline"] = True

    out["offer_uid"] = mod._extract_offer_uid_from_pdp_html(html)

    lc = html.casefold()
    _apply_dom_buy_signals(page, out)
    has_pos = bool(out["can_add"] or out.get("has_op_voorraad"))
    out["oos"] = _explicit_product_oos_from_html(lc, has_positive_buy_signal=has_pos)

    out["price_text"] = _extract_price_text_from_pdp_html(html)
    if not out["price_text"]:
        try:
            out["price_text"] = _read_shelf_price_from_pdp_dom(page)
        except Exception:
            out["price_text"] = None

    # Strong signals → never treat as offline (title/HTML edge cases / SPA timing).
    if (
        out["can_add"]
        or out.get("offer_uid")
        or out.get("has_op_voorraad")
    ):
        out["offline"] = False
    if "buyblockslot" in lc or "retailproduct" in lc.replace(" ", ""):
        out["offline"] = False

    try:
        out["product_title"] = _read_product_title_from_pdp(page)
    except Exception:
        out["product_title"] = None

    # Must run last: overrides thin PDP / buy-block heuristics — block/shell page is not shoppable.
    if _html_looks_like_bol_access_blocked(html):
        out["offline"] = True
        out["offline_reason"] = "bol_access_blocked"
        # Log line is neutral in _print_monitor_offline_log (URL may be pre-drop / not live yet).

    return out


def _print_monitor_offline_log(
    sp: str, snap: dict, *, url_hint: str | None = None
) -> None:
    """One line per offline PDP — restricted/shell uses neutral wording (not “IP blocked”)."""
    http_s = int(snap.get("http_status") or 0)
    err = (snap.get("load_error") or "").strip()
    reason = snap.get("offline_reason")
    short_u = ""
    if url_hint:
        u = url_hint.strip()
        short_u = f"{u[:72]}…" if len(u) > 72 else u
    url_part = f" URL: {short_u}" if short_u else ""
    if reason == "bol_access_blocked":
        print(
            f"{sp}[MONITOR] OFFLINE — No full product page yet (not shoppable; may go live later). "
            f"HTTP {http_s}.{url_part} Next check ~40–60s; staying on flow.",
            flush=True,
        )
        return
    extra = f" ({err})" if err else ""
    print(
        f"{sp}[MONITOR] offline / missing PDP (HTTP {http_s}){extra}{url_part} — "
        f"retry in ~40–60s (stay on flow, no home redirect).",
        flush=True,
    )


def _immediate_pdp_offline_probe(page, product_url: str, mod, sp: str) -> None:
    """
    After login, before post-login sleep: each Chromium worker calls this for **its own** CSV row
    (slot 0 → first URL, slot 1 → second, …) — not slot-1-only. Uses `_evaluate_product_snapshot`
    with navigate=False, so **every** offline rule applies (404 / oops shell / title / IP-geblokkeerd /
    thin PDP overrides, etc.), same as `_slot_inner_iteration` monitoring.
    """
    try:
        response = page.goto(
            product_url, wait_until="domcontentloaded", timeout=90000
        )
        goto_status = int(response.status) if response is not None else 0
    except Exception as e:
        print(
            f"{sp}[MONITOR] OFFLINE — first PDP load failed: {str(e)[:220]} "
            f"— {product_url[:72]}…",
            flush=True,
        )
        return
    _dismiss_cookie_wall(page)
    _settle_pdp_after_goto(page)
    snap = _evaluate_product_snapshot(page, product_url, mod, navigate=False)
    if int(snap.get("http_status") or 0) == 0 and goto_status:
        snap["http_status"] = goto_status
    if snap.get("offline"):
        _print_monitor_offline_log(sp, snap, url_hint=product_url)


def _human_like_click(page, locator, *, timeout_ms: int = 12000) -> bool:
    """
    Scroll into view → move mouse to center (stepped path in headed mode) → click.
    Falls back to direct locator.click() if anything fails (legacy behaviour).
    """
    if os.getenv("BOL_BROWSER_INSTANT_CLICK", "").strip() in ("1", "true", "yes"):
        try:
            locator.click(timeout=timeout_ms)
            return True
        except Exception:
            return False
    try:
        locator.scroll_into_view_if_needed(timeout=timeout_ms)
        time.sleep(random.uniform(0.08, 0.22))
        box = locator.bounding_box()
        if box:
            x = box["x"] + box["width"] / 2
            y = box["y"] + box["height"] / 2
            steps = max(6, int(random.uniform(12, 26)))
            page.mouse.move(x, y, steps=steps)
            time.sleep(random.uniform(0.18, 0.45))
            page.mouse.click(x, y)
            return True
        locator.hover(timeout=timeout_ms)
        time.sleep(random.uniform(0.15, 0.4))
        locator.click(timeout=timeout_ms)
        return True
    except Exception:
        try:
            locator.click(timeout=timeout_ms)
            return True
        except Exception:
            return False


def _pdp_primary_add_button_visible(page) -> bool:
    """Yellow line-add CTA still showing — cart line not applied in UI yet."""
    buy_label = re.compile(
        r"(Toevoegen\s+aan\s+winkelwagen|In\s+winkelwagen)",
        re.I,
    )
    try:
        if page.get_by_role("button", name=buy_label).first.is_visible(timeout=500):
            return True
    except Exception:
        pass
    try:
        if page.locator("button:visible").filter(
            has_text=re.compile(r"In\s+winkelwagen|Toevoegen\s+aan\s+winkelwagen", re.I)
        ).first.is_visible(timeout=400):
            return True
    except Exception:
        pass
    return False


def _pdp_post_atc_overlay_visible(page) -> bool:
    """
    Post-click Bol UI: sheet / dialog with basket CTAs (overlay path checkout relies on).
    Detects cases where yellow button flickers but nothing was actually added.
    """
    modal_pat = re.compile(
        r"winkelwagen|toegevoegd|verder\s+naar\s+bestellen|naar\s+de\s+kassa",
        re.I,
    )
    try:
        dlg = page.locator('[role="dialog"]').filter(has_text=modal_pat).first
        if dlg.is_visible(timeout=800):
            return True
    except Exception:
        pass
    cta = re.compile(r"^\s*(Naar\s+de\s+kassa|Verder\s+naar\s+bestellen)\s*$", re.I)
    try:
        if page.get_by_role("button", name=cta).first.is_visible(timeout=500):
            return True
    except Exception:
        pass
    try:
        if page.get_by_role("link", name=cta).first.is_visible(timeout=450):
            return True
    except Exception:
        pass
    return False


def _pdp_add_to_cart_confirmed(page) -> bool:
    """
    True once Bol reflects the add: post-ATC overlay / dialog **or** primary add CTA no longer buy-mode.
    """
    if _pdp_post_atc_overlay_visible(page):
        return True
    try:
        dlg = page.locator('[role="dialog"]').filter(
            has_text=re.compile(r"winkelwagen|toegevoegd", re.I)
        ).first
        if dlg.is_visible(timeout=750):
            return True
    except Exception:
        pass
    return not _pdp_primary_add_button_visible(page)


def _ensure_atc_confirmed_before_checkout(
    page,
    *,
    sp: str,
    product_url: str,
    mod,
    idle_sec: float,
) -> bool:
    """
    Stay on the **same PDP**: after initial ATC loop, re-settle and re-click until overlay confirms
    or passes exhausted — never open basket/checkout if cart never stuck.
    """
    passes = max(0, int(os.getenv("BOL_ATC_SAME_PDP_CONFIRM_PASSES", "4")))
    idle2 = max(0.35, float(os.getenv("BOL_ATC_REVERIFY_IDLE_SEC", "0.72")))
    ok = False
    for i in range(passes + 1):
        eff_idle = idle_sec if i == 0 else idle2
        _page_smooth_idle_then_dom_idle(page, eff_idle)
        if _pdp_add_to_cart_confirmed(page):
            ok = True
            break
        if i < passes:
            print(
                f"{sp}[MONITOR] Cart/overlay not confirmed — same-PDP add-to-cart retry "
                f"({i + 1}/{passes})…",
                flush=True,
            )
            try:
                _dismiss_cookie_wall(page)
            except Exception:
                pass
            _perform_primary_buy_click(page)
            time.sleep(random.uniform(0.28, 0.62))
    if ok:
        return True
    print(
        f"{sp}[MONITOR] Add-to-cart still unconfirmed on PDP — skipping checkout this cycle "
        f"(will keep monitoring same URL).",
        flush=True,
    )
    if not _page_on_product_url(page, product_url, mod):
        try:
            tm = min(75000, int(os.getenv("BOL_BROWSER_PDP_REANCHOR_TIMEOUT_MS", "75000")))
            page.goto(product_url, wait_until="domcontentloaded", timeout=tm)
            _dismiss_cookie_wall(page)
            _settle_pdp_after_goto(page)
        except Exception:
            pass
    return False


def _perform_primary_buy_click(page) -> bool:
    """Dispatch one click on the main add-to-cart control (no post-click settle)."""
    buy_label = re.compile(
        r"(Toevoegen\s+aan\s+winkelwagen|In\s+winkelwagen)",
        re.I,
    )
    try:
        btn = page.get_by_role("button", name=buy_label).first
        if _human_like_click(page, btn):
            return True
    except Exception:
        pass
    try:
        loc = page.locator("button:visible").filter(
            has_text=re.compile(r"In\s+winkelwagen|Toevoegen\s+aan\s+winkelwagen", re.I)
        ).first
        if _human_like_click(page, loc):
            return True
    except Exception:
        pass
    return False


def _click_add_to_cart(page, *, sp: str) -> bool:
    """
    Click add-to-cart until the PDP reflects success (overlay or buy strip leaves add mode).
    Retries with random ms delays if the UI did not update.
    """
    max_attempts = max(3, int(os.getenv("BOL_ATC_CONFIRM_MAX_ATTEMPTS", "12")))
    r_lo = float(os.getenv("BOL_ATC_RETRY_DELAY_MIN_MS", "400")) / 1000.0
    r_hi = float(os.getenv("BOL_ATC_RETRY_DELAY_MAX_MS", "1600")) / 1000.0
    if r_hi < r_lo:
        r_lo, r_hi = r_hi, r_lo
    v_lo = float(os.getenv("BOL_ATC_POST_CLICK_VERIFY_MIN_SEC", "0.22"))
    v_hi = float(os.getenv("BOL_ATC_POST_CLICK_VERIFY_MAX_SEC", "0.62"))
    if v_hi < v_lo:
        v_lo, v_hi = v_hi, v_lo

    settle_lo = float(os.getenv("BOL_CHECKOUT_POST_ATC_CLICK_SETTLE_MIN", "0.32"))
    settle_hi = float(os.getenv("BOL_CHECKOUT_POST_ATC_CLICK_SETTLE_MAX", "0.68"))
    if settle_hi < settle_lo:
        settle_lo, settle_hi = settle_hi, settle_lo

    for attempt in range(1, max_attempts + 1):
        if _pdp_add_to_cart_confirmed(page):
            time.sleep(random.uniform(settle_lo, settle_hi))
            print(
                f"{sp}[MONITOR] Add-to-cart already reflected before retry click "
                f"(attempt {attempt}/{max_attempts}) — proceeding.",
                flush=True,
            )
            return True
        ok = _perform_primary_buy_click(page)
        if not ok:
            print(
                f"{sp}[MONITOR] Add-to-cart click dispatch failed (attempt {attempt}/{max_attempts}).",
                flush=True,
            )
        time.sleep(random.uniform(v_lo, v_hi))
        if _pdp_add_to_cart_confirmed(page):
            time.sleep(random.uniform(settle_lo, settle_hi))
            print(
                f"{sp}[MONITOR] Add-to-cart confirmed after click (attempt {attempt}/{max_attempts}, hover → click).",
                flush=True,
            )
            return True
        print(
            f"{sp}[MONITOR] Add-to-cart not reflected yet — retrying after random delay "
            f"(attempt {attempt}/{max_attempts})…",
            flush=True,
        )
        time.sleep(random.uniform(r_lo, r_hi))

    print(
        f"{sp}[MONITOR] Add-to-cart did not confirm after {max_attempts} attempt(s).",
        flush=True,
    )
    return False


def _checkout_verbose_cta() -> bool:
    return os.getenv("BOL_CHECKOUT_VERBOSE_CTA", "").strip().lower() in ("1", "true", "yes")


def _checkout_pause(
    min_env: str,
    max_env: str,
    default_min: float,
    default_max: float,
) -> None:
    """Random sleep — env overrides per deploy; defaults tuned for human-ish checkout pacing."""
    try:
        lo = float(os.getenv(min_env, str(default_min)))
        hi = float(os.getenv(max_env, str(default_max)))
    except ValueError:
        lo, hi = default_min, default_max
    if hi < lo:
        lo, hi = hi, lo
    time.sleep(random.uniform(lo, hi))


def _page_smooth_idle_then_dom_idle(page, idle_sec: float) -> None:
    """
    After a pause, avoid OS sleep → immediate Playwright action jerk: use page timing + DOM settle.
    """
    ms = int(max(0.0, float(idle_sec)) * 1000)
    if ms > 0:
        page.wait_for_timeout(ms)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=12000)
    except Exception:
        pass


def _checkout_log_step_page(page, *, step: int | None = None, phase: str = "") -> None:
    """Optional per-step URL/title — off by default (noisy; title() can throw during SPA nav)."""
    if os.getenv("BOL_CHECKOUT_VERBOSE_STEP_LOG", "").strip().lower() not in (
        "1",
        "true",
        "yes",
    ):
        return
    label = f"step {step}" if step is not None else (phase or "phase")
    try:
        u = _page_url_safe(page)
        ti = ""
        try:
            ti = ((page.title() or "").strip())[:110]
        except Exception:
            ti = "(title unavailable)"
        _cprint(f"[CHECKOUT] ({label}) url={u[:178]}", flush=True)
        _cprint(f"[CHECKOUT] ({label}) title≈{ti!r}", flush=True)
    except Exception as e:
        _cprint(f"[CHECKOUT] ({label}) snapshot failed: {e}", flush=True)


def _url_is_winkelwagen_basket(url: str) -> bool:
    """
    True on cart / basket page — after login, Bol sometimes sends you back here.
    Then only \"Verder naar bestellen\" makes sense first (not repeated Bestellen).
    """
    raw = (url or "").strip().lower()
    if not raw or "rnwy" in raw:
        return False
    try:
        path = (urlparse(raw).path or "").lower()
    except Exception:
        path = raw
    return (
        "basket" in path
        or "winkelwagen" in path
        or path.rstrip("/").endswith("basket.html")
    )


def _checkout_labels_for_url(url: str) -> tuple[str, ...]:
    """
    Order matters — **must** use anchored name matching in _checkout_click_dutch_cta so
    \"Bestellen\" never matches \"Verder naar bestellen\" (substring bug).
    Basket: only proceed-to-checkout CTAs. Runway: navigation → payment → confirm last.
    """
    if _url_is_winkelwagen_basket(url):
        return (
            "Verder naar bestellen",
            "Ik ga bestellen",
            "Naar de kassa",
        )
    ul = (url or "").lower()
    # Final summary blue CTA (see screenshot): \"Bestellen en betalen\" — exact phrase, not substring of Verder…
    _pay_confirm = (
        "Bestellen en betalen",
        "iDEAL",
        "Betalen",
        "Betaal",
        "Bevestigen",
        "Verder naar bestellen",
        "Naar de kassa",
        "Afrekenen",
        "Doorgaan",
        "Volgende",
        "Ik ga bestellen",
        "Bestellen",
    )
    # Near payment / ideal selection — same order; Bestellen en betalen first when visible (skips fast if absent).
    if "rnwy" in ul or "checkout" in ul:
        if any(x in ul for x in ("payment", "betaal", "betalen", "ideal", "afreken")):
            return _pay_confirm
    if "rnwy" in ul or "/nl/nl/checkout" in ul:
        return _pay_confirm
    return (
        "Verder naar bestellen",
        "Ik ga bestellen",
        "Naar de kassa",
        "Afrekenen",
        "Doorgaan",
        "Volgende",
        "Bevestigen",
        "Bestellen en betalen",
        "iDEAL",
        "Betalen",
        "Betaal",
        "Bestellen",
    )


def _checkout_url_looks_like_runway(cur: str) -> bool:
    """
    True when we are on bol checkout steps — never True on WSP/login URLs even if returnUrl=…checkout…
    in the query (that false positive broke post-login CTA logic on slow redirects, often noticed on S3).
    """
    if not cur or _url_suggests_bol_login(cur):
        return False
    cul = cur.casefold()
    if "rnwy" in cul:
        return True
    try:
        pu = urlparse(cur)
        path = ((pu.path or "") + "?" + (pu.query or "")).casefold()
    except Exception:
        path = cul
    return (
        "/nl/nl/checkout" in path
        or "/checkout/" in path
        or path.rstrip("/").endswith("/checkout")
        or "checkout?" in path
    )


def _checkout_log_land_after_login(page) -> None:
    """One line: URL + title + whether we are on basket vs checkout (after login redirect)."""
    try:
        u = page.url or ""
    except Exception:
        u = ""
    try:
        ti = (page.title() or "").strip()
        if len(ti) > 110:
            ti = ti[:110] + "…"
    except Exception:
        ti = "?"
    if _url_suggests_bol_login(u):
        phase = "still on login/WSP — wait or retry (redirect not onto storefront yet)"
    elif _url_is_winkelwagen_basket(u):
        phase = "basket — next step is Verder naar bestellen (not Bestellen spam)"
    elif _checkout_url_looks_like_runway(u):
        phase = "checkout runway — continue toward payment"
    elif _extract_ideal_from_url(u):
        phase = "payment gateway"
    else:
        phase = "storefront / other bol page — may need Verder naar bestellen / basket"
    _cprint(
        f"[CHECKOUT] Post-login landing: {phase}\n"
        f"[CHECKOUT]   url={u[:180]}\n"
        f"[CHECKOUT]   title={ti!r}",
        flush=True,
    )


def _checkout_click_dutch_cta(page, label: str, *, verbose: bool | None = None) -> bool:
    """Hover → click for checkout buttons (same human pattern as add-to-cart)."""
    if verbose is None:
        verbose = _checkout_verbose_cta()
    # Anchor full label — \"Bestellen\" must NOT match \"Verder naar bestellen\" (substring).
    pat = re.compile(rf"^\s*{re.escape(label)}\s*$", re.I)
    candidates = (
        page.get_by_role("button", name=pat),
        page.get_by_role("link", name=pat),
        page.locator("button:visible, a:visible").filter(has_text=pat),
    )
    for loc in candidates:
        try:
            if loc.count() == 0:
                continue
            el = loc.first
            if el.is_visible(timeout=3500) and _human_like_click(page, el):
                if verbose:
                    _cprint(f"[CHECKOUT] CTA: {label!r}", flush=True)
                return True
        except Exception:
            continue
    # NL final summary — blue \"Bestellen en betalen\" (chevron icon; whitespace-tolerant).
    if label.strip().casefold() == "bestellen en betalen":
        flex = re.compile(r"^\s*Bestellen\s+en\s+betalen\s*$", re.I)
        try:
            for role in ("button", "link"):
                loc = page.get_by_role(role, name=flex).first
                if loc.is_visible(timeout=2200) and _human_like_click(page, loc):
                    if verbose:
                        _cprint("[CHECKOUT] CTA: Bestellen en betalen (flex match)", flush=True)
                    return True
        except Exception:
            pass
        try:
            alt = page.locator("button:visible, a:visible").filter(has_text=flex).first
            if alt.is_visible(timeout=2200) and _human_like_click(page, alt):
                if verbose:
                    _cprint("[CHECKOUT] CTA: Bestellen en betalen (locator flex)", flush=True)
                return True
        except Exception:
            pass
    return False


def _url_suggests_bol_login(url: str) -> bool:
    """Checkout can redirect to login without invalidating cookies — re-auth in-page."""
    u = (url or "").lower()
    if "login.bol.com" in u:
        return True
    if "/wsp/login" in u:
        return True
    if "account/login" in u:
        return True
    if "bol.com" in u and "/inloggen" in u:
        return True
    if "identity.bol.com" in u:
        return True
    return False


def _checkout_post_submit_success(initial_url: str, current_url: str) -> bool:
    """
    Detect successful login redirect without calling page.content().
    Typical chain: login.bol.com/wsp/login → www.bol.com/.../rnwy/checkout?ref=...
    Uses host + path (not raw substring) so query strings cannot confuse detection.
    """
    raw = (current_url or "").strip()
    if _extract_ideal_from_url(raw):
        return True
    if not raw:
        return False

    try:
        pu = urlparse(raw)
        host = (pu.netloc or "").lower()
        path = (pu.path or "").lower()
    except Exception:
        host, path = "", raw.lower()

    ul = raw.lower()

    # Still on hosted login or explicit login path
    if host == "login.bol.com" or host.endswith(".login.bol.com"):
        return False
    if "/wsp/login" in path:
        return False
    if "/account/login" in path:
        return False
    if "/nl/inloggen" in path and "checkout" not in path and "rnwy" not in path:
        return False

    # Storefront — checkout / basket / order runway (normal after WSP login)
    if host.endswith("bol.com") and host != "login.bol.com":
        if any(
            x in path
            for x in (
                "/rnwy/",
                "rnwy",
                "/nl/nl/checkout",
                "/checkout/",
                "/checkout",
                "/nl/nl/order/",
                "/order/basket",
                "basket.html",
                "winkelwagen",
            )
        ):
            return True
        # Path ends with .../checkout (segment)
        if path.rstrip("/").endswith("/checkout"):
            return True

    iu = (initial_url or "").lower()
    # Redirected off login.bol.com onto any non-login bol storefront page = progress
    if ("login.bol.com" in iu or "/wsp/login" in iu) and host.endswith("bol.com"):
        if host != "login.bol.com" and "/account/login" not in path:
            return True

    if "rnwy/checkout" in ul or "/nl/rnwy/" in ul:
        return True

    if "account/login" in iu and "/account/login" not in path:
        return True

    return not _url_suggests_bol_login(raw)


def _page_url_safe(page, *, attempts: int = 8, pause: float = 0.12) -> str:
    """While navigating, page.url can briefly throw or be stale — retry."""
    last = ""
    for _ in range(max(1, attempts)):
        try:
            last = page.url or ""
            if last:
                return last
        except Exception:
            pass
        time.sleep(pause)
    return last


def _checkout_wait_for_post_login_redirect(page, initial_url: str) -> bool:
    """
    After Inloggen click: Bol redirects to checkout within a few seconds — poll URL quietly.
    No negative logs during this window (never claim 'stuck' while redirect may be in flight).
    """
    settle_sec = float(os.getenv("BOL_CHECKOUT_LOGIN_SETTLE_SEC", "0.42"))
    max_poll = float(os.getenv("BOL_CHECKOUT_LOGIN_MAX_REDIRECT_POLL_SEC", "22"))
    try:
        page.wait_for_load_state("domcontentloaded", timeout=28000)
    except Exception:
        pass
    try:
        page.wait_for_load_state("load", timeout=24000)
    except Exception:
        pass
    time.sleep(max(0.0, settle_sec))

    t0 = time.time()
    deadline = t0 + max_poll
    poll_interval = float(os.getenv("BOL_CHECKOUT_LOGIN_REDIRECT_POLL_INTERVAL_SEC", "0.22"))
    while time.time() < deadline:
        cur = _page_url_safe(page, attempts=5, pause=0.06)
        if _extract_ideal_from_url(cur):
            return True
        if _checkout_post_submit_success(initial_url, cur):
            return True
        time.sleep(poll_interval)

    cur = _page_url_safe(page, attempts=8, pause=0.08)
    return bool(_extract_ideal_from_url(cur) or _checkout_post_submit_success(initial_url, cur))


def _silent_extend_if_still_login_host(
    page,
    initial_url: str,
    *,
    extra_sec: float = 12.0,
) -> bool:
    """Last-chance poll without scary logs — only used when redirect flag still ambiguous."""
    t_end = time.time() + max(4.0, extra_sec)
    while time.time() < t_end:
        cur = _page_url_safe(page, attempts=4, pause=0.08)
        if _extract_ideal_from_url(cur):
            return True
        if _checkout_post_submit_success(initial_url, cur):
            return True
        time.sleep(0.45)
    cur = _page_url_safe(page)
    return bool(_extract_ideal_from_url(cur) or _checkout_post_submit_success(initial_url, cur))


def _checkout_handle_login_if_needed(
    page,
    email: str,
    password: str,
    context,
    state_path: Path | None,
    fp_bundle: tuple[Path, str, str] | None = None,
) -> bool:
    """
    If Bol sends you to Inloggen mid-checkout, fill Email/Password from .env and continue.
    Does not navigate away first — preserves return URL when possible.
    """
    if not _url_suggests_bol_login(page.url):
        return True
    if not email or not password:
        _cprint(
            "[CHECKOUT] Login page but BOL_EMAIL/BOL_PASSWORD (or Email/Password) missing in .env — "
            "waiting for manual login…",
            flush=True,
        )
        wait_manual = float(os.getenv("BOL_BROWSER_CHECKOUT_LOGIN_WAIT_SEC", "120"))
        t0 = time.time()
        while time.time() - t0 < max(25.0, wait_manual):
            time.sleep(2.0)
            if _extract_ideal_from_url(page.url):
                return True
            if not _url_suggests_bol_login(page.url):
                break
            try:
                page.wait_for_load_state("load", timeout=6000)
            except Exception:
                pass
        ok = (not _url_suggests_bol_login(page.url)) or bool(_extract_ideal_from_url(page.url))
        if ok and context and state_path:
            _persist_storage_state(context, state_path, fp_bundle)
        return ok

    try:
        initial_login_url = page.url or ""
    except Exception:
        initial_login_url = ""
    _cprint(
        f"[CHECKOUT] Login screen detected — auto-fill (session stays in browser). "
        f"url={initial_login_url!r}",
        flush=True,
    )
    time.sleep(random.uniform(0.06, 0.18))
    try:
        page.bring_to_front()
    except Exception:
        pass
    pause_fill = float(os.getenv("BOL_CHECKOUT_LOGIN_PAUSE_BEFORE_FILL_SEC", "0.12"))
    time.sleep(max(0.0, pause_fill))
    pause_submit = float(os.getenv("BOL_CHECKOUT_LOGIN_PAUSE_BEFORE_SUBMIT_SEC", "0.75"))
    filled = _try_fill_login_form(
        page,
        email,
        password,
        pause_before_submit_sec=pause_submit,
    )
    if not filled:
        _cprint(
            f"[CHECKOUT] Could not auto-locate login fields. {_login_debug_line(page)}",
            flush=True,
        )
        wait_manual = float(os.getenv("BOL_BROWSER_CHECKOUT_LOGIN_WAIT_SEC", "120"))
        _cprint(
            f"[CHECKOUT] Complete login in the browser if prompted; waiting up to {wait_manual:.0f}s…",
            flush=True,
        )
        t0 = time.time()
        while time.time() - t0 < max(25.0, wait_manual):
            time.sleep(2.0)
            if _extract_ideal_from_url(page.url):
                return True
            if not _url_suggests_bol_login(page.url):
                break
            try:
                page.wait_for_load_state("load", timeout=6000)
            except Exception:
                pass
        ok = (not _url_suggests_bol_login(page.url)) or bool(_extract_ideal_from_url(page.url))
        if ok and context and state_path:
            _persist_storage_state(context, state_path, fp_bundle)
        return ok

    _cprint("[CHECKOUT] Login submitted — waiting for checkout redirect…", flush=True)
    ok = _checkout_wait_for_post_login_redirect(page, initial_login_url)
    if not ok:
        ext = float(os.getenv("BOL_CHECKOUT_LOGIN_SILENT_EXTEND_SEC", "12"))
        ok = _silent_extend_if_still_login_host(page, initial_login_url, extra_sec=ext)

    if ok:
        _cprint("[CHECKOUT] Re-authenticated — checkout redirect OK.", flush=True)
        if context and state_path:
            _persist_storage_state(context, state_path, fp_bundle)
        return True

    cur_late = _page_url_safe(page, attempts=10, pause=0.12)
    if _checkout_post_submit_success(initial_login_url, cur_late) or _extract_ideal_from_url(
        cur_late
    ):
        _cprint("[CHECKOUT] Re-authenticated — checkout redirect OK (late arrival).", flush=True)
        if context and state_path:
            _persist_storage_state(context, state_path, fp_bundle)
        return True

    _cprint(
        "[CHECKOUT] Login submitted but still on WSP/login — not pretending checkout succeeded.",
        flush=True,
    )
    if context and state_path:
        _persist_storage_state(context, state_path, fp_bundle)
    return False


def _checkout_modal_post_click_settle(page) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=35000)
    except Exception:
        pass
    time.sleep(random.uniform(0.14, 0.38))


def _atc_overlay_after_cta_click(page) -> None:
    """Naar de kassa / Verder naar bestellen navigates to basket — wait for navigation only; no extra delays."""
    try:
        page.wait_for_load_state("domcontentloaded", timeout=14000)
    except Exception:
        pass


def _click_atc_confirmation_naar_de_kassa(page) -> bool:
    """
    Caller should idle ~3s after ATC so the sheet can open. Then a short budget (default 2s)
    to show the dialog and smoothly click **whichever** of Naar de kassa | Verder naar bestellen matches first
    (parallel name-regex on button ∪ link inside the overlay).
    """
    budget_sec = float(os.getenv("BOL_CHECKOUT_ATC_OVERLAY_ACTION_MAX_SEC", "2"))
    deadline = time.time() + max(0.35, budget_sec)

    def _ms_left() -> int:
        return max(200, int((deadline - time.time()) * 1000))

    modal_pat = re.compile(
        r"winkelwagen|toegevoegd|verder\s+naar\s+bestellen|naar\s+de\s+kassa",
        re.I,
    )
    dlg = page.locator('[role="dialog"]').filter(has_text=modal_pat).first
    try:
        dlg.wait_for(state="visible", timeout=_ms_left())
    except Exception:
        _cprint(
            "[CHECKOUT] ATC overlay not visible within overlay budget — opening basket URL instead.",
            flush=True,
        )
        return False

    time.sleep(random.uniform(0.04, 0.11))

    cta_name = re.compile(
        r"^\s*(Naar\s+de\s+kassa|Verder\s+naar\s+bestellen)\s*$",
        re.I,
    )

    try:
        cta = dlg.get_by_role("button", name=cta_name).or_(
            dlg.get_by_role("link", name=cta_name)
        ).first
        cta.wait_for(state="visible", timeout=_ms_left())
        ms = _ms_left()
        ok = _human_like_click(page, cta, timeout_ms=ms)
        if not ok:
            cta.click(timeout=max(800, ms))
        _cprint(
            "[CHECKOUT] Clicked first matching ATC overlay CTA (Naar de kassa | Verder naar bestellen).",
            flush=True,
        )
        _atc_overlay_after_cta_click(page)
        return True
    except Exception:
        pass

    try:
        alt = dlg.locator("button, a").filter(has_text=cta_name).first
        alt.wait_for(state="visible", timeout=_ms_left())
        ms = _ms_left()
        ok = _human_like_click(page, alt, timeout_ms=ms)
        if not ok:
            alt.click(timeout=max(800, ms))
        _cprint(
            "[CHECKOUT] Clicked ATC overlay CTA via dialog text filter "
            "(Naar de kassa | Verder naar bestellen).",
            flush=True,
        )
        _atc_overlay_after_cta_click(page)
        return True
    except Exception:
        pass

    _cprint(
        "[CHECKOUT] ATC overlay — no matching CTA within overlay budget — opening basket URL instead.",
        flush=True,
    )
    return False


def _checkout_flow_browser(
    page,
    mod,
    *,
    email: str = "",
    password: str = "",
    context=None,
    state_path: Path | None = None,
    fp_bundle: tuple[Path, str, str] | None = None,
    skip_direct_basket_goto: bool = False,
) -> str | None:
    """
    Payment URL **only inside Chromium** — mandje → Verder naar bestellen → … → iDEAL.
    Prefer **Naar de kassa** or **Verder naar bestellen** on the post-ATC overlay over direct basket URL when possible.
    """
    _cprint(
        "[CHECKOUT] Browser-only payment flow — cookies stay in this window (no HTTP checkout).",
        flush=True,
    )
    basket = os.getenv(
        "BOL_CHECKOUT_BASKET_URL",
        "https://www.bol.com/nl/nl/order/basket.html",
    ).strip()
    if skip_direct_basket_goto:
        _cprint(
            "[CHECKOUT] Using basket reached via overlay (Naar de kassa / Verder naar bestellen) — "
            "skipping direct basket URL navigation.",
            flush=True,
        )
        try:
            page.wait_for_load_state("domcontentloaded", timeout=28000)
        except Exception:
            pass
        try:
            u_ov = (page.url or "").lower()
        except Exception:
            u_ov = ""
        if "/nl/nl/p/" in u_ov or ("/p/" in u_ov and "bol.com" in u_ov):
            _cprint(
                "[CHECKOUT] Still on PDP — ATC overlay CTA missed; opening basket URL.",
                flush=True,
            )
            if not _goto_with_retries(page, basket, label="winkelwagen (fallback)"):
                _cprint("[CHECKOUT] Could not open basket URL — abort checkout this round.", flush=True)
                return None
            try:
                page.wait_for_load_state("domcontentloaded", timeout=26000)
            except Exception:
                pass
    else:
        _cprint(
            "[CHECKOUT] Opening mandje via basket URL — daarna afrekenen in same tab.",
            flush=True,
        )
        if not _goto_with_retries(page, basket, label="winkelwagen"):
            _cprint("[CHECKOUT] Could not open basket URL — abort checkout this round.", flush=True)
            return None
        try:
            page.wait_for_load_state("domcontentloaded", timeout=26000)
        except Exception:
            pass
    _dismiss_cookie_wall(page)
    _checkout_log_step_page(page, phase="after basket open")
    _checkout_pause(
        "BOL_CHECKOUT_AFTER_BASKET_OPEN_MIN_SEC",
        "BOL_CHECKOUT_AFTER_BASKET_OPEN_MAX_SEC",
        0.45,
        1.05,
    )

    ideal = _extract_ideal_from_url(page.url)
    if ideal:
        _cprint("[CHECKOUT] Payment gateway opened (browser).", flush=True)
        return ideal

    if _url_suggests_bol_login(page.url):
        if not _checkout_handle_login_if_needed(
            page, email, password, context, state_path, fp_bundle
        ):
            _cprint(
                "[CHECKOUT] Aborting checkout round — could not leave login after auto-fill.",
                flush=True,
            )
            return None
        _checkout_log_land_after_login(page)

    for step in range(36):
        ideal = _extract_ideal_from_url(page.url)
        if ideal:
            _cprint("[CHECKOUT] Payment gateway opened (browser).", flush=True)
            return ideal

        if _url_suggests_bol_login(page.url):
            if not _checkout_handle_login_if_needed(
                page, email, password, context, state_path, fp_bundle
            ):
                _cprint(
                    "[CHECKOUT] Aborting checkout round — stuck on login mid-flow.",
                    flush=True,
                )
                return None
            _checkout_log_land_after_login(page)

        try:
            cur = page.url or ""
        except Exception:
            cur = ""
        if _checkout_url_looks_like_runway(cur):
            _cprint(
                "[CHECKOUT] Checkout runway loaded — waiting before tapping CTAs "
                "(e.g. Bestellen en betalen / iDEAL)…",
                flush=True,
            )
        _checkout_log_step_page(page, step=step)
        _checkout_pause(
            "BOL_CHECKOUT_BEFORE_STEP_ACTION_MIN_SEC",
            "BOL_CHECKOUT_BEFORE_STEP_ACTION_MAX_SEC",
            0.38,
            0.92,
        )

        labels_next = _checkout_labels_for_url(cur)

        _dismiss_cookie_wall(page)
        clicked = False
        for lab in labels_next:
            if _checkout_click_dutch_cta(page, lab):
                clicked = True
                break

        if clicked:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=24000)
            except Exception:
                pass
            _checkout_pause(
                "BOL_CHECKOUT_AFTER_CTA_NAV_MIN_SEC",
                "BOL_CHECKOUT_AFTER_CTA_NAV_MAX_SEC",
                0.42,
                1.05,
            )
            continue

        try:
            il = page.locator("text=/^i\\s*DEAL$/i").first
            if il.is_visible(timeout=800):
                if _human_like_click(page, il):
                    clicked = True
                    _checkout_pause(
                        "BOL_CHECKOUT_AFTER_IDEAL_CLICK_MIN_SEC",
                        "BOL_CHECKOUT_AFTER_IDEAL_CLICK_MAX_SEC",
                        0.28,
                        0.72,
                    )
        except Exception:
            pass

        if clicked:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=24000)
            except Exception:
                pass
            _checkout_pause(
                "BOL_CHECKOUT_AFTER_CTA_NAV_MIN_SEC",
                "BOL_CHECKOUT_AFTER_CTA_NAV_MAX_SEC",
                0.38,
                0.95,
            )
            ideal = _extract_ideal_from_url(page.url)
            if ideal:
                _cprint("[CHECKOUT] Payment gateway opened (browser).", flush=True)
                return ideal
            continue

        if step == 12:
            _checkout_log_step_page(page, phase="recovery checkout URL")
            _checkout_pause(
                "BOL_CHECKOUT_BEFORE_RECOVERY_GOTO_MIN_SEC",
                "BOL_CHECKOUT_BEFORE_RECOVERY_GOTO_MAX_SEC",
                0.45,
                1.05,
            )
            try:
                page.goto(mod.CHECKOUT_PAGE_URL, wait_until="domcontentloaded", timeout=90000)
                page.wait_for_load_state("domcontentloaded", timeout=24000)
            except Exception:
                pass

        _checkout_pause(
            "BOL_CHECKOUT_IDLE_POLL_MIN_SEC",
            "BOL_CHECKOUT_IDLE_POLL_MAX_SEC",
            0.28,
            0.62,
        )
        ideal = _extract_ideal_from_url(page.url)
        if ideal:
            _cprint("[CHECKOUT] Payment gateway opened (browser).", flush=True)
            return ideal

    _cprint("[CHECKOUT] Could not reach payment URL (max steps).", flush=True)
    return None


def _slot_inner_iteration(
    slot: int,
    page,
    context,
    state_path: Path,
    fp_bundle: tuple[Path, str, str] | None,
    *,
    url: str,
    poll_idx: int,
    mod,
    email: str,
    password: str,
    discord_webhook,
    discord_thread_id,
    discord_thread_name,
    reload_every: int,
    reload_sec: float,
) -> tuple[bool, int, float | None, bool]:
    """
    Single iteration of the monitor inner loop for one context.
    Returns (continue_inner_loop, new_poll_idx, pause_until_epoch_when_continue, checkout_or_offline_cycle_done).
    Fourth is True after offline-wait, after checkout completes, or while continuing online monitor;
    False when re-login failed (session lost).
    """
    sp = f"[S{slot + 1}] "
    _set_checkout_log_slot(slot + 1)
    pid = mod.extract_product_id(url) or ""

    if "account/login" in (page.url or "").lower():
        print(f"{sp}[LOGIN] Session invalid — logging in again…", flush=True)
        if not ensure_logged_in(
            page, context, state_path, email, password, fp_bundle=fp_bundle
        ):
            time.sleep(reload_sec)
            return False, poll_idx, None, False

    full_reload = poll_idx % reload_every == 0
    snap = _evaluate_product_snapshot(page, url, mod, navigate=full_reload)
    poll_idx += 1

    if snap.get("offline"):
        _print_monitor_offline_log(sp, snap, url_hint=url)
        poll_idx = 0
        return True, poll_idx, time.time() + _pause_offline_duration(), True

    if snap["oos"]:
        print(
            f"{sp}[MONITOR] confirmed OOS (niet op voorraad), skip cart: {url[:72]}…",
            flush=True,
        )
        return True, poll_idx, time.time() + _pause_online_duration(), False

    if not snap["can_add"]:
        mode = "full PDP reload" if snap.get("navigated") else "light DOM poll"
        if snap.get("has_op_voorraad"):
            poll_hint = "Op voorraad seen — looking for In winkelwagen / Toevoegen button…"
        else:
            poll_hint = (
                'checking add-to-cart (yellow button: usually '
                '"In winkelwagen" or "Toevoegen aan winkelwagen")…'
            )
        print(
            f"{sp}[MONITOR] online — {mode}; {poll_hint} {url[:56]}…",
            flush=True,
        )
        return True, poll_idx, time.time() + _pause_online_duration(), False

    try:
        cur_u = (page.url or "").strip()
    except Exception:
        cur_u = ""
    print(
        f"{sp}[MONITOR] buy signal — PDP has add-to-cart; checking out from this page "
        f"(url={cur_u[:110]!r}…).",
        flush=True,
    )

    if not _click_add_to_cart(page, sp=sp):
        return True, reload_every - 1, time.time() + _pause_online_duration(), False

    idle_atc = float(os.getenv("BOL_CHECKOUT_AFTER_ATC_IDLE_SEC", "1.0"))
    print(
        f"{sp}[CHECKOUT] Idle {idle_atc:.1f}s on PDP — let add-to-cart overlay open "
        f"(wait_for_timeout + domcontentloaded settle before overlay).",
        flush=True,
    )
    if not _ensure_atc_confirmed_before_checkout(
        page, sp=sp, product_url=url, mod=mod, idle_sec=idle_atc
    ):
        return True, reload_every - 1, time.time() + _pause_online_duration(), False

    try:
        try:
            t2 = _read_product_title_from_pdp(page)
            if t2:
                snap["product_title"] = t2
        except Exception:
            pass

        try:
            pf_dom = _read_shelf_price_from_pdp_dom(page)
            if pf_dom:
                snap["price_text"] = pf_dom
            elif not snap.get("price_text"):
                snap["price_text"] = _extract_price_text_from_pdp_html(page.content())
        except Exception:
            pass

        ptitle = (snap.get("product_title") or "").strip() or "(unknown)"
        price_disp = snap.get("price_text") or "?"
        cart_note = "added to basket"
        print(
            f"{sp}[CART] {cart_note} — title={ptitle} | price={price_disp} | "
            f"product_id={pid or '?'} | url={url}",
            flush=True,
        )

        _persist_storage_state(context, state_path, fp_bundle)

        print(
            f"{sp}[CHECKOUT] Overlay step — first match 'Naar de kassa' | 'Verder naar bestellen' "
            f"(max {float(os.getenv('BOL_CHECKOUT_ATC_OVERLAY_ACTION_MAX_SEC', '2')):.0f}s)…",
            flush=True,
        )
        via_atc_overlay = _click_atc_confirmation_naar_de_kassa(page)

        ideal_url = _checkout_flow_browser(
            page,
            mod,
            email=email,
            password=password,
            context=context,
            state_path=state_path,
            fp_bundle=fp_bundle,
            skip_direct_basket_goto=via_atc_overlay,
        )

        if ideal_url:
            ou = str(snap.get("offer_uid") or "unknown")
            cnt = mod.save_payment_url(
                ideal_url,
                url,
                pid,
                ou,
                None,
            )
            disc_body = mod.format_discord_payment_block(
                ideal_url,
                product_url=url,
                product_id=str(pid or ""),
                offer_uid=str(ou),
                price=str(price_disp),
                title=ptitle,
                seller=None,
            )
            mod.send_discord_message(
                discord_webhook,
                disc_body,
                discord_thread_id,
                discord_thread_name,
                background=False,
            )
            print(
                f"{sp}[DISCORD] sent payment notification (URL + item block). "
                f"[CHECKOUT] payment_url={ideal_url} | saved_count={cnt} | "
                f"title={ptitle!r} | product_id={pid} | offer_uid={ou}",
                flush=True,
            )
        else:
            print(
                f"{sp}[CHECKOUT] incomplete — check basket / ideal in browser. "
                f"last_product_title={ptitle!r}",
                flush=True,
            )

        return False, 0, None, True
    finally:
        _set_checkout_log_slot(0)


def _browser_slot_worker(
    *,
    slot: int,
    mod,
    use_proxy: bool,
    proxy_rows: list,
    session_paths: list[Path],
    fp_bundles: list,
    slot_http_urls: list[str],
    sticky_slot_product_url: bool,
    email: str,
    password: str,
    discord_webhook,
    discord_thread_id,
    discord_thread_name,
    reload_sec: float,
    max_succ: int,
    slow_mo_ms: int,
    channel: str | None,
    shutdown_ev: threading.Event,
    login_outcomes: list[bool | None],
    failover_pool_lines: list[tuple[str, str, str, str]],
    enabled_slot_indices_sorted: tuple[int, ...],
) -> None:
    """One Playwright sync loop per thread — same PDP monitor → checkout flow for every slot."""
    from playwright.sync_api import sync_playwright

    sid = slot + 1
    sp = f"[S{sid}] "
    _set_checkout_log_slot(sid)
    # Tile windows by **enabled browser order** (first opened = top-left), not by absolute S2/S3 index —
    # avoids “only S1 runs but window sits where S3 used to” confusion vs session files.
    if enabled_slot_indices_sorted:
        try:
            pos_idx = enabled_slot_indices_sorted.index(slot)
        except ValueError:
            pos_idx = 0
        n_enabled_windows = len(enabled_slot_indices_sorted)
    else:
        pos_idx = min(slot, 2)
        n_enabled_windows = 3
    wx = 80 + pos_idx * 72
    wy = 40 + pos_idx * 48

    cookies_dir = session_paths[slot].parent
    fp_txt = _session_fingerprint_txt_path(cookies_dir, sid)
    legacy_fp = _session_fingerprint_legacy_fp_path(cookies_dir, sid)
    state_path = session_paths[slot]

    expected_session_name = f"session_{sid}.json"
    if state_path.name != expected_session_name:
        print(
            f"{sp}[BIND] Internal error: storage path {state_path.name!r} ≠ expected {expected_session_name!r}.",
            flush=True,
        )
    proxy_ln = f"proxy.txt line {sid}" if use_proxy else "direct (USE_PROXIES=0)"
    print(
        f"{sp}[BIND] Chromium S{sid} ⟷ `{state_path.name}` ⟷ `{fp_txt.name}` ⟷ {proxy_ln} "
        f"⟷ login {_mask_login_hint(email)} "
        f"(window #{pos_idx + 1}/{n_enabled_windows})",
        flush=True,
    )

    # Default 0 so parallel Chromiums start together; stagger made slot 2 look \"stuck\" on CMP while S1 advanced.
    _st_ms = max(0, int(os.getenv("BOL_BROWSER_SLOT_LAUNCH_STAGGER_MS", "0")))
    if _st_ms > 0 and pos_idx > 0:
        time.sleep(pos_idx * (_st_ms / 1000.0))

    failover_enabled = use_proxy and (
        os.getenv("BOL_BROWSER_FAILOVER_ENABLED", "1").strip().lower()
        not in ("0", "false", "no", "off")
    )
    primary_proxy = proxy_rows[slot] if use_proxy else ("", "", "", "")
    chain: list[tuple[str, str, str, str]] = []
    if use_proxy:
        chain = _unique_proxy_chain(primary_proxy, failover_pool_lines)
    proxy_cursor = 0
    consecutive_auth_resets = 0
    login_failover_ev = threading.Event()
    fb_box: list = [fp_bundles[slot]]
    active_pidx = 0
    h = pt = u = pw = ""

    try:
        while not shutdown_ev.is_set():
            login_failover_ev.clear()

            current_proxy_row: tuple[str, str, str, str] | None = None
            ctx_kw: dict = {
                "viewport": {"width": 1360, "height": 880},
                "locale": "nl-NL",
                "timezone_id": "Europe/Amsterdam",
            }

            if use_proxy:
                picked = _pick_live_proxy_index(chain, proxy_cursor)
                if picked is None:
                    wait_sec = min(
                        max(ProxyCooldownRegistry.min_remaining_chain(chain), 3.0),
                        120.0,
                    )
                    print(
                        f"{sp}[FAILOVER] All proxies cooling — retry in ~{wait_sec:.0f}s.",
                        flush=True,
                    )
                    if shutdown_ev.wait(wait_sec):
                        break
                    continue
                active_pidx, (h, pt, u, pw) = picked
                current_proxy_row = (h, pt, u, pw)
                ctx_kw["proxy"] = {
                    "server": f"http://{h}:{pt}",
                    "username": u,
                    "password": pw,
                }
                fp_hex = _proxy_tuple_fingerprint(h, pt, u, pw)
                hint = _proxy_hint_log(h, pt, u)
                fb_box[0] = (fp_txt, fp_hex, hint)
            else:
                fp_hex = _FP_DIRECT
                hint = "direct (USE_PROXIES=0)"
                fb_box[0] = fp_bundles[slot]

            if use_proxy and consecutive_auth_resets > 0:
                _clear_slot_session_storage(state_path, fp_txt, legacy_fp, sp)
                drel = _failover_relogin_delay_sec()
                print(
                    f"{sp}[FAILOVER] Context reset + clean cookies — next identity {hint}. "
                    f"Relogin delay {drel:.1f}s.",
                    flush=True,
                )
                if shutdown_ev.wait(drel):
                    break

            ctx_kw.pop("storage_state", None)
            if state_path.is_file():
                st = _read_stored_proxy_fingerprint_hex(fp_txt, legacy_fp)
                if st == fp_hex:
                    ctx_kw["storage_state"] = str(state_path)

            if ctx_kw.get("storage_state"):
                print(
                    f"{sp}[BIND] Loading saved cookies: {Path(ctx_kw['storage_state']).name}",
                    flush=True,
                )
            else:
                print(
                    f"{sp}[BIND] No matching cookie file for this proxy — clean login; "
                    f"will persist to `{state_path.name}`.",
                    flush=True,
                )

            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=False,
                    channel=channel,
                    slow_mo=slow_mo_ms,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        f"--window-position={wx},{wy}",
                    ],
                )
                print(f"{sp}[BROWSER] Chromium opened (parallel slot thread).", flush=True)
                ctx = browser.new_context(**ctx_kw)
                page = ctx.new_page()

                if not session_paths[slot].is_file():
                    print(
                        f"{sp}[SESSION] No session file yet — NL homepage → Inloggen → "
                        f"{session_paths[slot].name}.",
                        flush=True,
                    )

                persist_ok = True
                try:
                    try:
                        logged = ensure_logged_in(
                            page,
                            ctx,
                            session_paths[slot],
                            email,
                            password,
                            fp_bundle=fb_box[0],
                        )
                    except Exception as e:
                        print(f"{sp}[BROWSER] Login exception: {e}", flush=True)
                        traceback.print_exc()
                        logged = False

                    if not logged:
                        proxy_ln = f"proxy.txt line {sid}" if use_proxy else "direct (USE_PROXIES=0)"
                        print(
                            f"{sp}[BROWSER] NL homepage / login failed ({proxy_ln}). "
                            "Other slots continue if healthy.",
                            flush=True,
                        )
                        if use_proxy and failover_enabled and current_proxy_row is not None:
                            raise FailoverIdentityRestart()
                        login_outcomes[slot] = False
                        return

                    if use_proxy and failover_enabled:
                        if not _verify_browser_login_visible(page):
                            print(
                                f"{sp}[FAILOVER] Login verify failed (no Welkom on storefront) — "
                                f"rotating identity.",
                                flush=True,
                            )
                            raise FailoverIdentityRestart()

                    login_outcomes[slot] = True
                    consecutive_auth_resets = 0

                    ord_probe, probe_load_err = _load_ordered_http_products_retry(
                        mod, phase="post-login PDP probe"
                    )
                    probe_u = _pick_slot_product_url(
                        slot, ord_probe, slot_http_urls, sticky=sticky_slot_product_url
                    )
                    # One PDP load here — monitor starts with poll_idx=1 so first poll does not full-reload again.
                    if probe_u:
                        if probe_load_err or (
                            not sticky_slot_product_url and slot >= len(ord_probe)
                        ):
                            _note = probe_load_err or "fewer http rows than slots"
                            print(
                                f"{sp}[MONITOR] PDP probe — slot-bound URL; CSV note: {_note}",
                                flush=True,
                            )
                        _immediate_pdp_offline_probe(page, probe_u, mod, sp)
                    else:
                        _reset_to_nl_storefront(page, sp=sp)
                        print(
                            f"{sp}[MONITOR] No product URL for this slot — CSV has only "
                            f"{len(ord_probe)} http URL(s); add row {sid} in {mod.PRODUCTS_FILE} "
                            f"or disable tab {sid}. Browser stays on storefront until fixed.",
                            flush=True,
                        )

                    su_lo = float(os.getenv("BOL_BROWSER_POST_LOGIN_DELAY_MIN", "0.9"))
                    su_hi = float(os.getenv("BOL_BROWSER_POST_LOGIN_DELAY_MAX", "2.2"))
                    if su_hi < su_lo:
                        su_lo, su_hi = su_hi, su_lo
                    time.sleep(random.uniform(su_lo, su_hi))
                    print(f"{sp}[MONITOR] Post-login pause done — product queue.", flush=True)

                    warned_short_products = False
                    warned_csv_fallback = False
                    while not shutdown_ev.is_set():
                        reload_every = max(
                            2,
                            int(os.getenv("BOL_BROWSER_FULL_RELOAD_EVERY_N_POLLS", "8")),
                        )
                        products, err = mod.try_load_products_runtime(mod.PRODUCTS_FILE)
                        ordered = (
                            _ordered_http_products(products) if (not err and products) else []
                        )
                        url = _pick_slot_product_url(
                            slot, ordered, slot_http_urls, sticky=sticky_slot_product_url
                        )

                        if err and not url:
                            if shutdown_ev.wait(reload_sec):
                                break
                            continue
                        if err and url and not warned_csv_fallback:
                            print(
                                f"{sp}[MONITOR] CSV unreadable — using startup URL snapshot until load succeeds.",
                                flush=True,
                            )
                            warned_csv_fallback = True

                        if not err and not products and not url:
                            if shutdown_ev.wait(reload_sec):
                                break
                            continue

                        def run_slot_product_url(target_url: str) -> None:
                            # Avoid duplicate PDP goto: post-login probe already landed on this URL;
                            # poll_idx % reload_every == 0 triggers full reload — start at 1 so first poll is light DOM.
                            poll_idx_local = 1
                            pause_until_local = 0.0
                            inner_active = True
                            while inner_active and not shutdown_ev.is_set():
                                now_t = time.time()
                                if now_t < pause_until_local:
                                    delay = min(0.15, pause_until_local - now_t + 0.02)
                                    if shutdown_ev.wait(max(delay, 0.02)):
                                        break
                                    continue
                                (
                                    cont,
                                    poll_idx_local,
                                    pause_abs,
                                    cycle_ok,
                                ) = _slot_inner_iteration(
                                    slot,
                                    page,
                                    ctx,
                                    session_paths[slot],
                                    fb_box[0],
                                    url=target_url,
                                    poll_idx=poll_idx_local,
                                    mod=mod,
                                    email=email,
                                    password=password,
                                    discord_webhook=discord_webhook,
                                    discord_thread_id=discord_thread_id,
                                    discord_thread_name=discord_thread_name,
                                    reload_every=reload_every,
                                    reload_sec=reload_sec,
                                )
                                if shutdown_ev.is_set():
                                    break
                                if not cont:
                                    if use_proxy and failover_enabled and not cycle_ok:
                                        print(
                                            f"{sp}[FAILOVER] Session lost (re-login required) — "
                                            f"will reset context + rotate proxy.",
                                            flush=True,
                                        )
                                        login_failover_ev.set()
                                    inner_active = False
                                else:
                                    pause_until_local = (
                                        pause_abs if pause_abs is not None else time.time()
                                    )

                        if not url:
                            if not warned_short_products:
                                print(
                                    f"{sp}[MONITOR] Waiting — no product URL at CSV index {sid} "
                                    f"(have {len(ordered)} URL(s)). Add row {sid} or disable this slot; "
                                    f"retry every {reload_sec:.0f}s.",
                                    flush=True,
                                )
                                warned_short_products = True
                            if shutdown_ev.wait(reload_sec):
                                break
                            continue

                        if mod.get_product_success_count(url) >= max_succ:
                            if shutdown_ev.wait(reload_sec):
                                break
                            continue

                        run_slot_product_url(url)
                        if login_failover_ev.is_set() and failover_enabled:
                            raise FailoverIdentityRestart()

                        if shutdown_ev.wait(reload_sec):
                            break
                except FailoverIdentityRestart:
                    persist_ok = False
                    if (
                        use_proxy
                        and failover_enabled
                        and current_proxy_row is not None
                    ):
                        ProxyCooldownRegistry.mark(
                            *current_proxy_row,
                            _failover_proxy_cooldown_sec(),
                        )
                        proxy_cursor = (active_pidx + 1) % max(len(chain), 1)
                        consecutive_auth_resets += 1
                    login_failover_ev.clear()
                finally:
                    try:
                        if login_outcomes[slot] is True and persist_ok:
                            _persist_storage_state(
                                ctx, session_paths[slot], fb_box[0]
                            )
                    except Exception:
                        pass
                    try:
                        ctx.close()
                    except Exception:
                        pass
                    try:
                        browser.close()
                    except Exception:
                        pass

            if shutdown_ev.is_set():
                break
    except Exception as e:
        print(f"{sp}[BROWSER] Worker fatal: {e}", flush=True)
        traceback.print_exc()
        if login_outcomes[slot] is None:
            login_outcomes[slot] = False


def run_browser_mode_entry() -> None:
    cwd = os.getcwd()
    _load_dotenv_simple(os.path.join(cwd, ".env"))

    mod = _load_bol_module()
    truthy = mod._env_truthy

    if not truthy("USE_PROXIES", "1"):
        print("[BROWSER] USE_PROXIES=0 — launching without proxy (direct).", flush=True)

    base_parallel_cap = max(1, min(3, int(os.getenv("BOL_BROWSER_MAX_PARALLEL", "3"))))
    sticky_slot_product_url = _sticky_slot_product_url_default()

    mod.ensure_products_csv(mod.PRODUCTS_FILE)
    mod.initialize_product_success_counts(mod.PAYMENT_URLS_FILE)

    products_preview, err = mod.try_load_products_runtime(mod.PRODUCTS_FILE)
    ordered_preview = _ordered_http_products([] if err else products_preview)

    proxy_file = Path(cwd) / mod.PROXY_FILE
    use_proxy = truthy("USE_PROXIES", "1")
    proxy_rows = _parse_all_proxy_lines(proxy_file) if use_proxy else []

    pool_file_name = (
        os.getenv("BOL_BROWSER_FAILOVER_POOL_FILE") or DEFAULT_FAILOVER_POOL_FILE
    ).strip()
    failover_pool_path = Path(cwd) / pool_file_name
    _ensure_failover_pool_file(failover_pool_path)
    failover_pool_lines = (
        _parse_all_proxy_lines(failover_pool_path) if use_proxy else []
    )

    if use_proxy and not proxy_rows:
        print(
            "[BROWSER] USE_PROXIES=1 but proxy.txt has no valid host:port:user:pass line.",
            flush=True,
        )
        sys.exit(1)

    if not ordered_preview:
        print(
            f"[BROWSER] No http product URLs in {mod.PRODUCTS_FILE} — add at least one URL.",
            flush=True,
        )
        sys.exit(1)

    if use_proxy:
        n_resource = min(len(ordered_preview), len(proxy_rows), base_parallel_cap)
    else:
        # Same parallelism rule as proxy mode (minus proxy rows): each slot runs the identical
        # worker — login → PDP probe → monitor → ATC → checkout. Old min(1,…) forced only S1.
        n_resource = min(len(ordered_preview), base_parallel_cap)

    repeat_last = truthy("BOL_BROWSER_REPEAT_LAST_URL_FOR_SLOTS", "0")
    if repeat_last:
        if use_proxy:
            slot_budget = min(len(proxy_rows), base_parallel_cap)
        else:
            slot_budget = base_parallel_cap
        if slot_budget > n_resource:
            print(
                "[SETUP] BOL_BROWSER_REPEAT_LAST_URL_FOR_SLOTS=1 — extra Chromium slot(s) use the "
                "last http URL from CSV with their proxy line (same PDP, different identity).",
                flush=True,
            )
            n_resource = slot_budget

    slot_http_urls: list[str] = []
    for _i in range(n_resource):
        _j = min(_i, len(ordered_preview) - 1)
        slot_http_urls.append((ordered_preview[_j][1] or "").strip())

    if n_resource < 1:
        print("[BROWSER] No monitor slots — check products and BOL_BROWSER_MAX_PARALLEL.", flush=True)
        sys.exit(1)

    if truthy("BOL_BROWSER_USE_SLOT_ENABLE_FLAGS", "0"):
        requested_slots: list[int] = []
        if truthy("BOL_BROWSER_ENABLE_SLOT_1", "1"):
            requested_slots.append(0)
        if truthy("BOL_BROWSER_ENABLE_SLOT_2", "1"):
            requested_slots.append(1)
        if truthy("BOL_BROWSER_ENABLE_SLOT_3", "1"):
            requested_slots.append(2)
        enabled_slot_indices = [s for s in requested_slots if s < n_resource]
        if not enabled_slot_indices:
            print(
                "[BROWSER] No Chromium slots — BOL_BROWSER_USE_SLOT_ENABLE_FLAGS=1 but every "
                "BOL_BROWSER_ENABLE_SLOT_n is off or none fit CSV/proxy rows.",
                flush=True,
            )
            sys.exit(1)
        skipped_wr = [s + 1 for s in requested_slots if s >= n_resource]
        if skipped_wr:
            print(
                f"[SETUP] Slot(s) {[f'S{x}' for x in skipped_wr]} flagged but "
                f"need CSV row + proxy line {max(skipped_wr)} — not started.",
                flush=True,
            )
    else:
        enabled_slot_indices = list(range(n_resource))

    if use_proxy:
        # Reminder at startup: failover is separate from proxy.txt primary lines (optional pool).
        if failover_pool_lines:
            print(
                f"[SETUP] Failover pool `{failover_pool_path.name}`: "
                f"{len(failover_pool_lines)} extra line(s) — used only when login/session breaks "
                f"(not for normal offline PDP). Cooldown: BOL_BROWSER_PROXY_FAIL_COOLDOWN_SEC "
                f"(default 900); relogin delay: BOL_BROWSER_FAILOVER_RELOGIN_MIN_SEC/"
                f"MAX_SEC (default 10–30).",
                flush=True,
            )
        else:
            print(
                f"[SETUP] `{failover_pool_path.name}` — no optional backup lines (normal). "
                f"Slots use `{mod.PROXY_FILE}`; add spare host:port:user:pass rows here only if you want "
                f"extra identities after login/session recovery.",
                flush=True,
            )

    if not repeat_last and len(ordered_preview) > n_resource:
        print(
            f"[SETUP] {len(ordered_preview) - n_resource} product URL(s) exceed proxy lines / parallel cap "
            f"— only the first {n_resource} URL(s) are monitored (CSV order ↔ proxy line index).",
            flush=True,
        )
    if use_proxy and len(proxy_rows) > n_resource:
        print(
            f"[SETUP] {len(proxy_rows) - n_resource} proxy line(s) unused (fewer products or cap).",
            flush=True,
        )
    if not use_proxy and len(ordered_preview) > 1:
        print(
            f"[SETUP] USE_PROXIES=0 — up to {n_resource} Chromium slot(s) from CSV order "
            f"(same flow per slot as S1; separate session_N.json per tab).",
            flush=True,
        )

    cookies_dir = Path(cwd) / "cookies"
    cookies_dir.mkdir(parents=True, exist_ok=True)

    print("[SETUP] ========== Session / proxy alignment (before browser) ==========", flush=True)
    start_labels = ", ".join(f"S{s + 1}" for s in enabled_slot_indices)
    print(
        "[SETUP] One Chromium per slot index (same worker code; separate threads). "
        f"Starting: {start_labels} — {len(enabled_slot_indices)} window(s); "
        f"{n_resource} URL/proxy row(s) (cap {base_parallel_cap}). "
        "Bind: CSV row N ↔ proxy line N ↔ session_N.json ↔ EmailN/PasswordN.",
        flush=True,
    )
    if len(enabled_slot_indices) > 1:
        print(
            "[SETUP] Parallel slots — each Chromium uses its own Bol account "
            "(Email1/Password1 … Email3/Password3 in .env).",
            flush=True,
        )
    if sticky_slot_product_url:
        print(
            "[SETUP] Sticky product URL per slot — each tab keeps the PDP link bound at launch "
            "(CSV row adds/reorders do not switch S2/S3 to another product). "
            "Disable: BOL_BROWSER_STICKY_SLOT_PRODUCT_URL=0.",
            flush=True,
        )

    session_paths: list[Path] = []
    fp_bundles: list[tuple[Path, str, str]] = []

    for slot in range(n_resource):
        sid = slot + 1
        em_slot, pw_slot = _bol_credentials_for_slot(slot)
        if slot in enabled_slot_indices:
            if not em_slot.strip() or not pw_slot.strip():
                if slot == 0:
                    need = "Email1/Password1 or legacy Email/Password (and matching password keys)"
                else:
                    need = f"Email{sid}/Password{sid} (or BOL_EMAIL{sid}/BOL_PASSWORD{sid})"
                print(
                    f"[SETUP] Slot {sid} has empty credentials — set {need} in .env. "
                    "Each parallel slot must use its own Bol account so sessions do not overlap.",
                    flush=True,
                )
                sys.exit(1)

        state_path = cookies_dir / f"session_{sid}.json"
        fp_txt = _session_fingerprint_txt_path(cookies_dir, sid)
        legacy_fp = _session_fingerprint_legacy_fp_path(cookies_dir, sid)
        session_paths.append(state_path)

        if use_proxy:
            host, port, user, pw = proxy_rows[slot]
            current_fp = _proxy_tuple_fingerprint(host, port, user, pw)
            proxy_desc = _proxy_hint_log(host, port, user)
        else:
            current_fp = _FP_DIRECT
            proxy_desc = "direct (USE_PROXIES=0)"

        fp_bundles.append((fp_txt, current_fp, proxy_desc))

        if slot in enabled_slot_indices:
            print(f"[SETUP] Slot {sid}: {state_path.name} + {fp_txt.name}", flush=True)
            print(f"[SETUP] Slot {sid} login user (masked): {_mask_login_hint(em_slot)}", flush=True)
            print(f"[SETUP] Route: {proxy_desc}", flush=True)
            print(f"[SETUP] Fingerprint (sha256): {current_fp[:28]}…", flush=True)

        _sync_session_file_with_proxy_fingerprint(
            state_path,
            fp_txt,
            legacy_fp,
            current_fp,
            session_id=sid,
            hint=proxy_desc,
            announce=(slot in enabled_slot_indices),
        )

    skipped_sid = [s + 1 for s in range(n_resource) if s not in enabled_slot_indices]
    if skipped_sid:
        print(
            f"[SETUP] Slots idle this run ({', '.join(f'S{x}' for x in skipped_sid)}) — "
            f"BOL_BROWSER_USE_SLOT_ENABLE_FLAGS=1 with those tabs off; session files on disk unchanged.",
            flush=True,
        )

    if use_proxy and n_resource > 1:
        row_fps: list[str] = []
        for i in range(n_resource):
            h, pt, u, pw = proxy_rows[i]
            row_fps.append(_proxy_tuple_fingerprint(h, pt, u, pw))
        if len(set(row_fps)) < len(row_fps):
            print(
                "[SETUP] Note: some proxy.txt lines use the **same** host:port:user:pass — "
                "fingerprints match on purpose; Bol logins still stay separate (session_1.json vs session_2.json …).",
                flush=True,
            )

    print(
        "[CONFIG] Browser mode — up to 3 parallel Chromiums; same PDP → checkout flow per slot.",
        flush=True,
    )
    print("[SETUP] Alignment complete — preparing Playwright / Chromium next.", flush=True)

    print("[BROWSER] Preparing Playwright + Chromium (first run may download browser)…", flush=True)
    _ensure_playwright()

    discord_webhook = mod.load_discord_webhook()
    discord_thread_id, discord_thread_name = mod.load_discord_thread_config()

    slow_mo_ms = max(0, int(os.getenv("BOL_BROWSER_SLOW_MO_MS", "0")))
    channel = (os.getenv("BOL_BROWSER_CHANNEL") or "").strip() or None

    reload_sec = float(getattr(mod, "PRODUCTS_RELOAD_SECONDS", 6.0))
    max_succ = int(getattr(mod, "MAX_SUCCESSFUL_CHECKOUTS_PER_PRODUCT", 1))

    shutdown_ev = threading.Event()
    login_outcomes: list[bool | None] = [None] * n_resource
    for _s in range(n_resource):
        if _s not in enabled_slot_indices:
            login_outcomes[_s] = True

    print(
        "[BROWSER] Starting one Chromium + Playwright thread per enabled slot "
        "(windows open together).",
        flush=True,
    )

    threads: list[threading.Thread] = []
    enabled_sorted = tuple(sorted(enabled_slot_indices))
    for slot in enabled_slot_indices:
        em_slot, pw_slot = _bol_credentials_for_slot(slot)
        t = threading.Thread(
            target=_browser_slot_worker,
            kwargs={
                "slot": slot,
                "mod": mod,
                "use_proxy": use_proxy,
                "proxy_rows": proxy_rows,
                "session_paths": session_paths,
                "fp_bundles": fp_bundles,
                "slot_http_urls": slot_http_urls,
                "sticky_slot_product_url": sticky_slot_product_url,
                "email": em_slot,
                "password": pw_slot,
                "discord_webhook": discord_webhook,
                "discord_thread_id": discord_thread_id,
                "discord_thread_name": discord_thread_name,
                "reload_sec": reload_sec,
                "max_succ": max_succ,
                "slow_mo_ms": slow_mo_ms,
                "channel": channel,
                "shutdown_ev": shutdown_ev,
                "login_outcomes": login_outcomes,
                "failover_pool_lines": failover_pool_lines,
                "enabled_slot_indices_sorted": enabled_sorted,
            },
            name=f"BolBrowserSlot{slot + 1}",
            daemon=False,
        )
        t.start()
        threads.append(t)

    _sto = (os.getenv("BOL_BROWSER_STARTUP_TIMEOUT_SEC") or "").strip()
    if _sto:
        startup_deadline = time.time() + float(_sto)
    else:
        startup_deadline = time.time() + 900.0
    while (
        any(login_outcomes[s] is None for s in enabled_slot_indices)
        and time.time() < startup_deadline
    ):
        time.sleep(0.05)

    if any(login_outcomes[s] is None for s in enabled_slot_indices):
        print(
            "[BROWSER] Timeout waiting for slot login — stopping.",
            flush=True,
        )
        shutdown_ev.set()
        for t in threads:
            t.join(timeout=120)
        sys.exit(1)

    if not any(login_outcomes[s] for s in enabled_slot_indices):
        print(
            "[BROWSER] Every enabled slot failed during homepage/login — fix proxies / network then retry.",
            flush=True,
        )
        shutdown_ev.set()
        for t in threads:
            t.join(timeout=120)
        sys.exit(1)

    dead_n = sum(1 for s in enabled_slot_indices if login_outcomes[s] is False)
    if dead_n:
        ok_n = sum(1 for s in enabled_slot_indices if login_outcomes[s])
        print(
            f"[MONITOR] {dead_n} slot(s) skipped at startup (bad proxy / tunnel / login) — "
            f"{ok_n} slot(s) monitoring.",
            flush=True,
        )

    print("\nPress Ctrl+C to stop.\n", flush=True)
    interrupted = False
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        interrupted = True
        print("\nCtrl+C — stopping browser mode…", flush=True)
        shutdown_ev.set()
        for t in threads:
            t.join(timeout=120)

    if interrupted:
        print("Stopped.", flush=True)
