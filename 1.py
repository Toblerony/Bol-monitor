import sys
import subprocess
import json
import os
import random
import re
import time
import base64
import csv
import io
import threading
import urllib.request
import urllib.error
from urllib.parse import quote, unquote, urlencode, urlparse, urlunparse

try:
    import monitor as _bol_sitemap_monitor
except ImportError:
    _bol_sitemap_monitor = None

# ─────────────────────────────────────────────
#  AUTO-INSTALL
# ─────────────────────────────────────────────

REQUIRED_PIP_PACKAGES = {
    "curl_cffi": "curl_cffi",
}


def ensure_dependencies():
    needed = []
    for module_name, package_name in REQUIRED_PIP_PACKAGES.items():
        try:
            __import__(module_name)
        except ImportError:
            needed.append(package_name)

    if not needed:
        return

    needed = list(dict.fromkeys(needed))
    print(f"Installing missing modules: {', '.join(needed)}...")
    subprocess.check_call([
        sys.executable,
        "-m",
        "pip",
        "install",
        "--quiet",
        "--disable-pip-version-check",
        *needed,
    ])
    print("Restarting bot after dependency install...")
    os.execv(sys.executable, [sys.executable, *sys.argv])

ensure_dependencies()


def _configure_stdio_encoding() -> None:
    """Avoid UnicodeEncodeError on Windows consoles (cp1252) when printing UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if reconf is not None:
            try:
                reconf(encoding="utf-8", errors="replace")
            except (OSError, ValueError, AttributeError):
                pass


_configure_stdio_encoding()

from curl_cffi.requests import Session

try:
    import sessions_mgr as SESSIONS
except ImportError:
    SESSIONS = None


# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────

try:
    QUANTITY = max(1, min(99, int(os.getenv("CART_QUANTITY", "2"))))
except ValueError:
    QUANTITY = 2
PROXY_FILE               = "proxy.txt"
COOKIES_FILE             = "cookies.txt"
PRODUCTS_FILE            = "product.csv"
DISCORD_WEBHOOK_FILE     = "discord_webhook.txt"
DISCORD_THREAD_ID_FILE   = "discord_thread_id.txt"
DISCORD_THREAD_NAME_FILE = "discord_thread_name.txt"
PAYMENT_URLS_FILE        = "payment_urls.txt"
PAYMENT_URL_LAST_FILE    = "payment_url.txt"
GRAPHQL_URL   = "https://www.bol.com/api/graphql"
CHECKOUT_PAGE_URL = "https://www.bol.com/nl/nl/checkout/?entryPoint=BUY_NOW"
CREATE_BASKET_HASH = "sha256:92b016f96aa83a630f5cc5ebcd48d6da90e155aed1119a492e71856d99e590e0"
ADD_ITEM_HASH = "sha256:fda23bccf49694870747c1a4a5003944bca994020fc3cb05ae9c6cdf029aaa7c"
BOL_SELLER_NAME = "bol"
BOL_RETAILER_ID = "0"
WORKERS_PER_PRODUCT = 1
CHECK_DELAY_SECONDS = 3.0
PAGE_DOES_NOT_EXIST_DELAY_SECONDS = 100.0
PRODUCTS_RELOAD_SECONDS = 6.0
MAX_SUCCESSFUL_CHECKOUTS_PER_PRODUCT = 1
IDEAL_SELECTION_ATTEMPTS = 3
IDEAL_SELECTION_RETRY_DELAY_SECONDS = 0.25
IDEAL_SELECTION_SETTLE_SECONDS = 0.25
CSV_HEADERS = ("product_url", "bol_account")
CSV_LOCK = threading.Lock()
BASKET_LOCK = threading.Lock()
PURCHASE_FLOW_LOCK = threading.Lock()
PAYMENT_URLS_LOCK = threading.Lock()
BASKET_WARMUP_ATTEMPTS = 10
PRODUCT_SUCCESS_COUNTS: dict[str, int] = {}

SITEMAP_KNOWN_URLS: set[str] = set()

# Set True at startup when using a single sticky proxy (no pool rotation, no -session- username churn).
STICKY_PROXY_SINGLE_LINE = False

# Pause after a successful AddItem before checkout (calmer traffic).
try:
    AFTER_ADD_TO_CART_PAUSE = float(os.getenv("AFTER_ADD_TO_CART_PAUSE", "2.5"))
except ValueError:
    AFTER_ADD_TO_CART_PAUSE = 2.5


def _env_truthy(key: str, default: str = "1") -> bool:
    return os.getenv(key, default).strip().lower() not in ("0", "false", "no", "off")


def _sticky_proxy_mode_from_env(pool_len: int) -> bool:
    """
    STICKY_PROXY_MODE unset → on when exactly one pool line (typical single residential line + cookies).
    Set to 0 to use full proxy.txt pool; set to 1 to force first line only when multiple entries exist.
    """
    raw = os.getenv("STICKY_PROXY_MODE", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return pool_len == 1


SITEMAP_ENABLED = _env_truthy("SITEMAP_ENABLED", "1")
SITEMAP_SCAN_INTERVAL_SECS = float(os.getenv("SITEMAP_SCAN_INTERVAL_SECS", "45"))
SITEMAP_INDEX_URL = os.getenv("BOL_SITEMAP_INDEX", "https://www.bol.com/sitemap/nl-nl/")
SITEMAP_WORKERS = int(os.getenv("SITEMAP_WORKERS", "1"))
SITEMAP_DB = os.getenv("SITEMAP_DB", "bol_sitemap.sqlite3")
SITEMAP_ALL_LINKS = os.getenv("SITEMAP_ALL_LINKS", "all_links.txt")
SITEMAP_NEW_LINKS = os.getenv("SITEMAP_NEW_LINKS", "newlinks.txt")

DISCORD_DEBUG = _env_truthy("DISCORD_DEBUG", "0")
MONITOR_DELAY_ONLINE_MIN = float(os.getenv("MONITOR_DELAY_ONLINE_MIN", "2.0"))
MONITOR_DELAY_ONLINE_MAX = float(os.getenv("MONITOR_DELAY_ONLINE_MAX", "4.0"))
MONITOR_DELAY_ANY_MIN = float(os.getenv("MONITOR_DELAY_ANY_MIN", "1.5"))
MONITOR_DELAY_ANY_MAX = float(os.getenv("MONITOR_DELAY_ANY_MAX", "4.0"))
MONITOR_DELAY_OFFLINE_MIN = float(os.getenv("MONITOR_DELAY_OFFLINE_MIN", "10.0"))
MONITOR_DELAY_OFFLINE_MAX = float(os.getenv("MONITOR_DELAY_OFFLINE_MAX", "20.0"))
MONITOR_MAX_ERROR_RETRIES = int(os.getenv("MONITOR_MAX_ERROR_RETRIES", "3"))
MONITOR_OFFLINE_CONFIRM = int(os.getenv("MONITOR_OFFLINE_CONFIRM", "3"))

# After MONITOR_MAX_ERROR_RETRIES consecutive probe ERRORs, blacklist that proxy (soft block, seconds).
PROBE_ERROR_BLACKLIST_MIN = float(os.getenv("PROBE_ERROR_BLACKLIST_MIN", "600"))
PROBE_ERROR_BLACKLIST_MAX = float(os.getenv("PROBE_ERROR_BLACKLIST_MAX", "900"))

# Per-proxy spacing between HTTP calls on the same IP (human-like jitter).
PROXY_REQUEST_GAP_MIN = float(os.getenv("PROXY_REQUEST_GAP_MIN", "1.5"))
PROXY_REQUEST_GAP_MAX = float(os.getenv("PROXY_REQUEST_GAP_MAX", "4.0"))

# Rotating (non-checkout) traffic: extra pacing on top of PROXY_REQUEST_GAP_* (set 3–7 via env if needed).
ROTATING_PROXY_GAP_MIN = float(os.getenv("ROTATING_PROXY_GAP_MIN", "0.35"))
ROTATING_PROXY_GAP_MAX = float(os.getenv("ROTATING_PROXY_GAP_MAX", "1.1"))

# Append -session-<id> to proxy username on each rotating request (gateway sticky-IP workaround).
PROXY_USERNAME_SESSION_ROTATE = _env_truthy("PROXY_USERNAME_SESSION_ROTATE", "1")

# On HTTP 403 during rotating GET/POST, rotate proxy and retry (extra attempt(s)).
PROXY_403_ROTATING_RETRIES = max(1, int(os.getenv("PROXY_403_ROTATING_RETRIES", "2")))

# 403-style cooldown: random 2–5 minutes by default. Set PROXY_BLOCKED_COOLDOWN_SECS for a fixed duration.
_pb_cool = os.getenv("PROXY_BLOCKED_COOLDOWN_SECS", "").strip()
PROXY_BLOCKED_COOLDOWN_FIXED = float(_pb_cool) if _pb_cool else None
PROXY_BLOCKED_COOLDOWN_MIN = float(os.getenv("PROXY_BLOCKED_COOLDOWN_MIN", "120"))
PROXY_BLOCKED_COOLDOWN_MAX = float(os.getenv("PROXY_BLOCKED_COOLDOWN_MAX", "300"))

# Extra probe line: GET product PDP | proxy | HTTP code (optional noise control).
MONITOR_HTTP_TRACE = _env_truthy("MONITOR_HTTP_TRACE", "0")

# [DIAG] verbose HTTP/login steps — default off so monitoring stays readable (set BOL_DIAGNOSTIC_LOG=1 to debug).
BOL_DIAGNOSTIC_LOG = _env_truthy("BOL_DIAGNOSTIC_LOG", "0")
# Multi-line monitor traces ([DIAG] HTML monitor 1/5 …) — only if both DIAG + full trace on.
BOL_MONITOR_FULL_TRACE = _env_truthy("BOL_MONITOR_FULL_TRACE", "0")

# Startup login: retry across proxy lines (transient 403 / block on first hop).
LOGIN_CHECK_RETRIES = max(1, int(os.getenv("LOGIN_CHECK_RETRIES", "3")))

# HTML PDP probe: GET + parse retries (short HTML shell is normal — patience + delay, not failure).
MONITOR_HTML_PROBE_RETRIES = max(10, min(40, int(os.getenv("MONITOR_HTML_PROBE_RETRIES", "18"))))
# After login (cookies): thin / empty-parse PDPs retry the SAME proxy with delay — no rotation.
PDP_THIN_SAME_PROXY_RETRIES = max(10, min(40, int(os.getenv("PDP_THIN_SAME_PROXY_RETRIES", "18"))))
PDP_THIN_RETRY_DELAY_MIN = float(os.getenv("PDP_THIN_RETRY_DELAY_MIN", "2.0"))
PDP_THIN_RETRY_DELAY_MAX = float(os.getenv("PDP_THIN_RETRY_DELAY_MAX", "5.0"))

# Logged-in PDP: same idea as browser automation — storefront, pause 1–2s, open product, optional settle before re-read HTML.
def _monitor_product_nav_delay_sec() -> float:
    try:
        lo = float(os.getenv("MONITOR_PRODUCT_NAV_DELAY_MIN", "1.0"))
        hi = float(os.getenv("MONITOR_PRODUCT_NAV_DELAY_MAX", "2.0"))
        if hi < lo:
            lo, hi = hi, lo
        return float(random.uniform(lo, hi))
    except ValueError:
        return 1.5


def _monitor_product_load_settle_sec() -> float:
    try:
        return max(0.25, float(os.getenv("MONITOR_PRODUCT_LOAD_SETTLE_SEC", "1.5")))
    except ValueError:
        return 1.5


ST_OFFLINE = "OFFLINE"
ST_ONLINE_OOS = "ONLINE_OOS"
ST_IN_STOCK = "IN_STOCK"
ST_BLOCKED = "BLOCKED"
ST_ERROR = "ERROR"

# PDP probe returned HTTP 200 but short HTML / missing parse — normal; keep monitoring (no fatal streak).
_PDP_THIN_SHELL_DIAGS = frozenset(
    {
        "thin_soft_cycle",
        "thin_page_after_same_proxy_retries",
        "thin_page_after_retries",
        "pdp_ready_to_buy_but_no_offerUid",
        "short_html_shell_or_challenge_retry_checkout_path",
        "thin_soft_retry_band",
    }
)

UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'

_THIN_FETCH_DETAILS_ONCE: set[str] = set()


def _notify_fetching_product_details_once(product_url: str) -> None:
    """Once per URL: single user-visible line when HTML is still a short shell (retries are normal)."""
    u = (product_url or "").strip()
    if not u or u in _THIN_FETCH_DETAILS_ONCE:
        return
    _THIN_FETCH_DETAILS_ONCE.add(u)
    print("  Fetching product details…", flush=True)


def _diag(message: str, *, probe: bool = False) -> None:
    """probe=True: only printed when BOL_DIAGNOSTIC_LOG and BOL_MONITOR_FULL_TRACE (monitor noise)."""
    if not BOL_DIAGNOSTIC_LOG:
        return
    if probe and not BOL_MONITOR_FULL_TRACE:
        return
    print(f"  [DIAG] {message}", flush=True)


def _diag_probe(message: str) -> None:
    """HTML monitor / probe traces — quiet unless BOL_MONITOR_FULL_TRACE=1."""
    _diag(message, probe=True)


def _html_title_snippet(html: str, max_len: int = 96) -> str:
    raw = html or ""
    m = re.search(r"<title[^>]*>([^<]{0,220})", raw, re.IGNORECASE | re.DOTALL)
    t = (m.group(1) if m else "").strip()
    if not t:
        og = re.search(
            r'<meta[^>]+property=["\']og:title["\'][^>]*\scontent=["\']([^"\']+)["\']',
            raw[:120_000],
            re.I,
        ) or re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*\sproperty=["\']og:title["\']',
            raw[:120_000],
            re.I,
        )
        if og:
            t = og.group(1).strip()
    if not t:
        tw = re.search(
            r'<meta[^>]+name=["\']twitter:title["\'][^>]*\scontent=["\']([^"\']+)["\']',
            raw[:120_000],
            re.I,
        )
        if tw:
            t = tw.group(1).strip()
    t = re.sub(r"\s+", " ", t)
    if len(t) > max_len:
        return t[: max_len - 1] + "…"
    return t or "(no title)"


def proxy_aggressive_block_seconds() -> float:
    """Cooldown after 403 / hard block (seconds): fixed env or random 2–5 min."""
    if PROXY_BLOCKED_COOLDOWN_FIXED is not None:
        return float(PROXY_BLOCKED_COOLDOWN_FIXED)
    lo = min(PROXY_BLOCKED_COOLDOWN_MIN, PROXY_BLOCKED_COOLDOWN_MAX)
    hi = max(PROXY_BLOCKED_COOLDOWN_MIN, PROXY_BLOCKED_COOLDOWN_MAX)
    return float(random.uniform(lo, hi))


# ─────────────────────────────────────────────
#  HEADERS
# ─────────────────────────────────────────────

PAGE_HEADERS = {
    "accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "accept-language":           "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
    "accept-encoding":           "gzip, deflate, br, zstd",
    "cache-control":             "no-cache",
    "pragma":                    "no-cache",
    "sec-ch-ua":                 '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile":          "?0",
    "sec-ch-ua-platform":        '"Windows"',
    "sec-fetch-dest":            "document",
    "sec-fetch-mode":            "navigate",
    "sec-fetch-site":            "none",      # correct for first navigation
    "sec-fetch-user":            "?1",
    "upgrade-insecure-requests": "1",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
}

# Headers for page navigations that have a same-origin referer
PAGE_HEADERS_SAME_ORIGIN = {
    **PAGE_HEADERS,
    "sec-fetch-site": "same-origin",
}

GQL_HEADERS = {
    "accept":             "application/graphql-response+json, application/graphql+json, application/json, text/event-stream",
    "accept-encoding":    "gzip, deflate, br, zstd",
    "accept-language":    "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
    "cache-control":      "no-cache",
    "content-type":       "application/json",
    "dnt":                "1",
    "origin":             "https://www.bol.com",
    "pragma":             "no-cache",
    "priority":           "u=1, i",
    "sec-ch-ua":          '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile":   "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest":     "empty",
    "sec-fetch-mode":     "cors",
    "sec-fetch-site":     "same-origin",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
}

JSON_HEADERS = {
    **GQL_HEADERS,
    "accept":       "application/json, text/plain, */*",
    "content-type": "application/json",
}

FORM_HEADERS = {
    **GQL_HEADERS,
    "accept":           "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "content-type":     "application/x-www-form-urlencoded",
    "sec-fetch-dest":   "document",
    "sec-fetch-mode":   "navigate",
}


# ─────────────────────────────────────────────
#  PROXY
# ─────────────────────────────────────────────

try:
    HTTP_SESSION_TIMEOUT = max(5, int(os.getenv("HTTP_SESSION_TIMEOUT", "25")))
except ValueError:
    HTTP_SESSION_TIMEOUT = 25


class RotateProxy(Exception):
    pass


class CheckoutFatal(BaseException):
    pass


def _scrub_proxy_field(raw: str) -> str:
    """Remove pasted template junk (e.g. Jinja {% comment %}) from host/user/pass fields."""
    s = (raw or "").strip()
    s = re.sub(r"\{%[\s\S]*?%\}\s*", "", s)
    return s.strip()


def _scrub_proxy_txt_line_raw(line: str) -> str:
    """Strip Jinja/HTML comment wrappers from a whole proxy.txt line before host:port:user:pass split."""
    s = (line or "").strip()
    if not s or s.startswith("#"):
        return ""
    s = re.sub(r"\{%[\s\S]*?%\}\s*", "", s)
    s = re.sub(r"<!--[\s\S]*?-->\s*", "", s)
    return s.strip()


def _proxy_line_to_url(host: str, port: str, user: str, password: str) -> str | None:
    host = _scrub_proxy_field(host)
    port = _scrub_proxy_field(port)
    user = _scrub_proxy_field(user)
    password = _scrub_proxy_field(password)
    if not host or not port or not user:
        return None
    if any(x in host + port + user + password for x in ("{%", "%}", "<%", "%>", "{{", "}}")):
        return None
    if " " in host or "\n" in host or "\r" in host:
        return None
    try:
        int(port)
    except ValueError:
        return None
    return f"http://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"


def load_proxies(filename: str = PROXY_FILE) -> list:
    if not os.path.exists(filename):
        raise FileNotFoundError(f"{filename} not found.")
    proxies = []
    line_no = 0
    with open(filename, "r", encoding="utf-8") as f:
        for line in f:
            line_no += 1
            line = _scrub_proxy_txt_line_raw(line)
            if not line or line.startswith("#"):
                continue
            parts = line.split(":")
            if len(parts) != 4:
                print(f"  [!] Bad proxy format (need host:port:user:pass) line {line_no}: {line[:80]}")
                continue
            host, port, user, password = parts
            url = _proxy_line_to_url(host, port, user, password)
            if not url:
                print(
                    f"  [!] Skipped proxy line {line_no} (invalid host or pasted HTML/template in field). "
                    f"Fix proxy.txt — host must be plain hostname, e.g. residential.example.com:5000",
                    flush=True,
                )
                continue
            proxies.append(url)
    if not proxies:
        raise ValueError(f"No valid proxies in {filename}")
    return proxies


# ─────────────────────────────────────────────
#  COOKIES
# ─────────────────────────────────────────────

def load_cookies(filepath: str) -> dict:
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"{filepath} not found.")
    with open(filepath, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, list):
        raise ValueError("cookies.txt must be a JSON array.")
    return {c["name"]: c["value"] for c in raw if "name" in c and "value" in c}


# Single pool entry when USE_PROXIES=0 (direct connection; no proxy.txt).
DIRECT_PROXY = "__DIRECT__"


def resolve_proxies_and_cookies_for_start() -> tuple[list[str], dict, bool]:
    """
    USE_COOKIES / USE_PROXIES from environment (GUI passes 0/1). Defaults: both on.
    Returns (proxies, cookies, sticky_single_line).
    """
    global STICKY_PROXY_SINGLE_LINE

    if _env_truthy("USE_COOKIES", "1"):
        try:
            cookies = load_cookies(os.path.join(os.getcwd(), COOKIES_FILE))
        except (FileNotFoundError, ValueError) as e:
            print(f"Error: {e}")
            sys.exit(1)
    else:
        cookies = {}
        print(
            "  [CONFIG] USE_COOKIES=0 — not loading cookies.txt (anonymous session; cart/login usually fail).",
            flush=True,
        )

    if _env_truthy("USE_PROXIES", "1"):
        try:
            proxies = load_proxies(os.path.join(os.getcwd(), PROXY_FILE))
        except (FileNotFoundError, ValueError) as e:
            print(f"Error: {e}")
            sys.exit(1)
        sticky = _sticky_proxy_mode_from_env(len(proxies))
        if sticky and len(proxies) > 1:
            print(
                "  [CONFIG] Sticky proxy mode: using first line in proxy.txt only; rotation disabled.",
                flush=True,
            )
            proxies = [proxies[0]]
        STICKY_PROXY_SINGLE_LINE = bool(sticky)
        if STICKY_PROXY_SINGLE_LINE:
            print(
                "  [CONFIG] Single sticky proxy: no pool rotation; calmer request spacing.",
                flush=True,
            )
        return proxies, cookies, STICKY_PROXY_SINGLE_LINE

    proxies = [DIRECT_PROXY]
    print("  [CONFIG] USE_PROXIES=0 — direct connection (no proxy pool).", flush=True)
    STICKY_PROXY_SINGLE_LINE = False
    return proxies, cookies, False


def _diag_startup_banner(proxies: list[str], cookies: dict, sticky: bool) -> None:
    _diag(
        f"run {time.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()} "
        f"USE_PROXIES={_env_truthy('USE_PROXIES', '1')} USE_COOKIES={_env_truthy('USE_COOKIES', '1')} "
        f"pool_lines={len(proxies)} sticky={sticky} cookie_names={len(cookies)} "
        f"stable_sessions_with_cookies={bool(cookies)}"
    )


def _use_session_pool_flag() -> bool | None:
    """None = auto: use sessions/ when it contains at least one cookies.txt."""
    raw = os.getenv("USE_SESSION_POOL", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return None


def _auto_session_slots_flag() -> bool:
    """Create sessions/slot_XX from proxy.txt + template cookies (multi-session)."""
    return _env_truthy("AUTO_SESSION_SLOTS", "0")


def _sessions_dir_has_cookie_slots(cwd: str) -> bool:
    if SESSIONS is None:
        return False
    d = os.path.join(cwd, "sessions")
    if not os.path.isdir(d):
        return False
    for name in os.listdir(d):
        if os.path.isfile(os.path.join(d, name, "cookies.txt")):
            return True
    return False


def _init_session_pool_factory() -> None:
    if SESSIONS is None:
        return
    SESSIONS.load_env_file()
    SESSIONS.set_proxy_manager_factory(
        lambda proxies, cookies, st: ProxyManager(proxies, cookies, sticky_single=st)
    )
    SESSIONS.set_login_recovery(attempt_bol_password_login_for_slot)


def _bootstrap_network_clients(cwd: str):
    """
    Returns (primary_pm, session_pool_or_none).
    With a pool, primary_pm is the first slot's ProxyManager (shared sitemap / banner only).
    """
    _init_session_pool_factory()
    flag = _use_session_pool_flag()
    auto_slots = _auto_session_slots_flag()
    want_pool = flag is True or (flag is None and (_sessions_dir_has_cookie_slots(cwd) or auto_slots))

    if SESSIONS is not None and want_pool:
        if flag is True or auto_slots:
            SESSIONS.ensure_multi_session_layout(
                cwd,
                os.path.join(cwd, PROXY_FILE),
                os.path.join(cwd, COOKIES_FILE),
            )
        pool = SESSIONS.SessionPool.load_from_sessions_dir(
            cwd,
            os.path.join(cwd, PROXY_FILE),
            os.path.join(cwd, COOKIES_FILE),
        )
        if pool and len(pool.slots) > 0:
            global STICKY_PROXY_SINGLE_LINE
            STICKY_PROXY_SINGLE_LINE = True
            return pool.slots[0].pm, pool
        if flag is True:
            print(
                "  [SESSION] USE_SESSION_POOL=1 but no loadable sessions/*/cookies.txt — "
                "falling back to legacy cookies.txt + proxy.txt.",
                flush=True,
            )

    proxies, cookies, sticky = resolve_proxies_and_cookies_for_start()
    pm = ProxyManager(proxies, cookies, sticky_single=sticky)
    return pm, None


def _warm_pm(pm, warm_tries: int) -> None:
    warmed = False
    for attempt in range(warm_tries):
        try:
            _diag(f"warm-up GET bol.com homepage try {attempt + 1}/{warm_tries}")
            r = pm.get("https://www.bol.com/nl/nl/", headers=PAGE_HEADERS)
            _diag(f"warm-up OK HTTP={r.status_code}")
            warmed = True
            break
        except RotateProxy as e:
            _diag(f"warm-up RotateProxy: {e}")
            print(f"  Warm-up try {attempt + 1} blocked ({e}), backing off...")
            pm.rotate()
        except Exception as e:
            _diag(f"warm-up error {type(e).__name__}: {e}")
            print(f"  Warm-up try {attempt + 1} error ({e}), backing off...")
            pm.rotate()
    if not warmed:
        print("  WARNING: All proxies blocked on warm-up -- continuing anyway")
        _diag("warm-up: no successful hop — login may fail; check proxies or USE_PROXIES=0 for local test")


def _diag_startup_clients(pm, session_pool: object | None) -> None:
    if session_pool is not None:
        s0 = session_pool.slots[0]
        _diag_startup_banner([s0.proxy_url], s0.cookies, True)
    else:
        _diag_startup_banner(pm.proxies, pm.cookies, getattr(pm, "_sticky_single", False))


def _startup_login_pool_or_legacy(
    pm,
    session_pool: object | None,
    warm_tries: int,
) -> None:
    if session_pool is not None and SESSIONS is not None:
        slots = getattr(session_pool, "slots", []) or []
        any_ok = False
        for slot in slots:
            _warm_pm(slot.pm, warm_tries)
            email = check_login_with_retries(slot.pm, session_slot=slot)
            if not email and os.getenv("BOL_EMAIL", "").strip() and os.getenv("BOL_PASSWORD", "").strip():
                print(f"  [SESSION {slot.slot_id}] Trying .env password login…", flush=True)
                if slot.try_recover():
                    email = check_login_with_retries(slot.pm, session_slot=slot)
            if email:
                slot.on_login_ok(email)
                print(f"  [SESSION {slot.slot_id}] Logged in as: {email}", flush=True)
                any_ok = True
            elif _env_truthy("USE_COOKIES", "1"):
                print(
                    f"  [SESSION {slot.slot_id}] Login check failed — "
                    f"refresh cookies in {slot.cookies_path} or fix proxy for this slot.",
                    flush=True,
                )
                slot.on_login_redirect()
        if not any_ok and _env_truthy("USE_COOKIES", "1"):
            print("ERROR: No session slot passed login check.", flush=True)
            sys.exit(1)
        return

    _warm_pm(pm, warm_tries)
    email = check_login_with_retries(pm, session_slot=None)
    if email:
        print(f"Logged in as: {email}")
    elif _env_truthy("USE_COOKIES", "1"):
        _diag("startup stopping: login check failed after retries (see [DIAG] lines above).")
        print("ERROR: Login check failed -- cookies are missing or not logged in on bol.com.")
        print(f"Export a fresh {COOKIES_FILE} from your browser while logged in, then restart the bot.")
        sys.exit(1)
    else:
        print(
            "  [CONFIG] USE_COOKIES=0 — skipping login check (no session cookies).",
            flush=True,
        )


# ─────────────────────────────────────────────
#  SESSION
# ─────────────────────────────────────────────

def _format_proxy_netloc(username: str, password: str, hostname: str, port: int | None) -> str:
    """Rebuild userinfo@host:port for http(s) proxy URLs (username/password safely quoted)."""
    auth = quote(username, safe="")
    if password:
        auth += ":" + quote(password, safe="")
    hostpart = hostname
    if port:
        hostpart = f"{hostname}:{int(port)}"
    return f"{auth}@{hostpart}"


def inject_rotating_proxy_session(proxy_url: str) -> str:
    """
    Many residential pools use one gateway host:port; providers accept '-session-<n>' on the
    username to pin a fresh exit IP. Appends a new session id every call (strips prior suffix).
    """
    if STICKY_PROXY_SINGLE_LINE or not PROXY_USERNAME_SESSION_ROTATE:
        return proxy_url
    raw = (proxy_url or "").strip()
    if raw == DIRECT_PROXY:
        return DIRECT_PROXY
    if not raw:
        return raw
    p = urlparse(raw)
    if p.scheme not in ("http", "https") or not p.hostname:
        return raw
    user = unquote(p.username) if p.username else ""
    if not user:
        return raw
    pw = unquote(p.password) if p.password else ""
    base_user = re.sub(r"-session-[A-Za-z0-9_-]+$", "", user, flags=re.IGNORECASE)
    sid = random.randint(1, 999_999_999)
    new_user = f"{base_user}-session-{sid}"
    netloc = _format_proxy_netloc(new_user, pw, p.hostname, p.port)
    return urlunparse((p.scheme, netloc, p.path or "", p.params, p.query, p.fragment))


def build_session(cookies: dict, proxy_url: str | None = None, timeout: int = 5) -> Session:
    """TLS fingerprint via curl_cffi; keep in sync with PAGE_HEADERS Chrome version (131)."""
    prefer = (os.getenv("BOL_CURL_IMPERSONATE") or "").strip().lower()
    chain = []
    if prefer:
        chain.append(prefer)
    for imp in ("chrome131", "chrome124", "chrome120"):
        if imp not in chain:
            chain.append(imp)
    last_err: BaseException | None = None
    for impersonate in chain:
        try:
            kwargs = dict(
                impersonate=impersonate,
                allow_redirects=True,
                timeout=timeout,
                headers=PAGE_HEADERS,
                cookies=cookies,
            )
            if proxy_url and proxy_url != DIRECT_PROXY:
                kwargs["proxies"] = {"http": proxy_url, "https": proxy_url}
            return Session(**kwargs)
        except Exception as e:
            last_err = e
    raise RuntimeError(f"curl_cffi Session failed (tried {chain!r}): {last_err}") from last_err


# ─────────────────────────────────────────────
#  PROXY MANAGER
# ─────────────────────────────────────────────

class ProxyManager:
    """
    Manages a pool of proxies with per-proxy cooldowns and light usage tracking.

    Key design: each thread gets its own dedicated Session via get_thread_session().
    Sessions are NOT shared between threads -- this prevents thread [1]'s rotation
    from killing thread [2]'s active connection mid-request.

    Connection reuse: curl_cffi Sessions keep TCP connections alive (HTTP keep-alive)
    so consecutive requests to the same host reuse the same socket. This means the
    product page fetch and the add-to-cart POST both reuse the same connection --
    no extra handshake between them, which is the fastest possible path.

    With non-empty cookies (logged-in export): rotating traffic reuses one Session per
    pool proxy line (no per-request -session- username mutation) and merges Set-Cookie
    into the shared cookie dict so Bol login stays valid across requests.
    """
    COOLDOWN_SECS = 60

    def __init__(self, proxies: list, cookies: dict, *, sticky_single: bool = False):
        self.proxies       = proxies
        self.cookies       = cookies
        self._sticky_single = bool(sticky_single)
        # Logged-in browser export: keep proxy username as in proxy.txt and reuse Sessions so
        # Bol + cookies stay aligned (no per-request -session- spin on the same gateway line).
        self._logged_in_cookies = bool(cookies)
        self.lock          = threading.Lock()
        self.blocked_until = {}              # proxy_url -> float timestamp
        self._proxy_meta   = {}              # failure_count, last_used, last_request_end, block_tier
        self._global_index = 0              # for warm-up / login check
        self._rr_index = 0                  # round-robin for rotating requests
        self._last_proxy_by_host: dict[str, str] = {}  # avoid same proxy twice in a row per host
        # Thread-local storage:
        # - current proxy assignment for the thread
        # - per-thread sessions keyed by proxy
        self._thread_local = threading.local()

    def _host(self, proxy_url: str) -> str:
        m = re.search(r'@(.+)$', proxy_url)
        return m.group(1) if m else proxy_url

    def proxy_label_for_log(self, proxy_url: str) -> str:
        """
        Host:port alone is misleading — many pool lines share one gateway (different user/pass).
        Include pool index so logs show rotation across lines.
        """
        if proxy_url == DIRECT_PROXY:
            return "direct (no proxy)"
        host = self._host(proxy_url)
        try:
            idx = self.proxies.index(proxy_url) + 1
        except ValueError:
            return host
        return f"#{idx}/{len(self.proxies)} {host}"

    def _meta(self, proxy_url: str) -> dict:
        return self._proxy_meta.setdefault(
            proxy_url,
            {
                "failure_count": 0,
                "last_used": 0.0,
                "last_request_end": 0.0,
                "block_tier": None,
            },
        )

    def _clear_block_tier_if_expired(self, proxy_url: str) -> None:
        if time.time() >= float(self.blocked_until.get(proxy_url, 0)):
            self._meta(proxy_url).pop("block_tier", None)

    def proxy_lifecycle_status(self, proxy_url: str) -> str:
        """ACTIVE (usable), COOLDOWN (short soft block), BLOCKED (403-style hard cooldown)."""
        with self.lock:
            self._clear_block_tier_if_expired(proxy_url)
            if time.time() >= float(self.blocked_until.get(proxy_url, 0)):
                return "ACTIVE"
            tier = self._meta(proxy_url).get("block_tier") or "soft"
            return "BLOCKED" if tier == "hard" else "COOLDOWN"

    def proxy_failure_count(self, proxy_url: str) -> int:
        with self.lock:
            return int(self._meta(proxy_url).get("failure_count", 0))

    def _throttle_before_request(self, proxy_url: str, *, rotating: bool = False) -> None:
        """Space out HTTP traffic per proxy (random gap since last request ended)."""
        if self._sticky_single:
            lo = float(os.getenv("STICKY_GAP_MIN", "2.5"))
            hi = float(os.getenv("STICKY_GAP_MAX", "5.5"))
            if hi < lo:
                lo, hi = hi, lo
        elif rotating:
            lo = min(ROTATING_PROXY_GAP_MIN, ROTATING_PROXY_GAP_MAX)
            hi = max(ROTATING_PROXY_GAP_MIN, ROTATING_PROXY_GAP_MAX)
        else:
            lo = min(PROXY_REQUEST_GAP_MIN, PROXY_REQUEST_GAP_MAX)
            hi = max(PROXY_REQUEST_GAP_MIN, PROXY_REQUEST_GAP_MAX)
        gap = float(random.uniform(lo, hi))
        with self.lock:
            last_end = float(self._meta(proxy_url).get("last_request_end", 0.0))
            now = time.time()
            wait_s = max(0.0, last_end + gap - now)
        if wait_s > 0:
            time.sleep(wait_s)

    def _mark_request_done(self, proxy_url: str) -> None:
        with self.lock:
            now = time.time()
            m = self._meta(proxy_url)
            m["last_request_end"] = now
            m["last_used"] = now

    def _mark_blocked(self, proxy_url: str, block_seconds: float, *, block_tier: str = "soft"):
        self.blocked_until[proxy_url] = time.time() + float(block_seconds)
        self._meta(proxy_url)["block_tier"] = block_tier

    def _is_blocked(self, proxy_url: str) -> bool:
        return time.time() < self.blocked_until.get(proxy_url, 0)

    def _next_available(self, after_index: int) -> tuple[int, str]:
        """Pick least-recently-used proxy that is not in cooldown (stable, not round-robin)."""
        now = time.time()
        n = len(self.proxies)
        candidates: list[tuple[int, str]] = []
        for i in range(n):
            p = self.proxies[i]
            self._clear_block_tier_if_expired(p)
            if now >= self.blocked_until.get(p, 0):
                candidates.append((i, p))
        if not candidates:
            idx = min(range(n), key=lambda j: self.blocked_until.get(self.proxies[j], 0))
            return idx, self.proxies[idx]
        candidates.sort(
            key=lambda ip: (self._meta(ip[1])["last_used"], ip[0]),
        )
        return candidates[0][0], candidates[0][1]

    # ── Per-thread proxy assignment ─────────────────────────────

    def get_thread_proxy(self) -> str:
        """Returns this thread's current proxy, assigning one if not yet set."""
        if not hasattr(self._thread_local, "proxy"):
            with self.lock:
                idx, proxy = self._next_available(self._global_index)
                self._global_index = idx
            self._thread_local.proxy = proxy
        return self._thread_local.proxy

    @staticmethod
    def _host_key_for_url(url: str) -> str:
        try:
            u = url if "://" in url else f"https://{url}"
            host = (urlparse(u).netloc or "default").lower().split(":")[0]
            return host or "default"
        except Exception:
            return "default"

    def pick_rotating_proxy_for_request(self, url: str) -> str:
        """
        Round-robin among unblocked proxies; never pick the same proxy consecutively for this host
        when the pool has more than one usable line.
        """
        if self._sticky_single and self.proxies:
            chosen = self.proxies[0]
            self._thread_local.proxy = chosen
            return chosen
        host = self._host_key_for_url(url)
        chosen: str
        with self.lock:
            n = len(self.proxies)
            now = time.time()
            available: list[str] = []
            for p in self.proxies:
                self._clear_block_tier_if_expired(p)
                if now >= float(self.blocked_until.get(p, 0)):
                    available.append(p)
            if not available:
                idx = min(range(n), key=lambda j: self.blocked_until.get(self.proxies[j], 0))
                chosen = self.proxies[idx]
            else:
                last = self._last_proxy_by_host.get(host)
                chosen = available[0]
                if len(available) > 1 and last is not None:
                    for step in range(len(available)):
                        cand = available[(self._rr_index + step) % len(available)]
                        if cand != last:
                            chosen = cand
                            break
                try:
                    ci = self.proxies.index(chosen)
                except ValueError:
                    ci = 0
                self._rr_index = (ci + 1) % max(n, 1)
                self._last_proxy_by_host[host] = chosen
        self._thread_local.proxy = chosen
        return chosen

    def rotate(
        self,
        block_seconds: float | None = None,
        *,
        aggressive_block: bool = False,
        count_failure: bool = True,
    ):
        """
        Cooldown current proxy and move this thread to another.
        aggressive_block=True → hard BLOCKED tier (random 2–5 min by default, env-tunable).
        """
        if self._sticky_single:
            delay = float(os.getenv("STICKY_ROTATE_BACKOFF_SEC", "4"))
            delay = min(max(delay, 1.0), 30.0)
            _diag(f"sticky proxy: rotate() → short backoff {delay:.1f}s (no pool switch)")
            time.sleep(delay)
            return
        current = getattr(self._thread_local, "proxy", self.proxies[0])
        with self.lock:
            if aggressive_block:
                dur = proxy_aggressive_block_seconds()
                tier = "hard"
            elif block_seconds is not None:
                dur = float(block_seconds)
                tier = "soft"
            else:
                dur = float(self.COOLDOWN_SECS)
                tier = "soft"
            self._mark_blocked(current, dur, block_tier=tier)
            if count_failure:
                self._meta(current)["failure_count"] += 1
            current_idx = self.proxies.index(current) if current in self.proxies else 0
            _, new_proxy = self._next_available(current_idx)
            if new_proxy == current and len(self.proxies) > 1:
                now_t = time.time()
                others = [
                    p
                    for p in self.proxies
                    if p != current and now_t >= float(self.blocked_until.get(p, 0))
                ]
                if others:
                    others.sort(key=lambda p: self._meta(p)["last_used"])
                    new_proxy = others[0]
        self._thread_local.proxy = new_proxy

    def _get_session(self, proxy: str, checkout: bool = False) -> Session:
        sessions = getattr(self._thread_local, "sessions", None)
        if sessions is None:
            sessions = {}
            self._thread_local.sessions = sessions
        if proxy not in sessions:
            sessions[proxy] = build_session(self.cookies, proxy, timeout=HTTP_SESSION_TIMEOUT)
        return sessions[proxy]

    def _rotating_effective_proxy(self, pool_proxy: str) -> str:
        if self._logged_in_cookies:
            return pool_proxy
        return inject_rotating_proxy_session(pool_proxy)

    def _session_for_request(self, effective_proxy: str, checkout: bool) -> Session:
        """
        Checkout + non-rotating: reuse thread-local Session per pool proxy.
        Anonymous + PROXY_USERNAME_SESSION_ROTATE: ephemeral Session per call (-session- on user).
        Logged-in cookies: always reuse cached Session per pool line (stable with cookie merge).
        """
        if (
            checkout
            or not PROXY_USERNAME_SESSION_ROTATE
            or self._logged_in_cookies
        ):
            return self._get_session(effective_proxy, checkout)
        return build_session(self.cookies, effective_proxy, timeout=HTTP_SESSION_TIMEOUT)

    def _merge_cookies_from_session(self, session: Session) -> None:
        """
        Bol refreshes XSRF / session / rl_* cookies on many responses. Ephemeral rotating Sessions
        would otherwise drop those updates so the next request still used the stale cookies.txt
        snapshot — sessions looked \"expired\" within minutes.
        """
        try:
            jar = getattr(session, "cookies", None)
            if jar is None:
                return
            updates: dict[str, str] = {}
            for c in jar:
                name = getattr(c, "name", None)
                if not name:
                    continue
                dom = (getattr(c, "domain", None) or "").lstrip(".").lower()
                if dom and "bol.com" not in dom:
                    continue
                val = getattr(c, "value", None)
                if val is not None:
                    updates[str(name)] = str(val)
            if not updates:
                return
            with self.lock:
                self.cookies.update(updates)
            if BOL_DIAGNOSTIC_LOG:
                notable = [
                    k
                    for k in updates
                    if any(
                        x in k.upper()
                        for x in ("XSRF", "SESSION", "SID", "TOKEN", "RL_", "SECURE", "AUTH")
                    )
                ]
                if notable:
                    _diag(f"cookies updated from HTTP response: {', '.join(sorted(notable)[:12])}")
        except Exception:
            pass

    @staticmethod
    def _rotating_block_retryable(exc: BaseException) -> bool:
        s = str(exc)
        return "403" in s or "503" in s

    def _summarize_request(self, method: str, url: str, kw: dict) -> str:
        label = url
        payload = kw.get("json")
        if isinstance(payload, dict):
            operation = payload.get("operationName")
            if operation:
                label = f"{url} op={operation}"
        return f"{method} {label}"

    def _log_request(self, method: str, proxy: str, checkout: bool, url: str, kw: dict) -> None:
        return

    # ── HTTP methods ────────────────────────────────────────────

    def _check(self, resp):
        if resp.status_code == 429:
            raise RotateProxy("429 rate-limited")
        if resp.status_code in (403, 503):
            raise RotateProxy(f"Blocked ({resp.status_code})")

    def get(self, url, checkout=False, raise_for_status: bool = True, **kw):
        max_attempts = PROXY_403_ROTATING_RETRIES if not checkout else 1
        for attempt in range(max_attempts):
            if checkout:
                proxy = self.get_thread_proxy()
                effective = proxy
            else:
                proxy = self.pick_rotating_proxy_for_request(url)
                effective = self._rotating_effective_proxy(proxy)
            self._throttle_before_request(proxy, rotating=not checkout)
            try:
                self._log_request("GET", proxy, checkout, url, kw)
                sess = self._session_for_request(effective, checkout)
                r = sess.get(url, **kw)
                self._merge_cookies_from_session(sess)
                if raise_for_status:
                    try:
                        self._check(r)
                    except RotateProxy as e:
                        self._mark_request_done(proxy)
                        if (
                            not checkout
                            and attempt + 1 < max_attempts
                            and self._rotating_block_retryable(e)
                        ):
                            self.rotate(
                                block_seconds=0.0,
                                aggressive_block=False,
                                count_failure=False,
                            )
                            continue
                        raise
                self._mark_request_done(proxy)
                return r
            except RotateProxy as e:
                self._mark_request_done(proxy)
                if (
                    not checkout
                    and attempt + 1 < max_attempts
                    and self._rotating_block_retryable(e)
                ):
                    self.rotate(
                        block_seconds=0.0,
                        aggressive_block=False,
                        count_failure=False,
                    )
                    continue
                raise
            except Exception as e:
                self._mark_request_done(proxy)
                raise RotateProxy(f"GET failed: {e}")
        raise RotateProxy("GET failed after retries")

    def post(self, url, checkout=False, raise_for_status: bool = True, **kw):
        max_attempts = PROXY_403_ROTATING_RETRIES if not checkout else 1
        for attempt in range(max_attempts):
            if checkout:
                proxy = self.get_thread_proxy()
                effective = proxy
            else:
                proxy = self.pick_rotating_proxy_for_request(url)
                effective = self._rotating_effective_proxy(proxy)
            self._throttle_before_request(proxy, rotating=not checkout)
            try:
                self._log_request("POST", proxy, checkout, url, kw)
                sess = self._session_for_request(effective, checkout)
                r = sess.post(url, **kw)
                self._merge_cookies_from_session(sess)
                if raise_for_status:
                    try:
                        self._check(r)
                    except RotateProxy as e:
                        self._mark_request_done(proxy)
                        if (
                            not checkout
                            and attempt + 1 < max_attempts
                            and self._rotating_block_retryable(e)
                        ):
                            self.rotate(
                                block_seconds=0.0,
                                aggressive_block=False,
                                count_failure=False,
                            )
                            continue
                        raise
                self._mark_request_done(proxy)
                return r
            except RotateProxy as e:
                self._mark_request_done(proxy)
                if (
                    not checkout
                    and attempt + 1 < max_attempts
                    and self._rotating_block_retryable(e)
                ):
                    self.rotate(
                        block_seconds=0.0,
                        aggressive_block=False,
                        count_failure=False,
                    )
                    continue
                raise
            except Exception as e:
                self._mark_request_done(proxy)
                raise RotateProxy(f"POST failed: {e}")
        raise RotateProxy("POST failed after retries")

    # ── Checkout session direct access ──────────────────────────
    @property
    def checkout_session(self) -> Session:
        """Direct session access needed by get_ideal_url (allow_redirects=False)."""
        proxy = self.get_thread_proxy()
        return self._get_session(proxy)

    # ── Warm-up / login (main thread, not per-thread) ───────────
    @property
    def session(self) -> Session:
        """Current thread's session on its currently assigned proxy."""
        proxy = self.get_thread_proxy()
        return self._get_session(proxy)

# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────

def extract_product_id(url: str) -> str | None:
    m = re.search(r'/p/[^/]+/(\d{10,})', url)
    return m.group(1) if m else None


def _extract_dehydrated(html: str, key: str) -> str | None:
    """Extracts a value from Remix's double-escaped dehydrated SSR state."""
    m = re.search(r'\\\"' + re.escape(key) + r'\\\"[,\s]*\\\"([^\\\"]+)\\\"', html)
    if m:
        return m.group(1)
    m = re.search(r'"' + re.escape(key) + r'"\s*[,:]\s*"([^"]+)"', html)
    return m.group(1) if m else None


# ─────────────────────────────────────────────
#  DISCORD
# ─────────────────────────────────────────────

def _product_csv_url_field(fieldnames: list[str] | None) -> str:
    normalized = {
        name.strip().lstrip("\ufeff").lower(): name
        for name in (fieldnames or [])
        if name
    }
    url_field = normalized.get("product_url")
    if not url_field:
        raise ValueError(f"{PRODUCTS_FILE} must have a 'product_url' header.")
    return url_field


def _product_csv_account_field_optional(fieldnames: list[str] | None) -> str | None:
    normalized = {
        name.strip().lstrip("\ufeff").lower(): name
        for name in (fieldnames or [])
        if name
    }
    for key in ("bol_account", "account", "bol_email_slot"):
        k = normalized.get(key)
        if k:
            return k
    return None


def _parse_bol_account_cell(acc_raw: str, row_index: int) -> int:
    """CSV bol_account: 1–3 maps to Email1…Email3. Default follows row order (capped at 3)."""
    dft = min(3, max(1, row_index + 1))
    s = (acc_raw or "").strip()
    if not s:
        return dft
    try:
        n = int(float(s))
    except ValueError:
        return dft
    return min(3, max(1, n))


def _session_pool_slot_index(product: dict, *, fallback_row_index: int, pool_len: int) -> int:
    """Map CSV bol_account to SessionPool.slots index (0-based); clamp if pool smaller than account id."""
    if pool_len <= 0:
        return 0
    acc = _parse_bol_account_cell(str(product.get("bol_account") or ""), fallback_row_index)
    idx = acc - 1
    idx = min(max(idx, 0), pool_len - 1)
    if acc > pool_len:
        print(
            f"  [SESSION] bol_account={acc} exceeds session pool size ({pool_len}) — "
            f"using index {idx} (same as browser: align pool rows with accounts).",
            flush=True,
        )
    return idx


def _http_urls_from_loose_product_csv(raw: str) -> list[str]:
    """Lines that look like product URLs when the file has no product_url header row."""
    seen: set[str] = set()
    out: list[str] = []
    for line in raw.splitlines():
        u = line.strip().strip('"').strip()
        if u.lower().startswith(("http://", "https://")) and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _write_products_csv_rows(filename: str, rows: list[tuple[str, int]]) -> None:
    with open(filename, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        writer.writeheader()
        for url, acc in rows:
            writer.writerow({"product_url": url, "bol_account": str(int(acc))})


def ensure_products_csv(filename: str = PRODUCTS_FILE) -> None:
    legacy_filename = os.path.join(os.path.dirname(filename), "products.txt")
    if os.path.exists(filename):
        try:
            with open(filename, "r", encoding="utf-8-sig", errors="replace") as f:
                raw = f.read()
        except OSError:
            return
        reader = csv.DictReader(io.StringIO(raw))
        fieldnames = reader.fieldnames or []
        url_field = None
        if fieldnames:
            try:
                url_field = _product_csv_url_field(fieldnames)
            except ValueError:
                url_field = None
        if url_field:
            account_field = _product_csv_account_field_optional(fieldnames)
            rows_out: list[tuple[str, int]] = []
            for row_index, row in enumerate(reader):
                url = (row.get(url_field) or "").strip()
                if not url:
                    continue
                acc_cell = (
                    (row.get(account_field) or "").strip()
                    if account_field
                    else ""
                )
                rows_out.append((url, _parse_bol_account_cell(acc_cell, row_index)))
            fn_tuple = tuple(fieldnames)
            need_norm = fn_tuple != CSV_HEADERS or account_field is None
            if need_norm:
                _write_products_csv_rows(filename, rows_out)
                print(f"Normalized {PRODUCTS_FILE} to headers: product_url, bol_account")
            return

        loose = _http_urls_from_loose_product_csv(raw)
        if loose:
            paired = [(u, min(3, i + 1)) for i, u in enumerate(loose)]
            _write_products_csv_rows(filename, paired)
            print(
                f"Normalized {PRODUCTS_FILE}: wrote {len(loose)} URL(s) with product_url + bol_account "
                "(file had URLs but no CSV header row).",
            )
            return

        _write_products_csv_rows(filename, [])
        print(f"Reset empty {PRODUCTS_FILE} with headers: product_url, bol_account")
        return

    urls = []
    if os.path.exists(legacy_filename):
        with open(legacy_filename, "r", encoding="utf-8") as f:
            urls = [line.strip() for line in f if line.strip().startswith("http")]

    legacy_rows = [(u, min(3, i + 1)) for i, u in enumerate(urls)]
    _write_products_csv_rows(filename, legacy_rows)
    if urls:
        print(f"Migrated {len(urls)} product(s) from products.txt to {PRODUCTS_FILE}.")
    else:
        print(f"Created empty {PRODUCTS_FILE} with headers: product_url, bol_account")


def load_products(filename: str = PRODUCTS_FILE) -> list[dict]:
    if not os.path.exists(filename):
        raise FileNotFoundError(f"{filename} not found.")

    with open(filename, "r", encoding="utf-8-sig", errors="replace") as f:
        raw = f.read()
    reader = csv.DictReader(io.StringIO(raw))
    try:
        url_field = _product_csv_url_field(reader.fieldnames)
    except ValueError:
        url_rows = _http_urls_from_loose_product_csv(raw)
        products: list[dict] = []
        seen_urls: set[str] = set()
        for i, url in enumerate(url_rows):
            if url in seen_urls:
                print(f"  [!] Skipping duplicate product URL: {url}")
                continue
            seen_urls.add(url)
            products.append({
                "product_url": url,
                "bol_account": min(3, i + 1),
                "offer_uid": None,
                "last_checked_offer_uid": None,
                "last_checked_seller_name": None,
                "last_checked_seller_id": None,
                "last_checked_is_bol": None,
                "last_skipped_offer_uid": None,
            })
        return products

    account_field = _product_csv_account_field_optional(reader.fieldnames)
    products = []
    seen_urls = set()
    for row_index, row in enumerate(reader):
        url = (row.get(url_field) or "").strip()
        if not url:
            continue
        if not url.startswith("http"):
            print(f"  [!] Skipping invalid product URL: {url}")
            continue
        if url in seen_urls:
            print(f"  [!] Skipping duplicate product URL: {url}")
            continue
        seen_urls.add(url)
        acc_cell = (
            (row.get(account_field) or "").strip()
            if account_field
            else ""
        )
        bol_acc = _parse_bol_account_cell(acc_cell, row_index)
        products.append({
            "product_url": url,
            "bol_account": bol_acc,
            "offer_uid": None,
            "last_checked_offer_uid": None,
            "last_checked_seller_name": None,
            "last_checked_seller_id": None,
            "last_checked_is_bol": None,
            "last_skipped_offer_uid": None,
        })
    return products


def try_load_products_runtime(filename: str = PRODUCTS_FILE) -> tuple[list[dict] | None, str | None]:
    try:
        return load_products(filename), None
    except PermissionError as e:
        return None, f"{PRODUCTS_FILE} is locked or being edited ({e})"
    except (FileNotFoundError, OSError, ValueError, csv.Error) as e:
        return None, str(e)


def sync_sitemap_known_urls_from_csv_urls(urls: tuple[str, ...]) -> None:
    """Reset in-memory URL set to match current product.csv rows (authoritative dedup)."""
    with CSV_LOCK:
        SITEMAP_KNOWN_URLS.clear()
        for u in urls:
            u = (u or "").strip()
            if u:
                SITEMAP_KNOWN_URLS.add(u)


def append_product_url_to_csv_if_new(url: str) -> bool:
    """
    Append a single product_url if absent from product.csv and memory.
    Returns True when a new row was written.
    """
    url = (url or "").strip()
    if not url.startswith("http"):
        return False
    ensure_products_csv(PRODUCTS_FILE)
    with CSV_LOCK:
        if url in SITEMAP_KNOWN_URLS:
            return False
        try:
            with open(PRODUCTS_FILE, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                url_field = _product_csv_url_field(reader.fieldnames)
                for row in reader:
                    u = (row.get(url_field) or "").strip()
                    if u == url:
                        SITEMAP_KNOWN_URLS.add(url)
                        return False
        except (OSError, ValueError, csv.Error):
            pass

        with open(PRODUCTS_FILE, "a", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
            writer.writerow({"product_url": url, "bol_account": "1"})
        SITEMAP_KNOWN_URLS.add(url)
        return True


def _sitemap_monitor_loop(
    stop_event: threading.Event,
    discord_webhook: str | None,
    discord_thread_id: str | None,
    discord_thread_name: str | None,
) -> None:
    if _bol_sitemap_monitor is None:
        print("[SITEMAP] monitor.py not found -- sitemap integration disabled", flush=True)
        return

    cwd = os.getcwd()
    interval = max(15.0, SITEMAP_SCAN_INTERVAL_SECS)
    extra_kw_path = os.path.join(cwd, _bol_sitemap_monitor.SITEMAP_EXTRA_KEYWORDS_FILE)
    warned_no_extra_keywords = False

    while not stop_event.is_set():
        if not _bol_sitemap_monitor.read_extra_sitemap_keyword_tokens(extra_kw_path):
            if not warned_no_extra_keywords:
                print(
                    f"[SITEMAP] Optional sitemap keywords missing ({_bol_sitemap_monitor.SITEMAP_EXTRA_KEYWORDS_FILE}) "
                    f"— product monitoring from {PRODUCTS_FILE} is unchanged.",
                    flush=True,
                )
                warned_no_extra_keywords = True
            if stop_event.wait(interval):
                break
            continue
        warned_no_extra_keywords = False

        try:
            print("[SITEMAP] Scanning for new products...", flush=True)
            new_links = _bol_sitemap_monitor.get_new_product_links(
                index_url=SITEMAP_INDEX_URL,
                db_path=os.path.join(cwd, SITEMAP_DB),
                all_links_path=os.path.join(cwd, SITEMAP_ALL_LINKS),
                new_links_path=os.path.join(cwd, SITEMAP_NEW_LINKS),
                proxy_file=(
                    os.path.join(cwd, PROXY_FILE) if _env_truthy("USE_PROXIES", "1") else None
                ),
                workers=max(1, SITEMAP_WORKERS),
                timeout=30.0,
                proxy_timeout=8.0,
                retries=2,
                silent=True,
                product_csv_path=os.path.join(cwd, PRODUCTS_FILE),
                require_extra_sitemap_keywords=True,
            )
        except Exception as e:
            print(f"[SITEMAP] Scan error (will retry next cycle): {e}", flush=True)
        else:
            for link in new_links:
                print(f"[SITEMAP] New product detected: {link}", flush=True)
                if append_product_url_to_csv_if_new(link):
                    print(
                        "[SITEMAP] Added to product.csv and monitoring started",
                        flush=True,
                    )
                    send_discord_message(
                        discord_webhook,
                        f"New product detected from sitemap: {link}",
                        discord_thread_id,
                        discord_thread_name,
                    )
                else:
                    print(f"[SITEMAP] Skipped duplicate: {link}", flush=True)

        if stop_event.wait(interval):
            break


def _read_file(filename: str) -> str | None:
    path = os.path.join(os.getcwd(), filename)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        v = f.read().strip()
        return v or None


def load_discord_webhook() -> str | None:
    return (
        os.getenv("DISCORD_WEBHOOK_URL")
        or os.getenv("DISCORD_WEBHOOK")
        or os.getenv("WEBHOOK_URL")
        or _read_file(DISCORD_WEBHOOK_FILE)
    )


def load_discord_thread_config() -> tuple[str | None, str | None]:
    tid  = (os.getenv("DISCORD_THREAD_ID")   or os.getenv("WEBHOOK_THREAD_ID")   or _read_file(DISCORD_THREAD_ID_FILE))
    name = (os.getenv("DISCORD_THREAD_NAME") or os.getenv("WEBHOOK_THREAD_NAME") or _read_file(DISCORD_THREAD_NAME_FILE))
    return (tid.strip() if tid else None), (name.strip() if name else None)


def initialize_product_success_counts(filename: str = PAYMENT_URLS_FILE) -> None:
    _ = filename
    with PAYMENT_URLS_LOCK:
        PRODUCT_SUCCESS_COUNTS.clear()


def get_product_success_count(product_url: str) -> int:
    with PAYMENT_URLS_LOCK:
        return PRODUCT_SUCCESS_COUNTS.get(product_url, 0)


def record_product_success(product_url: str) -> int:
    with PAYMENT_URLS_LOCK:
        PRODUCT_SUCCESS_COUNTS[product_url] = PRODUCT_SUCCESS_COUNTS.get(product_url, 0) + 1
        return PRODUCT_SUCCESS_COUNTS[product_url]


def save_payment_url(
    ideal_url: str,
    product_url: str,
    product_id: str,
    offer_uid: str,
    seller_name: str | None,
) -> int | None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    seller_label = seller_name or "unknown"
    line = (
        f"{timestamp}\tproductId={product_id}\tofferUid={offer_uid}"
        f"\tseller={seller_label}\tproductUrl={product_url}\tpayUrl={ideal_url}\n"
    )
    try:
        with PAYMENT_URLS_LOCK:
            with open(PAYMENT_URLS_FILE, "a", encoding="utf-8") as f:
                f.write(line)
            cnt = PRODUCT_SUCCESS_COUNTS.get(product_url, 0) + 1
            PRODUCT_SUCCESS_COUNTS[product_url] = cnt
        try:
            with open(PAYMENT_URL_LAST_FILE, "w", encoding="utf-8") as f2:
                f2.write(ideal_url.strip() + "\n")
        except Exception as e2:
            print(f"  Could not write {PAYMENT_URL_LAST_FILE}: {e2}")
        print(f"  Appended to {PAYMENT_URLS_FILE}; latest URL → {PAYMENT_URL_LAST_FILE}")
        return cnt
    except Exception as e:
        print(f"  Could not append payment URL: {e}")
        return None


def create_worker_basket_ctx() -> dict:
    return {
        "basket_id": None,
        "xsrf": None,
        "page_id": None,
    }


def request_bot_stop(stop_event: threading.Event, reason: str) -> None:
    if not getattr(stop_event, "reason", None):
        stop_event.reason = reason
    stop_event.set()


def worker_should_stop(
    stop_event: threading.Event,
    worker_stop_event: threading.Event | None = None,
) -> bool:
    return stop_event.is_set() or (worker_stop_event is not None and worker_stop_event.is_set())


def wait_check_delay(
    stop_event: threading.Event,
    worker_stop_event: threading.Event | None = None,
    delay_seconds: float | None = None,
) -> None:
    if delay_seconds is None:
        delay_seconds = CHECK_DELAY_SECONDS

    if delay_seconds <= 0 or worker_should_stop(stop_event, worker_stop_event):
        return

    deadline = time.time() + delay_seconds
    while not worker_should_stop(stop_event, worker_stop_event):
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        stop_event.wait(min(0.25, remaining))


def acquire_purchase_flow_lock(
    stop_event: threading.Event,
    worker_stop_event: threading.Event | None = None,
) -> bool:
    while not worker_should_stop(stop_event, worker_stop_event):
        if PURCHASE_FLOW_LOCK.acquire(timeout=0.1):
            return True
    return False


def _start_product_workers(
    pm: ProxyManager,
    product: dict,
    product_slot: int,
    stop_event: threading.Event,
    discord_webhook: str | None,
    discord_thread_id: str | None,
    discord_thread_name: str | None,
    all_threads: list[threading.Thread],
    session_slot=None,
    session_pool=None,
) -> dict:
    if WORKERS_PER_PRODUCT > 1:
        print(
            f"  [!] WORKERS_PER_PRODUCT={WORKERS_PER_PRODUCT}: same URL may be probed "
            f"in parallel -- set to 1 for strict single-loop monitoring.",
            flush=True,
        )
    product_stop_event = threading.Event()
    threads = []

    session_cursor_ref = None
    session_slot_arg = session_slot
    pool_slots = (getattr(session_pool, "slots", None) or []) if session_pool is not None else []
    if session_pool is not None and len(pool_slots) > 1:
        pi = _session_pool_slot_index(
            product, fallback_row_index=product_slot - 1, pool_len=len(pool_slots)
        )
        session_cursor_ref = [pi]
        worker_pm = pool_slots[pi].pm
        session_slot_arg = None
    else:
        worker_pm = session_slot.pm if session_slot is not None else pm

    for worker_index in range(1, WORKERS_PER_PRODUCT + 1):
        worker_label = f"{product_slot}.{worker_index}"
        worker_basket_ctx = create_worker_basket_ctx()
        thread = threading.Thread(
            target=monitor_url_bol_only,
            args=(
                worker_pm,
                product,
                worker_label,
                worker_basket_ctx,
                stop_event,
                product_stop_event,
                discord_webhook,
                discord_thread_id,
                discord_thread_name,
                session_slot_arg,
                session_pool,
                session_cursor_ref,
            ),
            daemon=True,
        )
        thread.start()
        threads.append(thread)
        all_threads.append(thread)

    return {
        "product": product,
        "stop_event": product_stop_event,
        "threads": threads,
        "slot": product_slot,
    }


def format_discord_payment_block(
    ideal_url: str,
    *,
    product_url: str = "",
    product_id: str = "",
    offer_uid: str = "",
    price: str = "?",
    title: str | None = None,
    seller: str | None = None,
) -> str:
    """
    Single Discord message: payment URL plus a compact title | value block (same fields as cart logs).
    No extra HTTP/HTML parsing — caller passes strings already known from monitoring/checkout.
    """
    def one_line(s: str, max_len: int = 900) -> str:
        s = (s or "").replace("\r", " ").replace("\n", " ").strip()
        s = s.replace("```", "'''")
        if len(s) > max_len:
            s = s[: max_len - 1] + "…"
        return s or "—"

    iu = one_line(ideal_url, 1900)
    rows: list[tuple[str, str]] = [
        ("Title", one_line(title, 650)),
        ("Price", one_line(str(price), 48)),
        ("Product ID", one_line(str(product_id), 32)),
        ("Offer UID", one_line(str(offer_uid), 96)),
    ]
    if seller and str(seller).strip():
        rows.append(("Seller", one_line(str(seller), 120)))
    rows.append(("Product URL", one_line(product_url, 900)))
    inner = "\n".join(f"{lab:12} │ {val}" for lab, val in rows)
    out = (
        "🧾 **Bol · payment link ready**\n\n"
        f"{iu}\n\n"
        "```\n"
        f"{inner}\n"
        "```"
    )
    if len(out) > 1980:
        return out[:1970] + "\n…(truncated)"
    return out


def send_discord_message(
    webhook_url: str | None,
    content: str,
    thread_id: str | None = None,
    thread_name: str | None = None,
    background: bool = True,
) -> bool:
    if not webhook_url:
        return False
    body = {"content": content, "allowed_mentions": {"parse": []}}
    if thread_name and not thread_id:
        body["thread_name"] = thread_name
    url = webhook_url
    query = {"wait": "false"}
    if thread_id:
        query["thread_id"] = thread_id
    sep = '&' if '?' in url else '?'
    url = f"{url}{sep}{urlencode(query)}"

    def _send():
        payload = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
            method="POST")
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass

    if background:
        threading.Thread(target=_send, daemon=True).start()
        return True
    else:
        _send()
        return True


# ─────────────────────────────────────────────
#  LOGIN CHECK
#  Uses the Preferences persisted query (confirmed from HAR).
#  Returns the logged-in email or None if not authenticated.
# ─────────────────────────────────────────────

def check_login(pm: ProxyManager, session_slot=None) -> str | None:
    """
    Checks login by fetching the orders page.
    - Logged in  → title contains "Bestellingen"
    - Logged out → title contains "Inloggen" (redirected to login page)
    This is the most reliable method -- confirmed by user testing.
    """
    try:
        resp = pm.get(
            "https://www.bol.com/nl/nl/account/bestellingen/overzicht/",
            headers={**PAGE_HEADERS_SAME_ORIGIN, "referer": "https://www.bol.com/nl/nl/"},
        )
        html = resp.text
        code = int(resp.status_code)
        final_url = getattr(resp, "url", "") or ""
        if session_slot is not None and code in (401, 403):
            session_slot.on_http_status(code, final_url)
        plab = pm.proxy_label_for_log(pm.get_thread_proxy())
        _diag(
            f"login GET bestellingen HTTP={code} url={final_url[:120] or '(n/a)'} "
            f"html_bytes={len(html)} proxy={plab}"
        )
        if re.search(r'<title[^>]*>[^<]*Bestellingen[^<]*</title>', html, re.IGNORECASE):
            # Extract name from page if possible
            name_m = re.search(r'Hallo[,\s]+([A-Za-z]+)', html)
            name = name_m.group(1) if name_m else "user"
            _diag("login: page title Bestellingen → logged in")
            return name
        if re.search(r'<title[^>]*>[^<]*Inloggen[^<]*</title>', html, re.IGNORECASE):
            _diag("login: page title Inloggen → not logged in (cookies invalid/expired or wrong proxy region)")
            if session_slot is not None:
                session_slot.on_login_redirect()
            return None  # redirected to login -- not authenticated
        # Unexpected page -- check for any logged-in indicator
        if re.search(r'/account/bestellingen|uitloggen', html, re.IGNORECASE):
            _diag("login: matched account/uitloggen markers → treating as logged in")
            return "user"
        _diag(f"login: unknown state title={_html_title_snippet(html)!r}")
        return None
    except Exception as e:
        _diag(f"login: request failed {type(e).__name__}: {e}")
        print(f"  Login check error: {e}")
        return None


def check_login_with_retries(pm: ProxyManager, session_slot=None) -> str | None:
    """Same as check_login but tries other proxies if the first hop errors or shows login page."""
    if session_slot is not None:
        return check_login(pm, session_slot=session_slot)
    # Logged-in cookie sessions: never rotate proxy on login probe (keeps IP aligned with cookies).
    if _pdp_lock_same_proxy(pm):
        return check_login(pm, session_slot=None)
    for attempt in range(LOGIN_CHECK_RETRIES):
        _diag(f"login attempt {attempt + 1}/{LOGIN_CHECK_RETRIES}")
        email = check_login(pm)
        if email:
            if attempt > 0:
                print(f"  Login OK after {attempt + 1} attempt(s).", flush=True)
            return email
        if attempt + 1 < LOGIN_CHECK_RETRIES:
            _diag("login: rotating proxy and retrying…")
            try:
                pm.rotate(block_seconds=0.0, aggressive_block=False, count_failure=False)
            except Exception as ex:
                _diag(f"login: rotate failed {ex!r}")
            time.sleep(min(1.2, 0.25 + 0.35 * attempt))
    _diag(
        "login FAILED after all retries → re-export cookies from same browser profile, "
        "or try same-country proxy as export, or disable rotating pool for this account."
    )
    return None


def attempt_bol_password_login_for_slot(slot) -> bool:
    """
    Experimental password login using BOL_EMAIL / BOL_PASSWORD from .env.
    On success, cookies are merged into the slot jar and saved to disk.
    Bol may use SSO/captcha — if this returns False, export cookies per slot manually.
    """
    if SESSIONS is None:
        return False
    SESSIONS.load_env_file()
    email = os.getenv("BOL_EMAIL", "").strip()
    pw = os.getenv("BOL_PASSWORD", "").strip()
    if not email or not pw:
        return False
    pm = slot.pm
    login_get = "https://www.bol.com/nl/inloggen/"
    try:
        r0 = pm.get(
            login_get,
            checkout=True,
            raise_for_status=False,
            headers={
                **PAGE_HEADERS_SAME_ORIGIN,
                "referer": "https://www.bol.com/nl/nl/",
            },
        )
        referer = getattr(r0, "url", None) or login_get
        # Legacy-style fields (site may be SPA; POST often fails without browser flow)
        data = urlencode(
            {
                "loginFormEmail": email,
                "loginFormPassword": pw,
            }
        ).encode("utf-8")
        r1 = pm.post(
            login_get,
            checkout=True,
            raise_for_status=False,
            data=data,
            headers={
                **PAGE_HEADERS_SAME_ORIGIN,
                "referer": referer,
                "content-type": "application/x-www-form-urlencoded",
            },
        )
        _diag(f"login POST experiment HTTP={r1.status_code}")
    except Exception as e:
        _diag(f"login POST experiment failed: {e}")
        return False
    who = check_login(pm, session_slot=slot)
    if not who:
        print(
            "  [LOGIN] Password POST did not establish session (normal if bol uses SSO/captcha). "
            "Paste cookies into sessions/slot_XX/cookies.txt per proxy.",
            flush=True,
        )
        return False
    try:
        SESSIONS.save_cookie_jar_json(slot.cookies_path, dict(pm.cookies), preserve_shape_path=slot.cookies_path)
    except Exception as e:
        print(f"  [LOGIN] save cookies failed: {e}", flush=True)
        return False
    print(f"  [LOGIN] Session refreshed for slot {slot.slot_id}", flush=True)
    return True


def _monitor_pick_pm_slot(
    pm_arg: ProxyManager,
    session_pool,
    session_cursor_ref: list[int] | None,
    slot_fallback,
):
    """Returns (pm, slot_or_none) for this monitoring iteration — pinned to bol_account slot (no cross-account fallback)."""
    if session_pool is None or session_cursor_ref is None or SESSIONS is None:
        return pm_arg, slot_fallback
    n = len(session_pool.slots)
    if n == 0:
        return pm_arg, slot_fallback
    idx = session_cursor_ref[0] % n
    slot = session_pool.slots[idx]
    if slot.state != SESSIONS.STATE_DEAD:
        return slot.pm, slot
    return None, None


# ─────────────────────────────────────────────
#  STEP 1: FETCH PRODUCT PAGE
#  Single fetch returns everything needed for cart + checkout.
# ─────────────────────────────────────────────

def _product_graphql_headers(
    pm: ProxyManager,
    product_url: str,
    operation: str,
    basket_ctx: dict | None = None,
    client_app: str = "product-web-fe",
) -> dict:
    headers = {
        **GQL_HEADERS,
        "referer": product_url,
        "bol-app-country": "NL",
        "bol-app-operation-name": operation,
        "bol-client-app-name": client_app,
    }
    xsrf = current_xsrf_token(pm, basket_ctx)
    page_id = (basket_ctx or {}).get("page_id")
    if xsrf:
        headers["x-xsrf-token"] = xsrf
    if page_id:
        headers["bol-client-page-id"] = page_id
        headers["m2-page-id"] = page_id
    return headers


def is_bol_seller(seller_name: str | None, seller_id: str | None) -> bool:
    normalized_name = (seller_name or "").strip().casefold()
    normalized_id = (seller_id or "").strip()
    return normalized_name == BOL_SELLER_NAME or normalized_id == BOL_RETAILER_ID


# ─────────────────────────────────────────────
#  HTML product page (monitoring only — no Product / retailerInfo GraphQL)
# ─────────────────────────────────────────────


def infer_seller_from_pdp_html(html: str) -> tuple[str | None, str | None, bool]:
    name: str | None = None
    sid: str | None = None
    if not html:
        return None, None, False
    m = re.search(r'"retailerId"\s*:\s*"?([0-9]{1,16})"?', html)
    if m:
        sid = m.group(1)
    m2 = re.search(r'"sellerName"\s*:\s*"([^"\\]{0,200})"', html)
    if m2:
        name = m2.group(1).strip()
    if not name:
        m3 = re.search(
            r"Verkoop\s+door\s*</[^>]{1,40}>\s*([^<]{1,120})",
            html,
            re.I | re.DOTALL,
        )
        if m3:
            name = re.sub(r"\s+", " ", m3.group(1)).strip()
    is_bol = is_bol_seller(name, sid)
    if not is_bol and re.search(r"Verkoop\s+door\s*.{0,60}bol\.com", html, re.I | re.DOTALL):
        is_bol = True
        name = name or BOL_SELLER_NAME
    if not is_bol and re.search(r"Verkoop\s+door\s*bol\b", html, re.I):
        is_bol = True
        name = name or BOL_SELLER_NAME
    return name, sid, is_bol


def _extract_offer_uid_from_pdp_html(html: str) -> str | None:
    """
    Bol's AddItem API requires this UUID — same one as in real 'Toevoegen' links (?offerUid=…).
    One product URL can have multiple offers (Bol vs marketplace); offerUid picks the exact line.
    """
    if not html:
        return None
    chunk = html[: min(len(html), 1_200_000)]
    patterns = (
        r"[?&](?:amp;)?offerUid=(" + UUID + r")\b",
        r"(?:[?&]|%26|%3F)offerUid(?:%3D|=)(" + UUID + r")\b",
        r'"offerUid"\s*:\s*"(' + UUID + r')"',
        r"'offerUid'\s*:\s*'(" + UUID + r")'",
        r'\\"offerUid\\"\s*:\s*\\"(' + UUID + r')\\"',
        r'offerUid\\":\\"(' + UUID + r')\\"',
        r'offerUid["\s]*[=:]["\s]*(' + UUID + r')',
    )
    for pat in patterns:
        m = re.search(pat, chunk, re.I)
        if m:
            return m.group(1)
    # Any JSON-like offerUid values on page (same listing repeated)
    for u in re.findall(r'"offerUid"\s*:\s*"(' + UUID + r')"', chunk, re.I):
        return u
    return None


def _pdp_shell_override_not_thin(html: str, offline: bool) -> bool:
    """
    HTTP 200 shell pages are tiny (~2kB) with no product data — those stay thin.
    If we already have offerUid or embedded Next/API product JSON, do not treat as thin:
    monitoring + add-to-cart only need offerUid, not full desktop HTML.
    """
    if offline or not html:
        return False
    if _extract_offer_uid_from_pdp_html(html):
        return True
    chunk = html[:500_000]
    lc = chunk.casefold()
    if len(html) >= 50_000:
        return True
    # Modern bol PDP: JSON blobs even when visible buy block uses different class names
    strong = (
        "__next_data__" in lc,
        "globalentityid" in lc.replace(" ", ""),
        '"@type"' in chunk and "product" in lc[:120000],
        "schema.org/product" in lc,
        ("sellingprice" in lc or '"amount"' in lc[:80000])
        and ("offeruid" in lc.replace("\\", "") or "retailproduct" in lc),
        "productdetail" in lc or "product-detail" in lc,
        "toevoegen aan winkelwagen" in lc or "in winkelwagen" in lc,
    )
    return sum(1 for s in strong if s) >= 1


def _pdp_lock_same_proxy(pm) -> bool:
    """
    When True: PDP thin retries never switch pool line (single sticky + cookies).
    Multi-line pool + cookies: False — same session as your working logs (thin → rotate → full HTML on #6).
    """
    if not getattr(pm, "_logged_in_cookies", False):
        return False
    return bool(getattr(pm, "_sticky_single", False))


def _pdp_use_same_proxy_for_thin(pm, session_slot) -> bool:
    """
    For thin-PDP handling: use same proxy + delay retries instead of pool rotate when
    (a) a session slot is active, (b) cookies indicate logged-in state, or (c) only one
    pool line exists (pm.rotate is a no-op / short sleep — 'rotate' in logs was misleading).
    Anonymous multi-line pool: may still rotate to find a working exit.
    """
    if session_slot is not None:
        return True
    if getattr(pm, "_logged_in_cookies", False):
        return True
    if getattr(pm, "_sticky_single", False):
        return True
    return False


def _pdp_probe_max_attempts(pm, session_slot) -> int:
    if _pdp_use_same_proxy_for_thin(pm, session_slot):
        return max(1, PDP_THIN_SAME_PROXY_RETRIES)
    return MONITOR_HTML_PROBE_RETRIES


def _pdp_content_retry_delay(*, slow: bool = False) -> float:
    lo = min(PDP_THIN_RETRY_DELAY_MIN, PDP_THIN_RETRY_DELAY_MAX)
    hi = max(PDP_THIN_RETRY_DELAY_MIN, PDP_THIN_RETRY_DELAY_MAX)
    return float(random.uniform(lo, hi) * (1.45 if slow else 1.0))


def _populate_pdp_snapshot_from_html(out: dict, html: str, product_id: str) -> None:
    """Fill PDP parse fields from HTML body (caller sets HTTP)."""
    out["html_len"] = len(html)
    out["title"] = _html_title_snippet(html, 200)
    tit_low = out["title"].casefold()
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

    hcf = html.casefold()
    has_buy_signals = (
        "buyBlockSlot" in html
        or "offerUid" in html
        or "offeruid" in hcf.replace("\\", "")
        or "winkelwagen" in hcf
        or "in winkelwagen" in hcf
        or "op voorraad" in hcf
        or "toevoegen" in hcf
        or "__NEXT_DATA__" in html
        or "globalEntityId" in html
        or ("schema.org/product" in hcf or '"@type"' in html[:120000])
        or ('type="application/json"' in html and "product" in html[:50000])
        or ("sellingPrice" in html and ("availability" in html[:120000] or "offerUid" in html))
    )
    hl = len(html)
    uid_early = _extract_offer_uid_from_pdp_html(html)
    if uid_early:
        out["offer_uid"] = uid_early

    thin = True
    if uid_early:
        thin = False
    elif _pdp_shell_override_not_thin(html, bool(out.get("offline"))):
        thin = False
    elif hl >= 100_000:
        thin = False
    elif hl < 10_000:
        thin = True
    elif hl < 50_000:
        thin = not has_buy_signals
    else:
        # 50k–99k: soft band — treat as full page only when buy/product signals exist
        thin = not has_buy_signals

    out["thin_page"] = thin
    if thin:
        out["diag"] = (
            "short_html_shell_or_challenge_retry_checkout_path"
            if hl < 10_000
            else "thin_soft_retry_band"
        )
        return

    if not out.get("offer_uid"):
        out["offer_uid"] = _extract_offer_uid_from_pdp_html(html)

    pm_price = re.search(
        r'"sellingPrice"\s*:\s*\{[^}]*"amount"\s*:\s*"?([0-9]+[.,][0-9]{2})"?',
        html,
    )
    if pm_price:
        out["price_text"] = pm_price.group(1)
    else:
        pe = re.search(
            r'(?:content|aria-label)="[^"]*€\s*([0-9]+[.,][0-9]{2})',
            html[:400000],
            re.I,
        )
        if pe:
            out["price_text"] = pe.group(1)

    blob_lc = html[:350000].lower()
    no_stock_markers = (
        "no_stock",
        "item_no_offer",
        "niet op voorraad",
        "niet leverbaar",
        "tijdelijk niet leverbaar",
        "uitverkocht",
        "niet verkrijgbaar",
        "geen voorraad",
        '"availability":"outofstock"',
        '"instock":false',
    )
    out["no_stock"] = any(m in blob_lc for m in no_stock_markers)

    out["has_in_winkelwagen"] = "in winkelwagen" in blob_lc
    out["has_op_voorraad"] = (
        "op voorraad" in blob_lc and "niet op voorraad" not in blob_lc
    )
    out["pdp_ready_to_buy"] = bool(
        out["has_in_winkelwagen"] and out["has_op_voorraad"] and not out["no_stock"]
    )

    if not out["price_text"]:
        idx = hcf.find("in winkelwagen")
        if idx != -1:
            win = html[max(0, idx - 12000) : idx + 4000]
            m_nl = re.search(r"([0-9]{1,5})\s*,\s*-\s*", win)
            if m_nl:
                out["price_text"] = m_nl.group(1)

    sn, sid, ib = infer_seller_from_pdp_html(html)
    out["seller_name"] = sn
    out["seller_id"] = sid
    out["is_bol"] = ib

    out["parse_ok"] = bool(not out["offline"] and not thin)


def _prime_shopper_session_before_pdp(pm: ProxyManager) -> None:
    """
    Once per worker thread: load nl storefront before the first product PDP GET.
    Login already validated cookies on account URLs; PDP is another route — without this,
    some edges return a tiny shell (~2kB) on the first product request even with correct cookies+proxy.
    """
    if not getattr(pm, "_logged_in_cookies", False):
        return
    if not hasattr(pm, "_pdp_storefront_prime_tl"):
        pm._pdp_storefront_prime_tl = threading.local()
    if getattr(pm._pdp_storefront_prime_tl, "done", False):
        return
    try:
        pm.get(
            "https://www.bol.com/nl/nl/",
            checkout=True,
            raise_for_status=False,
            headers={
                **PAGE_HEADERS,
                "referer": "https://www.bol.com/",
            },
        )
        time.sleep(random.uniform(0.08, 0.18))
    except Exception:
        pass
    pm._pdp_storefront_prime_tl.done = True


def html_product_page_snapshot(
    pm: ProxyManager,
    product_url: str,
    product_id: str,
) -> dict:
    """GET PDP: logged-in = storefront once/thread → delay → product URL → parse (optional second GET after settle)."""
    empty: dict = {
        "http": -1,
        "html_len": 0,
        "title": "",
        "price_text": None,
        "offer_uid": None,
        "no_stock": False,
        "offline": False,
        "thin_page": True,
        "parse_ok": False,
        "seller_name": None,
        "seller_id": None,
        "is_bol": False,
        "diag": "",
        "has_in_winkelwagen": False,
        "has_op_voorraad": False,
        "pdp_ready_to_buy": False,
    }
    use_checkout = bool(getattr(pm, "_logged_in_cookies", False))
    nl_storefront = "https://www.bol.com/nl/nl/"

    def _get_once(referer: str) -> dict:
        out = dict(empty)
        try:
            pdp_headers = {**PAGE_HEADERS_SAME_ORIGIN, "referer": referer}
            resp = pm.get(
                product_url,
                checkout=use_checkout,
                raise_for_status=False,
                headers=pdp_headers,
            )
            if MONITOR_HTTP_TRACE:
                ph = pm.proxy_label_for_log(pm.get_thread_proxy())
                print(
                    f"[HTTP] GET product productId={product_id} | Proxy {ph} | Status {resp.status_code} "
                    f"referer={referer[:72]!r}",
                    flush=True,
                )
            code = int(resp.status_code)
            out["http"] = code
            if code != 200:
                return out
            html = resp.text or ""
            _populate_pdp_snapshot_from_html(out, html, product_id)
        except RotateProxy as e:
            out["diag"] = f"RotateProxy: {e}"
            out["http"] = -1
        except Exception as e:
            out["diag"] = f"{type(e).__name__}: {e}"
            out["http"] = -1
        return out

    if use_checkout:
        _prime_shopper_session_before_pdp(pm)
        time.sleep(_monitor_product_nav_delay_sec())
        last_out = _get_once(nl_storefront)
        if last_out["http"] != 200:
            return last_out
        if not last_out["thin_page"] or last_out.get("offline"):
            return last_out
        time.sleep(_monitor_product_load_settle_sec())
        last_out = _get_once(nl_storefront)
        return last_out

    return _get_once(product_url)


def html_monitor_probe(
    pm: ProxyManager,
    product_url: str,
    product_id: str,
    basket_ctx: dict | None = None,
    index: str | int | None = None,
    session_slot=None,
) -> tuple[int, str, dict]:
    """GET + parse PDP with retries; returns (http, ST_*, detail_dict). No GraphQL."""
    _ = basket_ctx
    last_detail: dict = {}
    prefix = f"[{index}] " if index is not None else ""
    lock_pdp = _pdp_lock_same_proxy(pm) or session_slot is not None
    max_attempts = _pdp_probe_max_attempts(pm, session_slot)

    def _trace(snap: dict, attempt: int, note: str = "") -> None:
        if not BOL_DIAGNOSTIC_LOG or not BOL_MONITOR_FULL_TRACE:
            return
        ou = snap.get("offer_uid")
        ou_h = f"…{ou[-8:]}" if ou else "none"
        _diag_probe(
            f"{prefix}HTML monitor {attempt + 1}/{max_attempts} | "
            f"HTTP {snap['http']} | len={snap['html_len']} | thin={snap['thin_page']} | "
            f"offline={snap['offline']} | parse_ok={snap['parse_ok']} | no_stock={snap['no_stock']} | "
            f"wagen={snap.get('has_in_winkelwagen')} voorraad={snap.get('has_op_voorraad')} "
            f"ready={snap.get('pdp_ready_to_buy')} | "
            f"offer_uid={ou_h} | price={snap.get('price_text')!r} | "
            f"title={snap.get('title', '')[:72]!r}"
            + (f" | {note}" if note else "")
        )

    for attempt in range(max_attempts):
        snap = html_product_page_snapshot(pm, product_url, product_id)
        last_detail = dict(snap)
        if snap.get("thin_page") and snap.get("http") == 200:
            _notify_fetching_product_details_once(product_url)
        _trace(snap, attempt)

        httpv = snap["http"]
        if not isinstance(httpv, int) or httpv < 0 or httpv == 0:
            if lock_pdp and attempt + 1 < max_attempts:
                _diag_probe(
                    f"{prefix}product page transport/timeout (http={httpv!r}) → same-proxy retry "
                    f"{attempt + 1}/{max_attempts}"
                )
                time.sleep(_pdp_content_retry_delay(slow=True))
                continue
            last_detail["diag"] = last_detail.get("diag") or "transport_error"
            return (int(httpv) if isinstance(httpv, int) else -1), ST_ERROR, last_detail

        if httpv in (403, 429, 503):
            if lock_pdp:
                if attempt + 1 < max_attempts:
                    _diag_probe(
                        f"{prefix}HTTP {httpv} → same-proxy backoff (no rotate) "
                        f"{attempt + 1}/{max_attempts}"
                    )
                    time.sleep(_pdp_content_retry_delay(slow=(httpv == 403)))
                    continue
                if (
                    httpv == 403
                    and (not getattr(pm, "_sticky_single", False))
                    and len(getattr(pm, "proxies", []) or []) > 1
                ):
                    try:
                        pm.rotate(
                            block_seconds=0.0,
                            aggressive_block=True,
                            count_failure=True,
                        )
                    except Exception as ex:
                        _diag_probe(f"{prefix}final 403 rotate: {ex!r}")
                if session_slot is not None and httpv in (401, 403):
                    session_slot.on_http_status(httpv, product_url)
                if session_slot is not None:
                    session_slot.log_line(httpv, product_url, "probe → BLOCKED after retries")
                return httpv, ST_BLOCKED, last_detail
            if attempt + 1 < max_attempts:
                try:
                    pm.rotate(
                        block_seconds=0.0,
                        aggressive_block=httpv == 403,
                        count_failure=True,
                    )
                except Exception as ex:
                    _diag_probe(f"{prefix}rotate after HTTP {httpv}: {ex!r}")
                time.sleep(0.12)
                continue
            return httpv, ST_BLOCKED, last_detail

        if httpv != 200:
            if lock_pdp:
                if attempt + 1 < max_attempts:
                    _diag_probe(f"{prefix}HTTP {httpv} → same-proxy retry {attempt + 1}/{max_attempts}")
                    time.sleep(_pdp_content_retry_delay())
                    continue
                if session_slot is not None:
                    session_slot.log_line(httpv, product_url, "probe non-200 final")
                return httpv, ST_ERROR, last_detail
            if attempt + 1 < max_attempts:
                try:
                    pm.rotate(block_seconds=0.0, aggressive_block=False, count_failure=False)
                except Exception as ex:
                    _diag_probe(f"{prefix}rotate after HTTP {httpv}: {ex!r}")
                time.sleep(0.1)
                continue
            return httpv, ST_ERROR, last_detail

        if snap["offline"]:
            return 200, ST_OFFLINE, last_detail

        if session_slot is not None:
            tit = (snap.get("title") or "").casefold()
            if "inloggen" in tit:
                if lock_pdp and attempt + 1 < max_attempts:
                    time.sleep(_pdp_content_retry_delay())
                    continue
                session_slot.on_login_redirect()
                session_slot.log_line(200, product_url, "product page title Inloggen → session EXPIRED")
                last_detail["diag"] = "pdp_login_title"
                return 200, ST_ERROR, last_detail

        if snap["thin_page"]:
            stay = _pdp_use_same_proxy_for_thin(pm, session_slot)
            if stay:
                if attempt + 1 < max_attempts:
                    time.sleep(_pdp_content_retry_delay())
                    continue
                last_detail["diag"] = "thin_soft_cycle"
                return 200, ST_ERROR, last_detail
            if attempt + 1 < max_attempts:
                try:
                    pm.rotate(block_seconds=0.0, aggressive_block=False, count_failure=False)
                except Exception as ex:
                    _diag_probe(f"{prefix}rotate after thin: {ex!r}")
                time.sleep(0.15)
                continue
            last_detail["diag"] = "thin_soft_cycle"
            return 200, ST_ERROR, last_detail

        if snap.get("no_stock"):
            return 200, ST_ONLINE_OOS, last_detail

        uid = snap.get("offer_uid")
        if uid:
            return 200, ST_IN_STOCK, last_detail

        if snap.get("pdp_ready_to_buy"):
            stay = _pdp_use_same_proxy_for_thin(pm, session_slot)
            if stay:
                if attempt + 1 < max_attempts:
                    time.sleep(_pdp_content_retry_delay())
                    continue
                last_detail["diag"] = "thin_soft_cycle"
                return 200, ST_ERROR, last_detail
            if attempt + 1 < max_attempts:
                _diag_probe(
                    f"{prefix}page shows In winkelwagen + Op voorraad maar nog geen offerUid — "
                    f"retry ({attempt + 1}/{max_attempts})"
                )
                try:
                    pm.rotate(block_seconds=0.0, aggressive_block=False, count_failure=False)
                except Exception as ex:
                    _diag_probe(f"{prefix}rotate (offerUid parse): {ex!r}")
                time.sleep(0.35)
                continue
            last_detail["diag"] = "pdp_ready_to_buy_but_no_offerUid"
            return 200, ST_ERROR, last_detail

        if _pdp_use_same_proxy_for_thin(pm, session_slot):
            if attempt + 1 < max_attempts:
                _diag_probe(
                    f"{prefix}ambiguous page (no offerUid) → same-proxy retry "
                    f"{attempt + 1}/{max_attempts}"
                )
                time.sleep(_pdp_content_retry_delay())
                continue

        if session_slot is not None:
            last_detail["diag"] = "no_offer_uid_session_slot"
            return 200, ST_ONLINE_OOS, last_detail

        if attempt + 1 < max_attempts:
            try:
                pm.rotate(block_seconds=0.0, aggressive_block=False, count_failure=False)
            except Exception as ex:
                _diag_probe(f"{prefix}rotate (no offerUid on product page): {ex!r}")
            time.sleep(0.2)
            continue
        return 200, ST_ONLINE_OOS, last_detail

    return int(last_detail.get("http", -1) or -1), ST_ERROR, last_detail


def html_fetch_offer_uid_with_retries(
    pm: ProxyManager,
    product_url: str,
    product_id: str,
) -> tuple[str | None, dict]:
    """Resolve listing id (offerUid) from PDP HTML for AddItem — same HTML as stock/monitor."""
    last: dict = {}
    lock_pdp = _pdp_lock_same_proxy(pm)
    n = _pdp_probe_max_attempts(pm, None)
    for attempt in range(n):
        snap = html_product_page_snapshot(pm, product_url, product_id)
        last = dict(snap)
        httpv = snap["http"]
        if not isinstance(httpv, int) or httpv < 0 or httpv == 0:
            if lock_pdp and attempt + 1 < n:
                time.sleep(_pdp_content_retry_delay(slow=True))
                continue
            return None, last
        if httpv in (403, 429, 503):
            if lock_pdp:
                if attempt + 1 < n:
                    time.sleep(_pdp_content_retry_delay(slow=(httpv == 403)))
                    continue
                if (
                    httpv == 403
                    and (not getattr(pm, "_sticky_single", False))
                    and len(getattr(pm, "proxies", []) or []) > 1
                ):
                    try:
                        pm.rotate(
                            block_seconds=0.0,
                            aggressive_block=True,
                            count_failure=True,
                        )
                    except Exception:
                        pass
                return None, last
            try:
                pm.rotate(
                    block_seconds=0.0,
                    aggressive_block=httpv == 403,
                    count_failure=True,
                )
            except Exception:
                pass
            time.sleep(0.1)
            continue
        if httpv != 200:
            if lock_pdp:
                if attempt + 1 < n:
                    time.sleep(_pdp_content_retry_delay())
                    continue
                return None, last
            try:
                pm.rotate(block_seconds=0.0, aggressive_block=False, count_failure=False)
            except Exception:
                pass
            time.sleep(0.08)
            continue
        if snap.get("thin_page"):
            if _pdp_use_same_proxy_for_thin(pm, None):
                if attempt + 1 < n:
                    time.sleep(_pdp_content_retry_delay())
                    continue
                return None, last
            try:
                pm.rotate(block_seconds=0.0, aggressive_block=False, count_failure=False)
            except Exception:
                pass
            time.sleep(0.1)
            continue
        if snap.get("offline"):
            return None, last
        uid = snap.get("offer_uid")
        if uid:
            return str(uid), last
        if _pdp_use_same_proxy_for_thin(pm, None) and attempt + 1 < n:
            time.sleep(_pdp_content_retry_delay())
            continue
        if not _pdp_use_same_proxy_for_thin(pm, None) and not snap.get("thin_page") and attempt + 1 < n:
            try:
                pm.rotate(block_seconds=0.0, aggressive_block=False, count_failure=False)
            except Exception:
                pass
            time.sleep(0.1)
            continue
        return None, last
    return None, last


# ─────────────────────────────────────────────
#  MONITORING: state probe, human-like pacing, logging
# ─────────────────────────────────────────────


def _monitor_delay_online() -> float:
    lo = min(MONITOR_DELAY_ONLINE_MIN, MONITOR_DELAY_ONLINE_MAX)
    hi = max(MONITOR_DELAY_ONLINE_MIN, MONITOR_DELAY_ONLINE_MAX)
    return float(random.uniform(lo, hi))


def _monitor_delay_any() -> float:
    lo = min(MONITOR_DELAY_ANY_MIN, MONITOR_DELAY_ANY_MAX)
    hi = max(MONITOR_DELAY_ANY_MIN, MONITOR_DELAY_ANY_MAX)
    return float(random.uniform(lo, hi))


def _monitor_delay_offline() -> float:
    lo = min(MONITOR_DELAY_OFFLINE_MIN, MONITOR_DELAY_OFFLINE_MAX)
    hi = max(MONITOR_DELAY_OFFLINE_MIN, MONITOR_DELAY_OFFLINE_MAX)
    return float(random.uniform(lo, hi))


def log_monitor_check(
    index: str | int,
    product_url: str,
    pm: ProxyManager,
    http_status: int | None,
    state: str,
    session_slot=None,
) -> None:
    proxy = pm.get_thread_proxy()
    label = pm.proxy_label_for_log(proxy)
    hs = "ERR" if http_status is None else str(http_status)
    life = pm.proxy_lifecycle_status(proxy)
    fails = pm.proxy_failure_count(proxy)
    extra = ""
    if session_slot is not None:
        ep = session_slot.proxy_url.split("@")[-1][:56] if session_slot.proxy_url else "?"
        extra = f" | session_id={session_slot.slot_id!r} proxy={ep!r} session_state={session_slot.state!r}"
    print(
        f"[CHECK] [{index}] {product_url} | Proxy {label} [{life}] fails={fails} "
        f"| Status {hs} | State {state}{extra}",
        flush=True,
    )


def scrape_offer_uid_from_product_page(pm: ProxyManager, product_url: str) -> tuple[int, str | None]:
    """offerUid from visible PDP links (same GET + parse as html_product_page_snapshot)."""
    pid = extract_product_id(product_url) or ""
    snap = html_product_page_snapshot(pm, product_url, pid)
    return int(snap["http"]), snap.get("offer_uid")


def send_monitor_debug_discord(
    webhook: str | None,
    thread_id: str | None,
    thread_name: str | None,
    message: str,
) -> None:
    if not DISCORD_DEBUG or not webhook:
        return
    send_discord_message(webhook, message, thread_id, thread_name, background=True)


def fetch_page_data(pm: ProxyManager, url: str) -> tuple:
    """Returns (offer_uid, basket_id, xsrf, page_id)."""
    resp = pm.get(
        url,
        checkout=True,
        headers={**PAGE_HEADERS_SAME_ORIGIN, "referer": "https://www.bol.com/nl/nl/"},
    )
    if resp.status_code != 200:
        return None, None, None, None

    html = resp.text

    # Validate the HTML is a real bol.com product page.
    # A genuine page is always 200KB+ and contains the buy block slot.
    # A stripped CDN/cached page is small and missing these markers.
    if len(html) < 100_000 or 'buyBlockSlot' not in html:
        raise RotateProxy(f"bad HTML ({len(html):,} bytes, missing buy block)")

    # offerUid -- in href of add-to-cart links: ?offerUid=<uuid>
    offer_uid = None
    m = re.search(r'[?&](?:amp;)?offerUid=(' + UUID + r')', html, re.IGNORECASE)
    if m:
        offer_uid = m.group(1)

    # basketId -- \"Basket\",\"uuid\" in Remix dehydrated state
    basket_id = _extract_dehydrated(html, "Basket")

    # xsrf -- \"xsrf\",\"uuid\"
    xsrf = _extract_dehydrated(html, "xsrf") or pm.session.cookies.get("XSRF-TOKEN")

    # pageId -- \"pageId\",\"uuid\"
    page_id = _extract_dehydrated(html, "pageId")

    return offer_uid, basket_id, xsrf, page_id


# ─────────────────────────────────────────────
#  STEP 2: ADD TO CART
#
#  Flow you care about: monitor URL → add line → checkout → payment URL.
#  Bol's cart API (GraphQL AddItem) requires productId + offerUid + basketId — there is no shortcut
#  that means 'same as the visible button' without offerUid. That id is which seller/price line
#  this listing is; it is the same UUID you see in real PDP add-to-cart URLs (?offerUid=…).
#  We read it from the same product HTML we already fetched for monitoring / in-stock signals.
# ─────────────────────────────────────────────

def cart_success(resp: dict) -> bool:
    add_item = resp.get("data", {}).get("basket", {}).get("addItem")
    if not isinstance(add_item, dict):
        return False

    typename = str(add_item.get("__typename") or "")
    if "Failed" in typename or "Problem" in typename:
        return False

    items = add_item.get("items")
    if isinstance(items, list) and items:
        return True

    return typename == "" or typename.endswith("Basket")


def cart_item_info(resp: dict) -> dict:
    add_item = resp.get("data", {}).get("basket", {}).get("addItem") or {}
    items = add_item.get("items") or []
    item = items[0] if items else {}

    product_id = (
        item.get("product", {}).get("id")
        or item.get("product", {}).get("productId")
        or item.get("productId")
    )
    offer_uid = (
        item.get("sellingOffer", {}).get("offerUid")
        or item.get("offerUid")
    )
    price = (
        item.get("sellingOffer", {})
        .get("sellingPrice", {})
        .get("price", {})
        .get("amount")
    )
    seller = item.get("sellingOffer", {}).get("retailer", {}) or {}
    return {
        "product_id": str(product_id) if product_id is not None else None,
        "offer_uid": offer_uid,
        "price": price,
        "seller_name": seller.get("name"),
        "seller_id": str(seller.get("id")) if seller.get("id") is not None else None,
        "raw_item": item,
    }


def cart_matches_request(resp: dict, product_id: str, offer_uid: str) -> bool:
    item = cart_item_info(resp)
    if item["product_id"] and item["product_id"] != str(product_id):
        return False
    if item["offer_uid"] and item["offer_uid"] != offer_uid:
        return False
    return (
        item["product_id"] == str(product_id)
        or item["offer_uid"] == offer_uid
    )


def _format_cart_message_entry(entry: dict) -> str | None:
    if not isinstance(entry, dict):
        return None

    parts = []
    for key in ("code", "key", "type", "severity", "reason"):
        value = entry.get(key)
        if value:
            parts.append(f"{key}={value}")
    for key in ("message", "text", "description", "title"):
        value = entry.get(key)
        if value:
            parts.append(f"{key}={value}")
            break
    return ", ".join(parts) if parts else None


def _collect_cart_message_entries(value, results: list[str]) -> None:
    if value is None:
        return
    if isinstance(value, dict):
        formatted = _format_cart_message_entry(value)
        if formatted:
            results.append(formatted)
        for nested in value.values():
            _collect_cart_message_entries(nested, results)
        return
    if isinstance(value, list):
        for item in value:
            _collect_cart_message_entries(item, results)


def _cart_failure_label_from_typename(typename: str | None) -> str | None:
    normalized = str(typename or "").strip()
    if not normalized:
        return None

    lowered = normalized.casefold()
    if lowered == "failedtoadditemtobasketproblem":
        return "Bol Server Problem"
    if "nostock" in lowered or "outofstock" in lowered:
        return "Stock Out"
    if "nooffer" in lowered or "item_no_offer" in lowered:
        return "No Offer"
    if "limit" in lowered or "quantity" in lowered:
        return "Quantity Limit"
    if "blocked" in lowered or "forbidden" in lowered:
        return "Blocked"
    return None


def cart_failure_summary(resp: dict) -> str | None:
    if not isinstance(resp, dict):
        return None

    add_item = resp.get("data", {}).get("basket", {}).get("addItem") or {}
    add_item_typename = add_item.get("__typename")
    if add_item_typename:
        label = _cart_failure_label_from_typename(add_item_typename)
        return f"{add_item_typename} ({label})" if label else str(add_item_typename)

    errors = resp.get("errors") or []
    for error in errors:
        if isinstance(error, dict):
            message = error.get("message")
            if message:
                return str(message)
        elif error:
            return str(error)

    details = cart_failure_details(resp)
    if details:
        return details.split(" || ", 1)[0]
    return None


def cart_failure_details(resp: dict) -> str | None:
    if not isinstance(resp, dict):
        return None

    parts = []

    errors = resp.get("errors") or []
    error_summaries = []
    for error in errors[:5]:
        if isinstance(error, dict):
            formatted = _format_cart_message_entry(error)
            if formatted:
                error_summaries.append(formatted)
            extensions = error.get("extensions")
            if extensions:
                ext_formatted = _format_cart_message_entry(extensions)
                if ext_formatted:
                    error_summaries.append(ext_formatted)
        elif error:
            error_summaries.append(str(error))
    if error_summaries:
        parts.append("errors: " + " | ".join(dict.fromkeys(error_summaries)))

    basket = resp.get("data", {}).get("basket", {}) or {}
    add_item = basket.get("addItem") or {}

    basket_typename = basket.get("__typename")
    if basket_typename:
        parts.append(f"basket.__typename={basket_typename}")

    add_item_typename = add_item.get("__typename")
    if add_item_typename:
        parts.append(f"addItem.__typename={add_item_typename}")

    for label, value in (
        ("basket", basket.get("messages")),
        ("addItem", add_item.get("messages")),
        ("validation", add_item.get("validationErrors")),
        ("notifications", add_item.get("notifications")),
    ):
        summaries = []
        _collect_cart_message_entries(value, summaries)
        summaries = list(dict.fromkeys(summaries))
        if summaries:
            parts.append(f"{label}: " + " | ".join(summaries[:5]))

    if not parts:
        return None
    return " || ".join(parts)


def current_xsrf_token(pm: ProxyManager, basket_ctx: dict | None = None) -> str | None:
    if basket_ctx and basket_ctx.get("xsrf"):
        return basket_ctx["xsrf"]
    return (
        pm.session.cookies.get("XSRF-TOKEN")
        or pm.checkout_session.cookies.get("XSRF-TOKEN")
    )


def fetch_basket_page(pm: ProxyManager) -> tuple[dict | None, str | None]:
    resp = pm.get(
        "https://www.bol.com/nl/nl/basket/",
        checkout=True,
        headers={**PAGE_HEADERS_SAME_ORIGIN, "referer": "https://www.bol.com/nl/nl/"},
    )
    if resp.status_code != 200:
        return None, None

    html = resp.text
    return {
        "basket_id": _extract_dehydrated(html, "Basket"),
        "xsrf": _extract_dehydrated(html, "xsrf") or current_xsrf_token(pm),
        "page_id": _extract_dehydrated(html, "pageId"),
    }, html


def get_basket_page_data(pm: ProxyManager) -> dict | None:
    data, _ = fetch_basket_page(pm)
    return data


def basket_page_item_status(pm: ProxyManager, basket_ctx: dict, product_id: str, offer_uid: str) -> tuple[bool, bool]:
    data, html = fetch_basket_page(pm)
    if data:
        with BASKET_LOCK:
            for key, value in data.items():
                if value:
                    basket_ctx[key] = value
    if not html:
        return False, False

    has_item = str(product_id) in html and offer_uid in html
    no_stock = False
    if not has_item:
        product_marker = (
            f'"value","{product_id}"' in html
            or f'"value":"{product_id}"' in html
            or str(product_id) in html
        )
        no_stock = product_marker and (
            "NO_STOCK" in html
            or "ITEM_NO_OFFER" in html
            or "Niet leverbaar" in html
            or "niet meer leverbaar" in html
        )
    return has_item, no_stock


def basket_page_contains_item(pm: ProxyManager, basket_ctx: dict, product_id: str, offer_uid: str) -> bool:
    has_item, _ = basket_page_item_status(pm, basket_ctx, product_id, offer_uid)
    return has_item


def populate_basket_ctx_from_product_page(pm: ProxyManager, product_url: str, basket_ctx: dict) -> bool:
    offer_uid, page_basket_id, xsrf, page_id = fetch_page_data(pm, product_url)
    with BASKET_LOCK:
        if page_basket_id:
            basket_ctx["basket_id"] = page_basket_id
        if xsrf:
            basket_ctx["xsrf"] = xsrf
        if page_id:
            basket_ctx["page_id"] = page_id
    return bool(basket_ctx.get("basket_id"))


def create_basket(pm: ProxyManager, basket_ctx: dict | None = None) -> dict | None:
    headers = {
        **GQL_HEADERS,
        "referer": "https://www.bol.com/nl/nl/",
        "bol-app-country": "NL",
        "bol-app-operation-name": "CreateBasket",
        "bol-client-app-name": "product-web-fe",
    }
    xsrf = current_xsrf_token(pm, basket_ctx)
    page_id = (basket_ctx or {}).get("page_id")
    if xsrf:
        headers["x-xsrf-token"] = xsrf
    if page_id:
        headers["bol-client-page-id"] = page_id
        headers["m2-page-id"] = page_id

    resp = pm.post(
        GRAPHQL_URL,
        checkout=True,
        json={
            "extensions": {"persistedQuery": {"sha256Hash": CREATE_BASKET_HASH, "version": 1}},
            "operationName": "CreateBasket",
            "variables": {},
        },
        headers=headers,
    )
    try:
        return resp.json()
    except Exception:
        return None


def warm_basket(pm: ProxyManager, basket_ctx: dict, attempts: int = BASKET_WARMUP_ATTEMPTS) -> bool:
    with BASKET_LOCK:
        for attempt in range(attempts):
            try:
                data = get_basket_page_data(pm)
                if data:
                    for key, value in data.items():
                        if value:
                            basket_ctx[key] = value
                if basket_ctx.get("basket_id"):
                    return True

                create_basket(pm, basket_ctx)
                data = get_basket_page_data(pm)
                if data:
                    for key, value in data.items():
                        if value:
                            basket_ctx[key] = value
                if basket_ctx.get("basket_id"):
                    return True

                print(f"  Basket warm-up incomplete ({attempt + 1}/{attempts}) - retrying...")
            except RotateProxy as e:
                print(f"  Basket warm-up blocked ({e}) - rotating...")
                pm.rotate()
            except Exception as e:
                print(f"  Basket warm-up error: {e}")
                break
        return False


def warm_basket_from_products(pm: ProxyManager, products: list[dict], basket_ctx: dict) -> bool:
    for product in products:
        url = product.get("product_url")
        if not url:
            continue
        try:
            if populate_basket_ctx_from_product_page(pm, url, basket_ctx):
                return True
        except RotateProxy as e:
            print(f"  Product-page basket fallback blocked ({e}) - rotating...")
            pm.rotate()
        except Exception as e:
            print(f"  Product-page basket fallback error for {url}: {e}")
    return False


def human_pause_after_add_to_cart() -> None:
    """Short wait after AddItem succeeds (less aggressive than jumping straight into checkout)."""
    if AFTER_ADD_TO_CART_PAUSE > 0:
        time.sleep(AFTER_ADD_TO_CART_PAUSE)


def add_item_to_cart(
    pm: ProxyManager,
    product_url: str,
    product_id: str,
    offer_uid: str,
    basket_ctx: dict,
) -> dict:
    """POST AddItem — offer_uid must match the listing id from the PDP (see STEP 2 header)."""
    basket_id = basket_ctx.get("basket_id")
    if not basket_id:
        raise ValueError("Basket ID is not ready.")

    xsrf = current_xsrf_token(pm, basket_ctx)
    page_id = basket_ctx.get("page_id")

    resp = pm.post(
        GRAPHQL_URL,
        checkout=True,
        json={
            "extensions": {"persistedQuery": {"sha256Hash": ADD_ITEM_HASH, "version": 1}},
            "operationName": "AddItem",
            "variables": {"input": {
                "basketId": basket_id,
                "offerUid": offer_uid,
                "productId": product_id,
                "quantity": QUANTITY,
            }},
        },
        headers={
            **GQL_HEADERS,
            "referer": product_url,
            "bol-app-country": "NL",
            "bol-app-operation-name": "AddItem",
            "bol-client-app-name": "product-web-fe",
            **({"x-xsrf-token": xsrf} if xsrf else {}),
            **({"bol-client-page-id": page_id} if page_id else {}),
            **({"m2-page-id": page_id} if page_id else {}),
        },
    )
    return resp.json()


# ─────────────────────────────────────────────
#  STEP 3: CHECKOUT → iDEAL URL
#
#  Confirmed flow from HAR analysis:
#  1. GET /nl/nl/checkout/?entryPoint=BUY_NOW -> hash, paymentPlanId, xsrf, pageId
#  2. POST CheckoutUpdatePaymentChoiceMutation → selects iDEAL server-side
#  3. POST execute-payment-plan → returns callbackPath (with tkn) + hash
#  4. POST /nl/payment-execution/ → 303 Location = pay.ideal.nl URL
# ─────────────────────────────────────────────

def get_checkout_page_data(pm: ProxyManager, product_url: str) -> dict | None:
    resp = pm.get(
        CHECKOUT_PAGE_URL,
        checkout=True,
        headers={**PAGE_HEADERS_SAME_ORIGIN, "referer": product_url},
    )
    if resp.status_code != 200:
        print(f"  Checkout page HTTP {resp.status_code}")
        return None

    html = resp.text

    # hash (=orderCandidateHash) -- \"hash\",\"<32hex>\"
    order_hash = None
    m = re.search(r'\\\"hash\\\"[,\s]*\\\"([a-f0-9]{32})\\\"', html)
    if not m:
        m = re.search(r'"hash"\s*[,:]\s*"([a-f0-9]{32})"', html)
    if m:
        order_hash = m.group(1)

    # paymentPlanId -- \"PaymentOffering\",\"<digits>\"
    payment_plan_id = None
    m = re.search(r'\\\"PaymentOffering\\\"[,\s]*\\\"(\d{6,12})\\\"', html)
    if not m:
        m = re.search(r'"PaymentOffering"[,\s]*"(\d{6,12})"', html)
    if m:
        payment_plan_id = m.group(1)

    xsrf    = _extract_dehydrated(html, "xsrf") or pm.checkout_session.cookies.get("XSRF-TOKEN")
    page_id = _extract_dehydrated(html, "pageId")

    # rsAnonymousId from cookie
    rs_anon_id = None
    anon_cookie = pm.checkout_session.cookies.get("rl_anonymous_id")
    if anon_cookie:
        try:
            raw = unquote(anon_cookie)
            if raw.startswith("RS_ENC_v3_"):
                decoded = base64.b64decode(raw[len("RS_ENC_v3_"):] + "==").decode("utf-8")
                rs_anon_id = json.loads(decoded)
        except Exception:
            pass
    if not rs_anon_id:
        rs_anon_id = _extract_dehydrated(html, "anonymousId")

    # rsSessionId -- use current timestamp as reliable fallback
    rs_session_id = int(time.time() * 1000)
    if not order_hash:
        raise CheckoutFatal("Cookie likely expired during checkout (orderCandidateHash missing).")

    return {
        "orderCandidateHash": order_hash,
        "paymentPlanId":      payment_plan_id,
        "xsrf":               xsrf,
        "page_id":            page_id,
        "rsAnonymousId":      rs_anon_id,
        "rsSessionId":        rs_session_id,
    }


def _checkout_headers(checkout_data: dict, operation: str, app: str = "checkout-web-fe") -> dict:
    """Build the full set of bol-specific headers for a checkout GraphQL call."""
    xsrf    = checkout_data.get("xsrf")
    page_id = checkout_data.get("page_id")
    hdrs = {
        **GQL_HEADERS,
        "referer":                  CHECKOUT_PAGE_URL,
        "bol-app-country":          "NL",
        "bol-app-operation-name":   operation,
        "bol-client-app-name":      app,
    }
    if xsrf:
        hdrs["x-xsrf-token"] = xsrf
    if page_id:
        hdrs["bol-client-page-id"] = page_id
        hdrs["m2-page-id"]         = page_id
    return hdrs


def select_ideal_payment(pm: ProxyManager, checkout_data: dict) -> bool:
    """
    Switches the selected payment method to iDEAL server-side.
    MUST be called before execute-payment-plan, otherwise bol defaults to PostPayment.
    Hash confirmed from HAR: 837fd4c2d7c8bb806a85c1ea7ce6b09f1c8b639d687ac298db25f5b965a4196c
    """
    try:
        resp = pm.post(
            GRAPHQL_URL,
            checkout=True,
            json={
                "extensions": {"persistedQuery": {
                    "sha256Hash": "837fd4c2d7c8bb806a85c1ea7ce6b09f1c8b639d687ac298db25f5b965a4196c",
                    "version": 1,
                }},
                "operationName": "CheckoutUpdatePaymentChoiceMutation",
                "variables": {"input": {
                    "paymentMethodCode":  "IDEAL",
                    "paymentOfferingId":  checkout_data["paymentPlanId"],
                }},
            },
            headers=_checkout_headers(checkout_data, "CheckoutUpdatePaymentChoiceMutation"),
        )
        data = resp.json()
        errors = data.get("errors", [])
        if errors:
            print(f"  iDEAL select errors: {errors}")
            return False
        return True
    except RotateProxy:
        raise
    except Exception as e:
        print(f"  iDEAL select failed: {e}")
        return False


def execute_payment_plan(pm: ProxyManager, checkout_data: dict) -> dict | None:
    """
    Submits the order. Returns response dict containing callbackPath, hash, redirectUrl.
    """
    try:
        resp = pm.post(
            "https://www.bol.com/nl/nl/rnwy/checkout/command/execute-payment-plan",
            checkout=True,
            json={
                "orderCandidateHash":    checkout_data["orderCandidateHash"],
                "paymentPlanId":         checkout_data["paymentPlanId"],
                "encryptedSecurityCode": "",
                "rsAnonymousId":         checkout_data.get("rsAnonymousId") or "",
                "rsSessionId":           checkout_data.get("rsSessionId") or int(time.time() * 1000),
            },
            headers={
                **_checkout_headers(checkout_data, "execute-payment-plan"),
                **{k: v for k, v in JSON_HEADERS.items() if k not in GQL_HEADERS},
            },
        )
        data = resp.json()
        # Validate the response has the expected structure
        inner = data.get("data") if isinstance(data, dict) else None
        if not inner or not inner.get("callbackPath"):
            print(f"  execute-payment-plan unexpected response: {data}")
            return None
        return inner
    except RotateProxy:
        raise
    except Exception as e:
        print(f"  execute-payment-plan failed: {e}")
        return None


def get_ideal_url(pm: ProxyManager, checkout_data: dict, payment_data: dict) -> str | None:
    """
    POSTs to payment-execution → 303 redirect → iDEAL URL in Location header.
    """
    callback_path   = payment_data.get("callbackPath", "")
    hash_val        = payment_data.get("hash", "")
    payment_plan_id = payment_data.get("paymentPlanId") or checkout_data["paymentPlanId"]
    redirect_url    = payment_data.get("redirectUrl", "https://www.bol.com/nl/payment-execution/")
    xsrf            = checkout_data.get("xsrf")

    hdrs = {
        **FORM_HEADERS,
        "referer": CHECKOUT_PAGE_URL,
    }
    if xsrf:
        hdrs["x-xsrf-token"] = xsrf

    proxy = pm.get_thread_proxy()
    pm._throttle_before_request(proxy)
    try:
        # Use checkout_session directly (allow_redirects=False); spacing same as pm.post/get.
        resp = pm.checkout_session.post(
            redirect_url,
            data={
                "client-callback-path":    callback_path,
                "encrypted-security-code": "",
                "payment-plan-id":         payment_plan_id,
                "hash":                    hash_val,
            },
            headers=hdrs,
            allow_redirects=False,
        )
        pm._merge_cookies_from_session(pm.checkout_session)
    except Exception as e:
        raise RotateProxy(f"payment-execution failed: {e}")
    finally:
        pm._mark_request_done(proxy)

    if resp.status_code == 429:
        raise RotateProxy("payment-execution 429 rate-limited")
    if resp.status_code in (403, 503):
        raise RotateProxy(f"payment-execution blocked ({resp.status_code})")

    if resp.status_code not in (301, 302, 303, 307, 308):
        print(f"  payment-execution: unexpected status {resp.status_code}")
        print(f"  body: {resp.text[:300]}")
        return None

    location = resp.headers.get("location") or resp.headers.get("Location")
    return location


def do_checkout(pm: ProxyManager, product_url: str) -> str | None:
    """
    Full checkout flow. Returns the iDEAL pay.ideal.nl URL or None.
    Retries the checkout flow on proxy errors and refreshes checkout state when
    switching the payment method to iDEAL fails.
    """
    print("\n  Checking out...")

    def checkout_data_ready(checkout_data: dict | None) -> bool:
        if not checkout_data:
            print("  Could not load checkout page")
            return False
        if not checkout_data.get("orderCandidateHash"):
            print("  orderCandidateHash not found -- basket may be empty")
            return False
        if not checkout_data.get("paymentPlanId"):
            print("  paymentPlanId not found -- add a payment method to your bol.com account")
            return False
        return True

    for attempt in range(3):
        try:
            # Step 1: checkout page
            checkout_data = get_checkout_page_data(pm, product_url)
            if not checkout_data_ready(checkout_data):
                return None

            # Step 2: select iDEAL
            selected_ideal = False
            for select_attempt in range(1, IDEAL_SELECTION_ATTEMPTS + 1):
                if select_ideal_payment(pm, checkout_data):
                    selected_ideal = True
                    break

                if select_attempt >= IDEAL_SELECTION_ATTEMPTS:
                    break

                print(
                    f"  iDEAL selection failed ({select_attempt}/{IDEAL_SELECTION_ATTEMPTS}) - "
                    "refreshing checkout state..."
                )
                if IDEAL_SELECTION_RETRY_DELAY_SECONDS > 0:
                    time.sleep(IDEAL_SELECTION_RETRY_DELAY_SECONDS)

                checkout_data = get_checkout_page_data(pm, product_url)
                if not checkout_data_ready(checkout_data):
                    return None

            if not selected_ideal:
                print(f"  iDEAL selection failed after {IDEAL_SELECTION_ATTEMPTS} attempt(s).")
                if attempt < 2:
                    print(f"  Restarting checkout flow ({attempt+1}/3)...")
                    continue
                return None

            if IDEAL_SELECTION_SETTLE_SECONDS > 0:
                time.sleep(IDEAL_SELECTION_SETTLE_SECONDS)

            # Step 3: execute payment plan
            payment_data = execute_payment_plan(pm, checkout_data)
            if not payment_data:
                return None

            # Step 4: get iDEAL URL
            ideal_url = get_ideal_url(pm, checkout_data, payment_data)
            if ideal_url:
                shown = ideal_url.strip()
                if len(shown) > 120:
                    shown = shown[:117] + "..."
                print(f"  Payment URL found: {shown}")
            return ideal_url

        except RotateProxy as e:
            print(f"  Checkout proxy error ({e}) -- backing off and retrying ({attempt + 1}/3)...")
            pm.rotate()
            time.sleep(1.0)
            continue
        except CheckoutFatal as e:
            print(f"  Checkout stopped: {e}")
            return None
        except Exception as e:
            print(f"  Checkout unexpected error: {e}")
            return None

    print("  Checkout failed after 3 proxy attempts.")
    return None


# ─────────────────────────────────────────────
#  MONITOR: one thread per URL
#  All threads share the ProxyManager (thread-safe rotation).
#  stop_event fires when any thread successfully carts + checks out.
# ─────────────────────────────────────────────

def monitor_url(
    pm: ProxyManager,
    product: dict,
    index: int,
    basket_ctx: dict,
    stop_event: threading.Event,
    discord_webhook: str | None,
    discord_thread_id: str | None,
    discord_thread_name: str | None,
) -> None:
    url = product["product_url"]
    offer_uid = product.get("offer_uid")
    product_id = extract_product_id(url)
    if not product_id:
        print(f"  [{index}] Cannot parse productId, skipping: {url}")
        return

    consecutive_page_misses = 0
    consecutive_cart_misses = 0
    PAGE_ROTATE_THRESHOLD = 8
    CART_ROTATE_THRESHOLD = 8
    first_iteration = True
    next_check_delay_seconds = CHECK_DELAY_SECONDS

    while not stop_event.is_set():
        if first_iteration:
            first_iteration = False
        else:
            wait_check_delay(stop_event)
            if stop_event.is_set():
                break
        if not offer_uid:
            new_uid, snap = html_fetch_offer_uid_with_retries(pm, url, product_id)
            h = int(snap.get("http", -1) or -1)
            if h in (403, 429, 503):
                print(f"  [{index}] blocked while fetching offerUid via HTML (HTTP {h}) -- rotating")
                pm.rotate()
                consecutive_page_misses = 0
                continue
            if h not in (200,):
                print(f"  [{index}] HTML offerUid fetch HTTP {h} -- retrying")
                consecutive_page_misses += 1
                if consecutive_page_misses >= PAGE_ROTATE_THRESHOLD:
                    pm.rotate()
                    consecutive_page_misses = 0
                continue

            if snap.get("offline"):
                consecutive_page_misses += 1
                if consecutive_page_misses == 1 or consecutive_page_misses % PAGE_ROTATE_THRESHOLD == 0:
                    print(f"  [{index}] productId={product_id}  HTML indicates product/page not found")
                if consecutive_page_misses >= PAGE_ROTATE_THRESHOLD:
                    pm.rotate()
                    consecutive_page_misses = 0
                continue

            if new_uid:
                consecutive_page_misses = 0
                offer_uid = new_uid
                product["offer_uid"] = offer_uid
                product["offer_uid_locked"] = False
                if update_offer_uid_in_csv(url, offer_uid):
                    print(f"  [{index}] Saved listing id for cart API → {PRODUCTS_FILE}: {offer_uid}")
            else:
                consecutive_page_misses += 1
                if consecutive_page_misses == 1 or consecutive_page_misses % PAGE_ROTATE_THRESHOLD == 0:
                    thin = bool(snap.get("thin_page"))
                    print(
                        f"  [{index}] productId={product_id}  no listing id on PDP "
                        f"(thin={thin}) — need HTML with ?offerUid= / JSON for cart API"
                    )
                if consecutive_page_misses >= PAGE_ROTATE_THRESHOLD:
                    pm.rotate()
                    consecutive_page_misses = 0
                continue

        if (not basket_ctx.get("basket_id")) and (not warm_basket(pm, basket_ctx)):
            # Let the product-page fallback handle basket recovery.
            pass

        if not basket_ctx.get("basket_id"):
            try:
                if populate_basket_ctx_from_product_page(pm, url, basket_ctx):
                    print(f"  [{index}] Recovered basket from product page fallback.")
            except RotateProxy as e:
                print(f"  [{index}] basket fallback blocked ({e}) - rotating")
                pm.rotate()
                continue
            except Exception as e:
                print(f"  [{index}] basket fallback error: {e}")

        if not basket_ctx.get("basket_id"):
            print(f"  [{index}] Basket is not ready yet -- retrying")
            continue

        try:
            cart_resp = add_item_to_cart(pm, url, product_id, offer_uid, basket_ctx)
        except RotateProxy as e:
            print(f"  [{index}] cart blocked ({e}) -- rotating")
            pm.rotate()
            consecutive_cart_misses = 0
            continue
        except ValueError:
            print(f"  [{index}] Basket ID missing -- retrying warm-up")
            basket_ctx.pop("basket_id", None)
            continue
        except Exception as e:
            print(f"  [{index}] cart error: {e}")
            continue

        if not cart_success(cart_resp):
            consecutive_cart_misses += 1
            if consecutive_cart_misses == 1 or consecutive_cart_misses % CART_ROTATE_THRESHOLD == 0:
                print(f"  [{index}] productId={product_id}  offerUid={offer_uid}  -> not carted")
            if consecutive_cart_misses >= CART_ROTATE_THRESHOLD:
                print(f"  [{index}] {CART_ROTATE_THRESHOLD} cart misses -- rotating proxy")
                pm.rotate()
                consecutive_cart_misses = 0
            continue

        item = cart_item_info(cart_resp)
        cart_matched = cart_matches_request(cart_resp, product_id, offer_uid)
        needs_basket_verification = (
            not cart_matched
            or item["product_id"] is None
            or item["offer_uid"] is None
        )
        basket_no_stock = False

        if needs_basket_verification:
            try:
                basket_verified, basket_no_stock = basket_page_item_status(pm, basket_ctx, product_id, offer_uid)
            except RotateProxy as e:
                print(f"  [{index}] basket verify blocked ({e}) - rotating")
                pm.rotate()
                consecutive_cart_misses += 1
                continue
            except Exception as e:
                print(f"  [{index}] basket verify error: {e}")
                basket_verified = False

            if basket_verified:
                cart_matched = True

        if not cart_matched:
            offer_uid_locked = product.get("offer_uid_locked", False)
            print(
                f"  [{index}] cart mismatch - requested productId={product_id}, offerUid={offer_uid}; "
                f"got productId={item['product_id']}, offerUid={item['offer_uid']}"
            )
            if basket_no_stock:
                if offer_uid_locked:
                    consecutive_cart_misses += 1
                    if consecutive_cart_misses == 1 or consecutive_cart_misses % CART_ROTATE_THRESHOLD == 0:
                        print(f"  [{index}] Basket reports NO_STOCK / ITEM_NO_OFFER - CSV offerUid is pinned, keeping it.")
                    if consecutive_cart_misses >= CART_ROTATE_THRESHOLD:
                        pm.rotate()
                        consecutive_cart_misses = 0
                    basket_ctx.pop("page_id", None)
                    continue

                refreshed_uid, rsnap = html_fetch_offer_uid_with_retries(pm, url, product_id)
                rh = int(rsnap.get("http", -1) or -1)
                if rh in (403, 429, 503):
                    print(f"  [{index}] HTML refresh blocked (HTTP {rh}) - rotating")
                    pm.rotate()
                    consecutive_cart_misses += 1
                    continue

                if refreshed_uid and refreshed_uid != offer_uid:
                    product["offer_uid"] = refreshed_uid
                    offer_uid = refreshed_uid
                    if update_offer_uid_in_csv(url, offer_uid):
                        print(
                            f"  [{index}] HTML returned a new active offerUid; "
                            f"updated {PRODUCTS_FILE}: {offer_uid}"
                        )
                    basket_ctx.pop("page_id", None)
                    consecutive_cart_misses += 1
                    continue
                if not refreshed_uid:
                    product["offer_uid"] = None
                    offer_uid = None
                    if update_offer_uid_in_csv(url, None):
                        if rsnap.get("offline"):
                            print(f"  [{index}] HTML refresh indicates product/page not found; cleared cached offerUid.")
                        else:
                            print(
                                f"  [{index}] HTML refresh found no offerUid "
                                f"(thin={bool(rsnap.get('thin_page'))}); cleared cached offerUid."
                            )
                    basket_ctx.pop("page_id", None)
                    consecutive_cart_misses += 1
                    continue

                consecutive_cart_misses += 1
                if consecutive_cart_misses == 1 or consecutive_cart_misses % CART_ROTATE_THRESHOLD == 0:
                    print(
                        f"  [{index}] Basket reports NO_STOCK / ITEM_NO_OFFER - "
                        f"HTML still matches cached offerUid."
                    )
                if consecutive_cart_misses >= CART_ROTATE_THRESHOLD:
                    pm.rotate()
                    consecutive_cart_misses = 0
                basket_ctx.pop("page_id", None)
                continue
            if offer_uid_locked:
                consecutive_cart_misses += 1
                if consecutive_cart_misses == 1 or consecutive_cart_misses % CART_ROTATE_THRESHOLD == 0:
                    print(f"  [{index}] Cart mismatch but CSV offerUid is pinned, keeping it.")
                if consecutive_cart_misses >= CART_ROTATE_THRESHOLD:
                    pm.rotate()
                    consecutive_cart_misses = 0
                basket_ctx.pop("page_id", None)
                continue
            product["offer_uid"] = None
            offer_uid = None
            if update_offer_uid_in_csv(url, None):
                print(f"  [{index}] Cleared stale offerUid from {PRODUCTS_FILE}; will re-scrape it.")
            basket_ctx.pop("page_id", None)
            consecutive_cart_misses += 1
            continue

        consecutive_cart_misses = 0

        price = item["price"] if item["price"] is not None else "?"

        print(f"\n{'='*60}")
        print("  [OK] ADDED TO CART")
        print(f"      Product  : {product_id}")
        print(f"      Listing id (Bol offerUid) : {offer_uid}")
        print(f"      Price    : €{price}")
        print(f"      Basket   : {basket_ctx.get('basket_id')}")
        print(f"{'='*60}")

        send_discord_message(discord_webhook,
            f"✅ Added to cart: {url} (€{price})",
            discord_thread_id, discord_thread_name)

        human_pause_after_add_to_cart()

        # Signal other threads to stop now that we've carted
        stop_event.set()

        # ── Checkout ──────────────────────────────────────────────
        try:
            ideal_url = do_checkout(pm, url)
        except CheckoutFatal as e:
            print(f"  [{index}] FATAL: {e}")
            print("  Stopping bot because checkout session/cookies are no longer valid.")
            send_discord_message(
                discord_webhook,
                f"DEAD COOKIE: {e} | product: {url}",
                discord_thread_id,
                discord_thread_name,
                background=False,
            )
            request_bot_stop(stop_event, str(e))
            return
        except Exception as e:
            print(f"  [{index}] Checkout crashed: {e}")
            ideal_url = None

        if ideal_url:
            print(f"\n{'='*60}")
            print("  [ORDER] ORDER PLACED - PAY HERE:")
            print(f"  {ideal_url}")
            print(f"{'='*60}\n")

            send_discord_message(
                discord_webhook,
                format_discord_payment_block(
                    ideal_url,
                    product_url=url,
                    product_id=str(product_id),
                    offer_uid=str(offer_uid or ""),
                    price=str(price),
                    title=None,
                    seller=item.get("seller_name"),
                ),
                discord_thread_id,
                discord_thread_name,
                background=False,
            )  # blocking -- process exits right after

            save_payment_url(ideal_url, url, product_id, offer_uid, item.get("seller_name"))
        else:
            print("  [WARN] Checkout incomplete - iDEAL URL not received.")
            print("  Check your bol.com account to see if the order was placed.")
            send_discord_message(discord_webhook,
                f"⚠️ Cart succeeded but checkout failed for {url}. Check bol.com account.",
                discord_thread_id, discord_thread_name,
                background=False)

        return   # thread exits after attempt


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

def monitor_url_bol_only(
    pm: ProxyManager,
    product: dict,
    index: int,
    basket_ctx: dict,
    stop_event: threading.Event,
    worker_stop_event: threading.Event | None = None,
    discord_webhook: str | None = None,
    discord_thread_id: str | None = None,
    discord_thread_name: str | None = None,
    session_slot=None,
    session_pool=None,
    session_cursor_ref: list[int] | None = None,
) -> None:
    url = product["product_url"]
    offer_uid = None
    seller_name = None
    seller_id = None
    product_id = extract_product_id(url)
    if not product_id:
        print(f"  [{index}] Cannot parse productId, skipping: {url}")
        return

    consecutive_cart_misses = 0
    PAGE_ROTATE_THRESHOLD = 8
    CART_ROTATE_THRESHOLD = 8
    first_iteration = True
    next_sleep = _monitor_delay_any()
    error_streak = 0
    offline_streak = 0
    last_listed: bool | None = None

    def status(message: str) -> None:
        print(f"  [{index}] productId={product_id}  -> {message}")

    _pool_ref = session_pool

    def bump_session(reason: str) -> None:
        """Hard failure: clear basket only — stay on this product's bol_account pool slot (never hop to another account)."""
        if session_cursor_ref is None or _pool_ref is None:
            return
        n = len(_pool_ref.slots)
        if n < 1:
            return
        idx = session_cursor_ref[0] % n
        sid = _pool_ref.slots[idx].slot_id
        status(f"same session slot {sid} — cleared basket ({reason})")
        basket_ctx["basket_id"] = None
        basket_ctx["page_id"] = None
        basket_ctx["xsrf"] = None

    def rotate_for_proxy_error(exc: BaseException) -> None:
        if session_cursor_ref is not None and _pool_ref is not None and len(_pool_ref.slots) >= 1:
            bump_session(str(exc)[:80])
            return
        if session_slot is not None:
            session_slot.try_recover()
            return
        aggr = any(x in str(exc) for x in ("403", "429", "503", "Blocked"))
        pm.rotate(aggressive_block=aggr)

    while not worker_should_stop(stop_event, worker_stop_event):
        success_count = get_product_success_count(url)
        if success_count >= MAX_SUCCESSFUL_CHECKOUTS_PER_PRODUCT:
            if not product.get("success_limit_announced"):
                status(
                    f"{success_count} successful checkout(s) in this session - stopping monitoring"
                )
                product["success_limit_announced"] = True
            if worker_stop_event is not None:
                worker_stop_event.set()
            return

        if first_iteration:
            first_iteration = False
        else:
            wait_check_delay(stop_event, worker_stop_event, next_sleep)
            if worker_should_stop(stop_event, worker_stop_event):
                break

        pm, session_slot = _monitor_pick_pm_slot(pm, session_pool, session_cursor_ref, session_slot)
        if pm is None:
            status("all session slots DEAD — stopping worker")
            return

        if session_slot is not None and SESSIONS is not None:
            if session_slot.state == SESSIONS.STATE_DEAD:
                status("session slot DEAD — stopping worker")
                return
            if session_slot.state in (SESSIONS.STATE_EXPIRED, SESSIONS.STATE_BLOCKED):
                session_slot.try_recover()
                em = check_login(session_slot.pm, session_slot=session_slot)
                if em:
                    session_slot.on_login_ok(em)
                else:
                    session_slot.on_login_failed()
                    if session_slot.login_failures >= 3:
                        session_slot.mark_dead("recovery: still not logged in after 3 cycle(s)")
                        return
                session_slot.throttled_persist(min_interval=15.0)
                next_sleep = _monitor_delay_any()
                continue
            now = time.time()
            if now - session_slot._last_health_ts > 120.0:
                session_slot._last_health_ts = now
                em = check_login(session_slot.pm, session_slot=session_slot)
                if em:
                    session_slot.on_login_ok(em)
                else:
                    session_slot.on_login_redirect()
                session_slot.throttled_persist()
                if session_slot.state in (SESSIONS.STATE_EXPIRED, SESSIONS.STATE_BLOCKED):
                    next_sleep = _monitor_delay_any()
                    continue

        if not offer_uid:
            http_st, raw_state, detail = html_monitor_probe(
                pm, url, product_id, basket_ctx, index, session_slot=session_slot
            )
            if raw_state == ST_ERROR and detail.get("diag"):
                _diag_probe(f"[{index}] probe diag={detail.get('diag')!r}")

            log_state = raw_state
            if raw_state == ST_OFFLINE and offline_streak + 1 < MONITOR_OFFLINE_CONFIRM:
                log_state = ST_ERROR
            log_monitor_check(index, url, pm, http_st, log_state, session_slot=session_slot)

            if raw_state == ST_BLOCKED:
                if session_cursor_ref is not None and _pool_ref is not None and len(_pool_ref.slots) > 1:
                    bump_session(f"BLOCKED HTTP {http_st}")
                    error_streak = 0
                    offline_streak = 0
                    next_sleep = _monitor_delay_any()
                    continue
                if session_slot is not None:
                    status(f"blocked HTTP {http_st} (sticky session — recovery, no proxy rotate)")
                    send_monitor_debug_discord(
                        discord_webhook,
                        discord_thread_id,
                        discord_thread_name,
                        f"[DEBUG] BLOCKED ({http_st}) session recovery: {url}",
                    )
                else:
                    _diag(f"[{index}] BLOCKED http={http_st} → rotate proxy (soft cooldown)")
                    send_monitor_debug_discord(
                        discord_webhook,
                        discord_thread_id,
                        discord_thread_name,
                        f"[DEBUG] BLOCKED ({http_st}) monitoring: {url}",
                    )
                    status(f"blocked HTTP {http_st}")
                    # Soft cooldown only: hard 403 blocks (2–5 min) exhaust the whole pool when
                    # many lines share one gateway host — logs looked like "same proxy" forever.
                    pm.rotate(aggressive_block=False)
                error_streak = 0
                offline_streak = 0
                next_sleep = _monitor_delay_any()
                continue

            if raw_state == ST_ERROR:
                diag = (detail or {}).get("diag") or ""
                # Thin shell HTML / parse retries stay on THIS slot (same login + basket continuity).
                # Session failover is only for ST_BLOCKED / RotateProxy via bump_session elsewhere.
                if _pdp_lock_same_proxy(pm) and diag in _PDP_THIN_SHELL_DIAGS:
                    error_streak = 0
                    _lo = max(MONITOR_DELAY_ANY_MAX, 8.0)
                    _hi = max(MONITOR_DELAY_OFFLINE_MIN * 0.5, 22.0)
                    if _lo > _hi:
                        _lo, _hi = _hi, _lo
                    next_sleep = float(random.uniform(_lo, _hi))
                    continue
                error_streak += 1
                status(f"probe error (streak {error_streak}/{MONITOR_MAX_ERROR_RETRIES})")
                if session_slot is not None:
                    session_slot.try_recover()
                    if error_streak >= MONITOR_MAX_ERROR_RETRIES:
                        error_streak = 0
                elif error_streak >= MONITOR_MAX_ERROR_RETRIES:
                    if getattr(pm, "_sticky_single", False):
                        _diag(
                            f"[{index}] probe streak max on single sticky proxy — "
                            f"skipping long blacklist (no other line); short backoff only"
                        )
                        pm.rotate(
                            block_seconds=0.0,
                            aggressive_block=False,
                            count_failure=False,
                        )
                    else:
                        lo = min(PROBE_ERROR_BLACKLIST_MIN, PROBE_ERROR_BLACKLIST_MAX)
                        hi = max(PROBE_ERROR_BLACKLIST_MIN, PROBE_ERROR_BLACKLIST_MAX)
                        _diag(
                            f"[{index}] probe streak reached max → blacklist proxy "
                            f"~{lo:.0f}-{hi:.0f}s then rotate"
                        )
                        pm.rotate(
                            block_seconds=float(random.uniform(lo, hi)),
                            aggressive_block=False,
                            count_failure=True,
                        )
                    error_streak = 0
                else:
                    _diag(f"[{index}] probe ERROR streak {error_streak} → instant proxy failover")
                    # Immediate failover: switch proxy without long cooldown on this hop.
                    pm.rotate(block_seconds=0.0, aggressive_block=False, count_failure=False)
                next_sleep = _monitor_delay_any()
                continue

            error_streak = 0

            if raw_state == ST_OFFLINE:
                offline_streak += 1
                if offline_streak < MONITOR_OFFLINE_CONFIRM:
                    status(
                        f"product null (unconfirmed OFFLINE {offline_streak}/"
                        f"{MONITOR_OFFLINE_CONFIRM})"
                    )
                    next_sleep = _monitor_delay_online()
                    continue
                offline_streak = 0
                status("page does not exist (confirmed OFFLINE)")
                if last_listed is True:
                    send_monitor_debug_discord(
                        discord_webhook,
                        discord_thread_id,
                        discord_thread_name,
                        f"[DEBUG] Product OFFLINE: {url}",
                    )
                last_listed = False
                next_sleep = _monitor_delay_offline()
                offer_uid = None
                product["offer_uid"] = None
                continue

            offline_streak = 0

            if raw_state == ST_ONLINE_OOS:
                if last_listed is False:
                    send_monitor_debug_discord(
                        discord_webhook,
                        discord_thread_id,
                        discord_thread_name,
                        f"[DEBUG] Product became ONLINE: {url}",
                    )
                last_listed = True
                status("stock out (listed, no offer)")
                next_sleep = _monitor_delay_online()
                offer_uid = None
                product["offer_uid"] = None
                continue

            graph_offer_uid = detail.get("offer_uid") if raw_state == ST_IN_STOCK else None
            if raw_state == ST_IN_STOCK and not graph_offer_uid:
                status("in stock signal but listing id not parsed yet — retry")
                next_sleep = _monitor_delay_any()
                continue

            if graph_offer_uid == product.get("last_checked_offer_uid"):
                seller_name = product.get("last_checked_seller_name")
                seller_id = product.get("last_checked_seller_id")
                is_bol_offer = bool(product.get("last_checked_is_bol"))
            else:
                seller_name = detail.get("seller_name")
                seller_id = detail.get("seller_id")
                is_bol_offer = bool(detail.get("is_bol"))
                product["last_checked_offer_uid"] = graph_offer_uid
                product["last_checked_seller_name"] = seller_name
                product["last_checked_seller_id"] = seller_id
                product["last_checked_is_bol"] = is_bol_offer

            if not is_bol_offer:
                if product.get("last_skipped_offer_uid") != graph_offer_uid:
                    seller_label = seller_name or seller_id or "unknown"
                    status(f"seller is {seller_label}, skipped")
                    product["last_skipped_offer_uid"] = graph_offer_uid
                offer_uid = None
                product["offer_uid"] = None
                next_sleep = _monitor_delay_online()
                continue

            product["last_skipped_offer_uid"] = None
            offer_uid = graph_offer_uid
            product["offer_uid"] = offer_uid
            if last_listed is False:
                send_monitor_debug_discord(
                    discord_webhook,
                    discord_thread_id,
                    discord_thread_name,
                    f"[DEBUG] Product became ONLINE: {url}",
                )
            last_listed = True
            status("in stock — listing id ok, cart / checkout next")

        if (not basket_ctx.get("basket_id")) and (not warm_basket(pm, basket_ctx)):
            pass

        if not basket_ctx.get("basket_id"):
            try:
                if populate_basket_ctx_from_product_page(pm, url, basket_ctx):
                    print(f"  [{index}] Recovered basket from product page fallback.")
            except RotateProxy as e:
                print(f"  [{index}] basket fallback blocked ({e}) - rotating")
                rotate_for_proxy_error(e)
                next_sleep = _monitor_delay_any()
                continue
            except Exception as e:
                print(f"  [{index}] basket fallback error: {e}")

        if not basket_ctx.get("basket_id"):
            print(f"  [{index}] Basket is not ready yet - retrying")
            next_sleep = _monitor_delay_any()
            continue

        if not acquire_purchase_flow_lock(stop_event, worker_stop_event):
            break

        try:
            try:
                cart_resp = add_item_to_cart(pm, url, product_id, offer_uid, basket_ctx)
            except RotateProxy as e:
                status(f"blocked ({e})")
                rotate_for_proxy_error(e)
                consecutive_cart_misses = 0
                next_sleep = _monitor_delay_any()
                continue
            except ValueError:
                print(f"  [{index}] Basket ID missing - retrying warm-up")
                basket_ctx.pop("basket_id", None)
                next_sleep = _monitor_delay_any()
                continue
            except Exception as e:
                print(f"  [{index}] cart error: {e}")
                next_sleep = _monitor_delay_any()
                continue

            if not cart_success(cart_resp):
                consecutive_cart_misses += 1
                failure_summary = cart_failure_summary(cart_resp)
                if failure_summary:
                    status(f"cart failed: {failure_summary}")
                else:
                    status("cart failed")
                if consecutive_cart_misses >= CART_ROTATE_THRESHOLD:
                    if session_slot is not None:
                        session_slot.try_recover()
                    else:
                        pm.rotate()
                    consecutive_cart_misses = 0
                offer_uid = None
                product["offer_uid"] = None
                next_sleep = _monitor_delay_online()
                continue

            item = cart_item_info(cart_resp)
            cart_matched = cart_matches_request(cart_resp, product_id, offer_uid)
            needs_basket_verification = (
                not cart_matched
                or item["product_id"] is None
                or item["offer_uid"] is None
            )
            basket_no_stock = False

            if needs_basket_verification:
                try:
                    basket_verified, basket_no_stock = basket_page_item_status(
                        pm, basket_ctx, product_id, offer_uid
                    )
                except RotateProxy as e:
                    status(f"blocked ({e})")
                    rotate_for_proxy_error(e)
                    consecutive_cart_misses += 1
                    next_sleep = _monitor_delay_any()
                    continue
                except Exception as e:
                    print(f"  [{index}] basket verify error: {e}")
                    basket_verified = False

                if basket_verified:
                    cart_matched = True

            if not cart_matched:
                if basket_no_stock:
                    status("stock out")
                else:
                    failure_summary = cart_failure_summary(cart_resp)
                    if failure_summary:
                        status(f"cart failed: {failure_summary}")
                    else:
                        status("not carted")
                product["offer_uid"] = None
                offer_uid = None
                seller_name = None
                seller_id = None
                basket_ctx.pop("page_id", None)
                consecutive_cart_misses += 1
                if consecutive_cart_misses >= CART_ROTATE_THRESHOLD:
                    if session_slot is not None:
                        session_slot.try_recover()
                    else:
                        pm.rotate()
                    consecutive_cart_misses = 0
                next_sleep = _monitor_delay_online()
                continue

            consecutive_cart_misses = 0

            price = item["price"] if item["price"] is not None else "?"
            cart_seller_name = item["seller_name"] or seller_name or "unknown"

            print(f"\n{'=' * 60}")
            print("  CARTED")
            print(f"      Product  : {product_id}")
            print(f"      Listing id (Bol offerUid) : {offer_uid}")
            print(f"      Seller   : {cart_seller_name}")
            print(f"      Price    : EUR {price}")
            print(f"      Basket   : {basket_ctx.get('basket_id')}")
            print(f"{'=' * 60}")

            send_discord_message(
                discord_webhook,
                f"Added to cart: {url} (seller: {cart_seller_name}, EUR {price})",
                discord_thread_id,
                discord_thread_name,
            )

            human_pause_after_add_to_cart()

            try:
                ideal_url = do_checkout(pm, url)
            except CheckoutFatal as e:
                print(f"  [{index}] FATAL: {e}")
                print("  Stopping bot because checkout session/cookies are no longer valid.")
                send_discord_message(
                    discord_webhook,
                    f"DEAD COOKIE: {e} | product: {url}",
                    discord_thread_id,
                    discord_thread_name,
                    background=False,
                )
                request_bot_stop(stop_event, str(e))
                return
            except Exception as e:
                print(f"  [{index}] Checkout crashed: {e}")
                ideal_url = None

            if ideal_url:
                print(f"\n{'=' * 60}")
                print("  ORDER PLACED - PAY HERE:")
                print(f"  {ideal_url}")
                print(f"{'=' * 60}\n")

                send_discord_message(
                    discord_webhook,
                    format_discord_payment_block(
                        ideal_url,
                        product_url=url,
                        product_id=str(product_id),
                        offer_uid=str(offer_uid or ""),
                        price=str(price),
                        title=None,
                        seller=cart_seller_name,
                    ),
                    discord_thread_id,
                    discord_thread_name,
                    background=False,
                )
                success_count = save_payment_url(
                    ideal_url, url, product_id, offer_uid, cart_seller_name
                )
                if (
                    success_count is not None
                    and success_count >= MAX_SUCCESSFUL_CHECKOUTS_PER_PRODUCT
                ):
                    status(
                        f"{success_count} successful checkout(s) reached in this session - stopping monitoring"
                    )
                    product["success_limit_announced"] = True
                    if worker_stop_event is not None:
                        worker_stop_event.set()
                    return
            else:
                print("  Checkout incomplete - iDEAL URL not received.")
                print("  Check your bol.com account to see if the order was placed.")
                send_discord_message(
                    discord_webhook,
                    f"Cart succeeded but checkout failed for {url}. Check bol.com account.",
                    discord_thread_id,
                    discord_thread_name,
                    background=False,
                )

            product["offer_uid"] = None
            offer_uid = None
            seller_name = None
            seller_id = None
            basket_ctx["basket_id"] = None
            basket_ctx["page_id"] = None
            basket_ctx["xsrf"] = current_xsrf_token(pm, basket_ctx)
            consecutive_cart_misses = 0
            next_sleep = _monitor_delay_online()
            continue
        finally:
            PURCHASE_FLOW_LOCK.release()


def main():
    # ── Load config ───────────────────────────────────────────────
    cwd = os.getcwd()
    pm, session_pool = _bootstrap_network_clients(cwd)
    _diag_startup_clients(pm, session_pool)
    warm_tries = (
        max(len(session_pool.slots) * 2, 5)
        if session_pool is not None
        else (max(len(pm.proxies), 5) if getattr(pm, "_sticky_single", False) else len(pm.proxies))
    )

    discord_webhook    = load_discord_webhook()
    discord_thread_id, discord_thread_name = load_discord_thread_config()
    if discord_webhook:
        print(f"Discord: webhook loaded" + (f" (thread: {discord_thread_id or discord_thread_name})" if discord_thread_id or discord_thread_name else ""))
    else:
        print(f"Discord: not configured (put URL in {DISCORD_WEBHOOK_FILE})")

    _startup_login_pool_or_legacy(pm, session_pool, warm_tries)

    # ── Load product.csv ──────────────────────────────────────────
    initialize_product_success_counts(PAYMENT_URLS_FILE)
    ensure_products_csv(PRODUCTS_FILE)
    try:
        _initial = load_products(PRODUCTS_FILE)
        sync_sitemap_known_urls_from_csv_urls(
            tuple(p["product_url"] for p in _initial if (p.get("product_url") or "").strip())
        )
    except (OSError, ValueError, FileNotFoundError, csv.Error):
        pass
    print("Press Ctrl+C to stop.\n")

    stop_event = threading.Event()
    if SITEMAP_ENABLED and _bol_sitemap_monitor is not None:
        threading.Thread(
            target=_sitemap_monitor_loop,
            args=(stop_event, discord_webhook, discord_thread_id, discord_thread_name),
            daemon=True,
            name="sitemap-monitor",
        ).start()
        print(
            f"[SITEMAP] Background monitor started (interval={SITEMAP_SCAN_INTERVAL_SECS}s)",
            flush=True,
        )
    elif SITEMAP_ENABLED and _bol_sitemap_monitor is None:
        print("[SITEMAP] monitor.py missing -- install monitor.py to enable sitemap", flush=True)

    active_products: dict[str, dict] = {}
    all_threads: list[threading.Thread] = []
    next_product_slot = 1
    last_snapshot: tuple[str, ...] | None = None
    last_reload_error: str | None = None
    empty_state_announced = False

    interrupted = False
    try:
        while not stop_event.is_set():
            products, reload_error = try_load_products_runtime(PRODUCTS_FILE)

            if reload_error:
                if reload_error != last_reload_error:
                    print(f"Product reload skipped: {reload_error}")
                last_reload_error = reload_error
            else:
                if last_reload_error is not None:
                    print(f"{PRODUCTS_FILE} reload recovered.")
                last_reload_error = None

                snapshot = tuple(product["product_url"] for product in products)
                sync_sitemap_known_urls_from_csv_urls(snapshot)
                if snapshot != last_snapshot:
                    previous_urls = set(active_products)
                    current_urls = set(snapshot)

                    removed_urls = [url for url in list(active_products) if url not in current_urls]
                    for url in removed_urls:
                        control = active_products.pop(url)
                        control["stop_event"].set()
                        print(f"Stopped monitoring removed product: {url}")

                    added_count = 0
                    skipped_limit_count = 0
                    for product in products:
                        url = product["product_url"]
                        if url in previous_urls:
                            continue
                        success_count = get_product_success_count(url)
                        if success_count >= MAX_SUCCESSFUL_CHECKOUTS_PER_PRODUCT:
                            skipped_limit_count += 1
                            print(
                                f"Skipping product with {success_count} successful checkout(s) in this session: {url}"
                            )
                            continue
                        slot_kw = {}
                        if session_pool is not None:
                            pi = _session_pool_slot_index(
                                product,
                                fallback_row_index=next_product_slot - 1,
                                pool_len=len(session_pool.slots),
                            )
                            slot_kw["session_slot"] = session_pool.slot_for_index(pi)
                            slot_kw["session_pool"] = session_pool
                        active_products[url] = _start_product_workers(
                            pm,
                            product,
                            next_product_slot,
                            stop_event,
                            discord_webhook,
                            discord_thread_id,
                            discord_thread_name,
                            all_threads,
                            **slot_kw,
                        )
                        next_product_slot += 1
                        added_count += 1
                        print(f"Started monitoring product: {url}")

                    if last_snapshot is None:
                        if snapshot:
                            print(f"Loaded {len(snapshot)} product(s) from {PRODUCTS_FILE}.")
                            empty_state_announced = False
                        else:
                            print(f"{PRODUCTS_FILE} is empty - waiting for rows.")
                            empty_state_announced = True
                    elif added_count or removed_urls or skipped_limit_count:
                        print(
                            f"Reloaded {PRODUCTS_FILE}: "
                            f"+{added_count} added, -{len(removed_urls)} removed, "
                            f"{skipped_limit_count} skipped at limit."
                        )

                    if snapshot:
                        empty_state_announced = False
                    elif not empty_state_announced:
                        print(f"{PRODUCTS_FILE} is empty - waiting for rows.")
                        empty_state_announced = True

                    last_snapshot = snapshot

            stop_event.wait(PRODUCTS_RELOAD_SECONDS)
    except KeyboardInterrupt:
        interrupted = True
        print("\nCtrl+C received. Stopping bot...")
    finally:
        stop_event.set()
        for control in active_products.values():
            control["stop_event"].set()

    if getattr(stop_event, "reason", None):
        print(f"\nStopping bot. Reason: {stop_event.reason}")
    elif interrupted:
        print("Stopped.")
    return

    try:
        products = load_products(PRODUCTS_FILE)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}"); sys.exit(1)

    if not products:
        print(f"{PRODUCTS_FILE} is empty -- add rows and restart."); sys.exit(1)

    print(f"Loaded {len(products)} product(s) from {PRODUCTS_FILE}.")

    # ── Warm basket once so trigger-time add-to-cart is a single AddItem call ──
    basket_ctx = {
        "basket_id": None,
        "xsrf": current_xsrf_token(pm),
        "page_id": None,
    }
    print("Warming basket...")
    if warm_basket(pm, basket_ctx):
        print(f"  Basket ready: {basket_ctx['basket_id']}")
    elif warm_basket_from_products(pm, products, basket_ctx):
        print(f"  Basket recovered from product page fallback: {basket_ctx['basket_id']}")
    else:
        print("  WARNING: Could not warm basket yet -- threads will retry.")

    print("Press Ctrl+C to stop.\n")

    stop_event = threading.Event()
    threads = [
        threading.Thread(
            target=monitor_url_bol_only,
            args=(
                pm,
                product,
                i + 1,
                basket_ctx,
                stop_event,
                None,
                discord_webhook,
                discord_thread_id,
                discord_thread_name,
                None,
                None,
                None,
            ),
            daemon=True,
        )
        for i, product in enumerate(products)
    ]

    for t in threads:
        t.start()

    for t in threads:
        t.join()

    if stop_event.is_set():
        if getattr(stop_event, "reason", None):
            print(f"\nStopping bot. Reason: {stop_event.reason}")
        else:
            print("\nPurchase complete. Stopping bot.")


def run_parallel_main():
    cwd = os.getcwd()
    pm, session_pool = _bootstrap_network_clients(cwd)
    _diag_startup_clients(pm, session_pool)
    warm_tries = (
        max(len(session_pool.slots) * 2, 5)
        if session_pool is not None
        else (max(len(pm.proxies), 5) if getattr(pm, "_sticky_single", False) else len(pm.proxies))
    )

    discord_webhook = load_discord_webhook()
    discord_thread_id, discord_thread_name = load_discord_thread_config()
    if discord_webhook:
        print(
            "Discord: webhook loaded"
            + (
                f" (thread: {discord_thread_id or discord_thread_name})"
                if discord_thread_id or discord_thread_name else ""
            )
        )
    else:
        print(f"Discord: not configured (put URL in {DISCORD_WEBHOOK_FILE})")

    _startup_login_pool_or_legacy(pm, session_pool, warm_tries)

    initialize_product_success_counts(PAYMENT_URLS_FILE)
    ensure_products_csv(PRODUCTS_FILE)
    try:
        _initial_rp = load_products(PRODUCTS_FILE)
        sync_sitemap_known_urls_from_csv_urls(
            tuple(p["product_url"] for p in _initial_rp if (p.get("product_url") or "").strip())
        )
    except (OSError, ValueError, FileNotFoundError, csv.Error):
        pass
    print("Press Ctrl+C to stop.\n")

    stop_event = threading.Event()
    if SITEMAP_ENABLED and _bol_sitemap_monitor is not None:
        threading.Thread(
            target=_sitemap_monitor_loop,
            args=(stop_event, discord_webhook, discord_thread_id, discord_thread_name),
            daemon=True,
            name="sitemap-monitor",
        ).start()
        print(
            f"[SITEMAP] Background monitor started (interval={SITEMAP_SCAN_INTERVAL_SECS}s)",
            flush=True,
        )
    elif SITEMAP_ENABLED and _bol_sitemap_monitor is None:
        print("[SITEMAP] monitor.py missing -- install monitor.py to enable sitemap", flush=True)

    active_products: dict[str, dict] = {}
    all_threads: list[threading.Thread] = []
    next_product_slot = 1
    last_snapshot: tuple[str, ...] | None = None
    last_reload_error: str | None = None
    empty_state_announced = False

    interrupted = False
    try:
        while not stop_event.is_set():
            products, reload_error = try_load_products_runtime(PRODUCTS_FILE)

            if reload_error:
                if reload_error != last_reload_error:
                    print(f"Product reload skipped: {reload_error}")
                last_reload_error = reload_error
            else:
                if last_reload_error is not None:
                    print(f"{PRODUCTS_FILE} reload recovered.")
                last_reload_error = None

                snapshot = tuple(product["product_url"] for product in products)
                sync_sitemap_known_urls_from_csv_urls(snapshot)
                if snapshot != last_snapshot:
                    previous_urls = set(active_products)
                    current_urls = set(snapshot)

                    removed_urls = [url for url in list(active_products) if url not in current_urls]
                    for url in removed_urls:
                        control = active_products.pop(url)
                        control["stop_event"].set()
                        print(f"Stopped monitoring removed product: {url}")

                    added_count = 0
                    skipped_limit_count = 0
                    for product in products:
                        url = product["product_url"]
                        if url in previous_urls:
                            continue
                        success_count = get_product_success_count(url)
                        if success_count >= MAX_SUCCESSFUL_CHECKOUTS_PER_PRODUCT:
                            skipped_limit_count += 1
                            print(
                                f"Skipping product with {success_count} successful checkout(s) in this session: {url}"
                            )
                            continue
                        slot_kw = {}
                        if session_pool is not None:
                            pi = _session_pool_slot_index(
                                product,
                                fallback_row_index=next_product_slot - 1,
                                pool_len=len(session_pool.slots),
                            )
                            slot_kw["session_slot"] = session_pool.slot_for_index(pi)
                            slot_kw["session_pool"] = session_pool
                        active_products[url] = _start_product_workers(
                            pm,
                            product,
                            next_product_slot,
                            stop_event,
                            discord_webhook,
                            discord_thread_id,
                            discord_thread_name,
                            all_threads,
                            **slot_kw,
                        )
                        next_product_slot += 1
                        added_count += 1
                        print(f"Started monitoring product: {url}")

                    if last_snapshot is None:
                        if snapshot:
                            print(f"Loaded {len(snapshot)} product(s) from {PRODUCTS_FILE}.")
                            empty_state_announced = False
                        else:
                            print(f"{PRODUCTS_FILE} is empty - waiting for rows.")
                            empty_state_announced = True
                    elif added_count or removed_urls or skipped_limit_count:
                        print(
                            f"Reloaded {PRODUCTS_FILE}: "
                            f"+{added_count} added, -{len(removed_urls)} removed, "
                            f"{skipped_limit_count} skipped at limit."
                        )

                    if snapshot:
                        empty_state_announced = False
                    elif not empty_state_announced:
                        print(f"{PRODUCTS_FILE} is empty - waiting for rows.")
                        empty_state_announced = True

                    last_snapshot = snapshot

            stop_event.wait(PRODUCTS_RELOAD_SECONDS)
    except KeyboardInterrupt:
        interrupted = True
        print("\nCtrl+C received. Stopping bot...")
    finally:
        stop_event.set()
        for control in active_products.values():
            control["stop_event"].set()

    if getattr(stop_event, "reason", None):
        print(f"\nStopping bot. Reason: {stop_event.reason}")
    elif interrupted:
        print("Stopped.")
    return

    try:
        products = load_products(PRODUCTS_FILE)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}"); sys.exit(1)

    if not products:
        print(f"{PRODUCTS_FILE} is empty -- add rows and restart."); sys.exit(1)

    print(f"Loaded {len(products)} product(s) from {PRODUCTS_FILE}.")

    total_workers = len(products) * WORKERS_PER_PRODUCT
    if WORKERS_PER_PRODUCT > 1:
        print(
            f"  [!] WORKERS_PER_PRODUCT={WORKERS_PER_PRODUCT}: same URL may be probed in parallel "
            f"-- set to 1 for strict single-loop monitoring.",
            flush=True,
        )
    print("Press Ctrl+C to stop.\n")

    stop_event = threading.Event()
    threads = []
    for product_index, product in enumerate(products, start=1):
        for worker_index in range(1, WORKERS_PER_PRODUCT + 1):
            worker_label = f"{product_index}.{worker_index}"
            worker_basket_ctx = create_worker_basket_ctx()
            threads.append(
                threading.Thread(
                    target=monitor_url_bol_only,
                    args=(
                        pm,
                        product,
                        worker_label,
                        worker_basket_ctx,
                        stop_event,
                        None,
                        discord_webhook,
                        discord_thread_id,
                        discord_thread_name,
                        None,
                        None,
                        None,
                    ),
                    daemon=True,
                )
            )

    for t in threads:
        t.start()

    for t in threads:
        t.join()

    if stop_event.is_set():
        if getattr(stop_event, "reason", None):
            print(f"\nStopping bot. Reason: {stop_event.reason}")
        else:
            print("\nPurchase complete. Stopping bot.")


if __name__ == "__main__":
    import browser_mode

    try:
        browser_mode.run_browser_mode_entry()
    except KeyboardInterrupt:
        print("\nStopped.")
