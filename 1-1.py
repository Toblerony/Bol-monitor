import sys
import subprocess
import json
import os
import re
import time
import base64
import csv
import threading
import urllib.request
import urllib.error
from urllib.parse import unquote, urlencode

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

from curl_cffi.requests import Session


# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────

QUANTITY = 2
PROXY_FILE               = "proxy.txt"
COOKIES_FILE             = "cookies.txt"
PRODUCTS_FILE            = "product.csv"
DISCORD_WEBHOOK_FILE     = "discord_webhook.txt"
DISCORD_THREAD_ID_FILE   = "discord_thread_id.txt"
DISCORD_THREAD_NAME_FILE = "discord_thread_name.txt"
PAYMENT_URLS_FILE        = "payment_urls.txt"
GRAPHQL_URL   = "https://www.bol.com/api/graphql"
CHECKOUT_PAGE_URL = "https://www.bol.com/nl/nl/checkout/?entryPoint=BUY_NOW"
CREATE_BASKET_HASH = "sha256:92b016f96aa83a630f5cc5ebcd48d6da90e155aed1119a492e71856d99e590e0"
ADD_ITEM_HASH = "sha256:fda23bccf49694870747c1a4a5003944bca994020fc3cb05ae9c6cdf029aaa7c"
PRODUCT_BEST_OFFER_HASH = "19a9e78148968e88bb63ef930b33d63b788c66d287ae658c413fe670389bcce4"
RETAILER_INFO_HASH = "sha256:5c82e256f671fb54f6775707b4cf11a857243a01109e10130daf3bb0320cc3d4"
BOL_SELLER_NAME = "bol"
BOL_RETAILER_ID = "0"
WORKERS_PER_PRODUCT = 2
CHECK_DELAY_SECONDS = 3.0
PAGE_DOES_NOT_EXIST_DELAY_SECONDS = 100.0
PRODUCTS_RELOAD_SECONDS = 6.0
MAX_SUCCESSFUL_CHECKOUTS_PER_PRODUCT = 1
IDEAL_SELECTION_ATTEMPTS = 3
IDEAL_SELECTION_RETRY_DELAY_SECONDS = 0.25
IDEAL_SELECTION_SETTLE_SECONDS = 0.25
CSV_HEADERS = ("product_url",)
CSV_LOCK = threading.Lock()
BASKET_LOCK = threading.Lock()
PURCHASE_FLOW_LOCK = threading.Lock()
PAYMENT_URLS_LOCK = threading.Lock()
BASKET_WARMUP_ATTEMPTS = 10
PRODUCT_SUCCESS_COUNTS: dict[str, int] = {}

UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'


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
    "sec-ch-ua":                 '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
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
        "Chrome/124.0.0.0 Safari/537.36"
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
    "sec-ch-ua":          '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile":   "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest":     "empty",
    "sec-fetch-mode":     "cors",
    "sec-fetch-site":     "same-origin",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
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

class RotateProxy(Exception):
    pass


class CheckoutFatal(BaseException):
    pass


def load_proxies(filename: str = PROXY_FILE) -> list:
    if not os.path.exists(filename):
        raise FileNotFoundError(f"{filename} not found.")
    proxies = []
    with open(filename, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(":")
            if len(parts) != 4:
                print(f"  [!] Bad proxy format (host:port:user:pass): {line}")
                continue
            host, port, user, password = parts
            proxies.append(f"http://{user}:{password}@{host}:{port}")
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


# ─────────────────────────────────────────────
#  SESSION
# ─────────────────────────────────────────────

def build_session(cookies: dict, proxy_url: str | None = None, timeout: int = 5) -> Session:
    kwargs = dict(
        impersonate="chrome124",
        allow_redirects=True,
        timeout=timeout,
        headers=PAGE_HEADERS,
        cookies=cookies,
    )
    if proxy_url:
        kwargs["proxies"] = {"http": proxy_url, "https": proxy_url}
    return Session(**kwargs)


# ─────────────────────────────────────────────
#  PROXY MANAGER
# ─────────────────────────────────────────────

class ProxyManager:
    """
    Manages a pool of proxies with per-proxy cooldowns.

    Key design: each thread gets its own dedicated Session via get_thread_session().
    Sessions are NOT shared between threads — this prevents thread [1]'s rotation
    from killing thread [2]'s active connection mid-request.

    Connection reuse: curl_cffi Sessions keep TCP connections alive (HTTP keep-alive)
    so consecutive requests to the same host reuse the same socket. This means the
    product page fetch and the add-to-cart POST both reuse the same connection —
    no extra handshake between them, which is the fastest possible path.
    """
    COOLDOWN_SECS = 60

    def __init__(self, proxies: list, cookies: dict):
        self.proxies       = proxies
        self.cookies       = cookies
        self.lock          = threading.Lock()
        self.blocked_until = {}              # proxy_url -> float timestamp
        self._global_index = 0              # for warm-up / login check
        # Thread-local storage:
        # - current proxy assignment for the thread
        # - per-thread sessions keyed by proxy
        self._thread_local = threading.local()

    def _host(self, proxy_url: str) -> str:
        m = re.search(r'@(.+)$', proxy_url)
        return m.group(1) if m else proxy_url

    def _mark_blocked(self, proxy_url: str):
        self.blocked_until[proxy_url] = time.time() + self.COOLDOWN_SECS

    def _is_blocked(self, proxy_url: str) -> bool:
        return time.time() < self.blocked_until.get(proxy_url, 0)

    def _next_available(self, after_index: int) -> tuple[int, str]:
        """Return (index, proxy_url) of the next proxy not in cooldown."""
        n = len(self.proxies)
        for offset in range(1, n + 1):
            idx = (after_index + offset) % n
            p = self.proxies[idx]
            if not self._is_blocked(p):
                return idx, p
        # All in cooldown — return the one expiring soonest
        idx = min(range(n), key=lambda i: self.blocked_until.get(self.proxies[i], 0))
        return idx, self.proxies[idx]

    # ── Per-thread proxy assignment ─────────────────────────────

    def get_thread_proxy(self) -> str:
        """Returns this thread's current proxy, assigning one if not yet set."""
        if not hasattr(self._thread_local, "proxy"):
            with self.lock:
                idx, proxy = self._next_available(self._global_index)
                self._global_index = idx
            self._thread_local.proxy = proxy
        return self._thread_local.proxy

    def rotate(self):
        """Puts this thread's current proxy in cooldown and picks the next one."""
        current = getattr(self._thread_local, "proxy", self.proxies[0])
        with self.lock:
            self._mark_blocked(current)
            current_idx = self.proxies.index(current) if current in self.proxies else 0
            _, new_proxy = self._next_available(current_idx)
        self._thread_local.proxy = new_proxy
        #print(f"  [→] Rotated → {self._host(new_proxy)}")

    def _get_session(self, proxy: str, checkout: bool = False) -> Session:
        sessions = getattr(self._thread_local, "sessions", None)
        if sessions is None:
            sessions = {}
            self._thread_local.sessions = sessions
        if proxy not in sessions:
            sessions[proxy] = build_session(self.cookies, proxy, timeout=8)
        return sessions[proxy]

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

    def get(self, url, checkout=False, **kw):
        proxy = self.get_thread_proxy()
        try:
            self._log_request("GET", proxy, checkout, url, kw)
            r = self._get_session(proxy, checkout).get(url, **kw)
            self._check(r)
            return r
        except RotateProxy:
            raise
        except Exception as e:
            raise RotateProxy(f"GET failed: {e}")

    def post(self, url, checkout=False, **kw):
        proxy = self.get_thread_proxy()
        try:
            self._log_request("POST", proxy, checkout, url, kw)
            r = self._get_session(proxy, checkout).post(url, **kw)
            self._check(r)
            return r
        except RotateProxy:
            raise
        except Exception as e:
            raise RotateProxy(f"POST failed: {e}")

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


def _write_products_csv_rows(filename: str, urls: list[str]) -> None:
    with open(filename, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        writer.writeheader()
        for url in urls:
            writer.writerow({"product_url": url})


def ensure_products_csv(filename: str = PRODUCTS_FILE) -> None:
    legacy_filename = os.path.join(os.path.dirname(filename), "products.txt")
    if os.path.exists(filename):
        with open(filename, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames or []
            if fieldnames:
                url_field = _product_csv_url_field(fieldnames)
                urls = []
                for row in reader:
                    url = (row.get(url_field) or "").strip()
                    if url:
                        urls.append(url)
                if tuple(fieldnames) != CSV_HEADERS:
                    _write_products_csv_rows(filename, urls)
                    print(f"Normalized {PRODUCTS_FILE} to header: product_url")
                return

        _write_products_csv_rows(filename, [])
        print(f"Reset empty {PRODUCTS_FILE} with header: product_url")
        return

    urls = []
    if os.path.exists(legacy_filename):
        with open(legacy_filename, "r", encoding="utf-8") as f:
            urls = [line.strip() for line in f if line.strip().startswith("http")]

    _write_products_csv_rows(filename, urls)
    if urls:
        print(f"Migrated {len(urls)} product(s) from products.txt to {PRODUCTS_FILE}.")
    else:
        print(f"Created empty {PRODUCTS_FILE} with header: product_url")


def load_products(filename: str = PRODUCTS_FILE) -> list[dict]:
    if not os.path.exists(filename):
        raise FileNotFoundError(f"{filename} not found.")

    with open(filename, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        url_field = _product_csv_url_field(reader.fieldnames)
        products = []
        seen_urls = set()
        for row in reader:
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
            products.append({
                "product_url": url,
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
        with open(PAYMENT_URLS_FILE, "a", encoding="utf-8") as f:
            f.write(line)
        print(f"  Appended payment URL to {PAYMENT_URLS_FILE}")
        return record_product_success(product_url)
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
) -> dict:
    product_stop_event = threading.Event()
    threads = []

    for worker_index in range(1, WORKERS_PER_PRODUCT + 1):
        worker_label = f"{product_slot}.{worker_index}"
        worker_basket_ctx = create_worker_basket_ctx()
        thread = threading.Thread(
            target=monitor_url_bol_only,
            args=(
                pm,
                product,
                worker_label,
                worker_basket_ctx,
                stop_event,
                product_stop_event,
                discord_webhook,
                discord_thread_id,
                discord_thread_name,
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

def check_login(pm: ProxyManager) -> str | None:
    """
    Checks login by fetching the orders page.
    - Logged in  → title contains "Bestellingen"
    - Logged out → title contains "Inloggen" (redirected to login page)
    This is the most reliable method — confirmed by user testing.
    """
    try:
        resp = pm.get(
            "https://www.bol.com/nl/nl/account/bestellingen/overzicht/",
            headers={**PAGE_HEADERS_SAME_ORIGIN, "referer": "https://www.bol.com/nl/nl/"},
        )
        html = resp.text
        if re.search(r'<title[^>]*>[^<]*Bestellingen[^<]*</title>', html, re.IGNORECASE):
            # Extract name from page if possible
            name_m = re.search(r'Hallo[,\s]+([A-Za-z]+)', html)
            name = name_m.group(1) if name_m else "user"
            return name
        if re.search(r'<title[^>]*>[^<]*Inloggen[^<]*</title>', html, re.IGNORECASE):
            return None  # redirected to login — not authenticated
        # Unexpected page — check for any logged-in indicator
        if re.search(r'/account/bestellingen|uitloggen', html, re.IGNORECASE):
            return "user"
        return None
    except Exception as e:
        print(f"  Login check error: {e}")
        return None


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


def fetch_product_offer_graphql(
    pm: ProxyManager,
    product_id: str,
    product_url: str,
    basket_ctx: dict | None = None,
) -> dict | None:
    resp = pm.post(
        GRAPHQL_URL,
        json={
            "extensions": {"persistedQuery": {"sha256Hash": PRODUCT_BEST_OFFER_HASH, "version": 1}},
            "operationName": "Product",
            "variables": {"productId": product_id},
        },
        headers=_product_graphql_headers(pm, product_url, "Product", basket_ctx),
    )
    try:
        data = resp.json()
    except Exception:
        return None

    product = data.get("data", {}).get("product")
    if not isinstance(product, dict):
        return {
            "product_exists": False,
            "offer_uid": None,
            "best_selling_offer": None,
            "response": data,
        }

    best_selling_offer = product.get("bestSellingOffer")
    offer_uid = best_selling_offer.get("offerUid") if isinstance(best_selling_offer, dict) else None
    return {
        "product_exists": True,
        "offer_uid": offer_uid,
        "best_selling_offer": best_selling_offer if isinstance(best_selling_offer, dict) else None,
        "response": data,
    }


def fetch_offer_seller_graphql(
    pm: ProxyManager,
    offer_uid: str,
    product_url: str,
    basket_ctx: dict | None = None,
) -> dict | None:
    resp = pm.post(
        GRAPHQL_URL,
        json={
            "operationName": "retailerInfo",
            "extensions": {"persistedQuery": {"sha256Hash": RETAILER_INFO_HASH, "version": 1}},
            "variables": {"offerUid": offer_uid},
        },
        headers=_product_graphql_headers(pm, product_url, "retailerInfo", basket_ctx),
    )
    try:
        data = resp.json()
    except Exception:
        return None

    selling_offer = data.get("data", {}).get("sellingOffer")
    retailer = selling_offer.get("retailer") if isinstance(selling_offer, dict) else None
    return {
        "offer_exists": isinstance(selling_offer, dict),
        "seller_name": retailer.get("name") if isinstance(retailer, dict) else None,
        "seller_id": str(retailer.get("id")) if isinstance(retailer, dict) and retailer.get("id") is not None else None,
        "retailer": retailer if isinstance(retailer, dict) else None,
        "response": data,
    }


def is_bol_seller(seller_name: str | None, seller_id: str | None) -> bool:
    normalized_name = (seller_name or "").strip().casefold()
    normalized_id = (seller_id or "").strip()
    return normalized_name == BOL_SELLER_NAME or normalized_id == BOL_RETAILER_ID


def fetch_page_data(pm: ProxyManager, url: str) -> tuple:
    """Returns (offer_uid, basket_id, xsrf, page_id)."""
    resp = pm.get(url, headers={**PAGE_HEADERS_SAME_ORIGIN, "referer": "https://www.bol.com/nl/nl/"})
    if resp.status_code != 200:
        return None, None, None, None

    html = resp.text

    # Validate the HTML is a real bol.com product page.
    # A genuine page is always 200KB+ and contains the buy block slot.
    # A stripped CDN/cached page is small and missing these markers.
    if len(html) < 100_000 or 'buyBlockSlot' not in html:
        raise RotateProxy(f"bad HTML ({len(html):,} bytes, missing buy block)")

    # offerUid — in href of add-to-cart links: ?offerUid=<uuid>
    offer_uid = None
    m = re.search(r'[?&](?:amp;)?offerUid=(' + UUID + r')', html, re.IGNORECASE)
    if m:
        offer_uid = m.group(1)

    # basketId — \"Basket\",\"uuid\" in Remix dehydrated state
    basket_id = _extract_dehydrated(html, "Basket")

    # xsrf — \"xsrf\",\"uuid\"
    xsrf = _extract_dehydrated(html, "xsrf") or pm.session.cookies.get("XSRF-TOKEN")

    # pageId — \"pageId\",\"uuid\"
    page_id = _extract_dehydrated(html, "pageId")

    return offer_uid, basket_id, xsrf, page_id


# ─────────────────────────────────────────────
#  STEP 2: ADD TO CART
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


def add_item_to_cart(
    pm: ProxyManager,
    product_url: str,
    product_id: str,
    offer_uid: str,
    basket_ctx: dict,
) -> dict:
    basket_id = basket_ctx.get("basket_id")
    if not basket_id:
        raise ValueError("Basket ID is not ready.")

    xsrf = current_xsrf_token(pm, basket_ctx)
    page_id = basket_ctx.get("page_id")

    resp = pm.post(
        GRAPHQL_URL,
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

    # hash (=orderCandidateHash) — \"hash\",\"<32hex>\"
    order_hash = None
    m = re.search(r'\\\"hash\\\"[,\s]*\\\"([a-f0-9]{32})\\\"', html)
    if not m:
        m = re.search(r'"hash"\s*[,:]\s*"([a-f0-9]{32})"', html)
    if m:
        order_hash = m.group(1)

    # paymentPlanId — \"PaymentOffering\",\"<digits>\"
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

    # rsSessionId — use current timestamp as reliable fallback
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

    try:
        # Use checkout_session directly here (longer timeout, already has cookies)
        # but wrap in try/except since we can't use pm.post with allow_redirects=False
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
    except Exception as e:
        raise RotateProxy(f"payment-execution failed: {e}")

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
            print("  orderCandidateHash not found — basket may be empty")
            return False
        if not checkout_data.get("paymentPlanId"):
            print("  paymentPlanId not found — add a payment method to your bol.com account")
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
            return get_ideal_url(pm, checkout_data, payment_data)

        except RotateProxy as e:
            print(f"  Checkout proxy error ({e}) — rotating and retrying ({attempt+1}/3)...")
            pm.rotate()
            continue
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
            gql_offer_data = None
            try:
                gql_offer_data = fetch_product_offer_graphql(pm, product_id, url, basket_ctx)
            except RotateProxy as e:
                print(f"  [{index}] blocked while fetching offerUid via GraphQL ({e}) — rotating")
                pm.rotate()
                consecutive_page_misses = 0
                continue
            except Exception as e:
                print(f"  [{index}] GraphQL offerUid fetch error: {e}")
                consecutive_page_misses += 1
                if consecutive_page_misses >= PAGE_ROTATE_THRESHOLD:
                    pm.rotate()
                    consecutive_page_misses = 0
                continue

            graph_offer_uid = gql_offer_data.get("offer_uid")
            if graph_offer_uid:
                consecutive_page_misses = 0
                offer_uid = graph_offer_uid
                product["offer_uid"] = offer_uid
                product["offer_uid_locked"] = False
                if update_offer_uid_in_csv(url, offer_uid):
                    print(f"  [{index}] Saved offerUid to {PRODUCTS_FILE}: {offer_uid}")
            else:
                consecutive_page_misses += 1
                if consecutive_page_misses == 1 or consecutive_page_misses % PAGE_ROTATE_THRESHOLD == 0:
                    if gql_offer_data.get("product_exists"):
                        print(f"  [{index}] productId={product_id}  bestSellingOffer=None  → unavailable")
                    else:
                        print(f"  [{index}] productId={product_id}  GraphQL product not found  → unavailable")
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
            print(f"  [{index}] Basket is not ready yet — retrying")
            continue

        try:
            cart_resp = add_item_to_cart(pm, url, product_id, offer_uid, basket_ctx)
        except RotateProxy as e:
            print(f"  [{index}] cart blocked ({e}) — rotating")
            pm.rotate()
            consecutive_cart_misses = 0
            continue
        except ValueError:
            print(f"  [{index}] Basket ID missing — retrying warm-up")
            basket_ctx.pop("basket_id", None)
            continue
        except Exception as e:
            print(f"  [{index}] cart error: {e}")
            continue

        if not cart_success(cart_resp):
            consecutive_cart_misses += 1
            if consecutive_cart_misses == 1 or consecutive_cart_misses % CART_ROTATE_THRESHOLD == 0:
                print(f"  [{index}] productId={product_id}  offerUid={offer_uid}  → not carted")
            if consecutive_cart_misses >= CART_ROTATE_THRESHOLD:
                print(f"  [{index}] {CART_ROTATE_THRESHOLD} cart misses — rotating proxy")
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

                refreshed_offer_data = None
                try:
                    refreshed_offer_data = fetch_product_offer_graphql(pm, product_id, url, basket_ctx)
                except RotateProxy as e:
                    print(f"  [{index}] GraphQL refresh blocked ({e}) - rotating")
                    pm.rotate()
                    consecutive_cart_misses += 1
                    continue
                except Exception as e:
                    print(f"  [{index}] GraphQL refresh error: {e}")

                if refreshed_offer_data is not None:
                    refreshed_offer_uid = refreshed_offer_data.get("offer_uid")
                    if refreshed_offer_uid and refreshed_offer_uid != offer_uid:
                        product["offer_uid"] = refreshed_offer_uid
                        offer_uid = refreshed_offer_uid
                        if update_offer_uid_in_csv(url, offer_uid):
                            print(f"  [{index}] GraphQL returned a new active offerUid; updated {PRODUCTS_FILE}: {offer_uid}")
                        basket_ctx.pop("page_id", None)
                        consecutive_cart_misses += 1
                        continue
                    if not refreshed_offer_uid:
                        product["offer_uid"] = None
                        offer_uid = None
                        if update_offer_uid_in_csv(url, None):
                            if refreshed_offer_data.get("product_exists"):
                                print(f"  [{index}] GraphQL confirms no active bestSellingOffer; cleared cached offerUid.")
                            else:
                                print(f"  [{index}] GraphQL product not found; cleared cached offerUid.")
                        basket_ctx.pop("page_id", None)
                        consecutive_cart_misses += 1
                        continue

                consecutive_cart_misses += 1
                if consecutive_cart_misses == 1 or consecutive_cart_misses % CART_ROTATE_THRESHOLD == 0:
                    print(f"  [{index}] Basket reports NO_STOCK / ITEM_NO_OFFER - GraphQL still matches cached offerUid.")
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
        print(f"  ✅  ADDED TO CART")
        print(f"      Product  : {product_id}")
        print(f"      offerUid : {offer_uid}")
        print(f"      Price    : €{price}")
        print(f"      Basket   : {basket_ctx.get('basket_id')}")
        print(f"{'='*60}")

        send_discord_message(discord_webhook,
            f"✅ Added to cart: {url} (€{price})",
            discord_thread_id, discord_thread_name)

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
            print(f"  🎉  ORDER PLACED — PAY HERE:")
            print(f"  {ideal_url}")
            print(f"{'='*60}\n")

            send_discord_message(discord_webhook,
                f"🎉 Pay here: {ideal_url}",
                discord_thread_id, discord_thread_name,
                background=False)   # blocking — process exits right after

            try:
                with open("payment_url.txt", "w") as f:
                    f.write(ideal_url + "\n")
                print("  Saved to payment_url.txt")
            except Exception as e:
                print(f"  Could not save payment_url.txt: {e}")
        else:
            print("  ⚠️  Checkout incomplete — iDEAL URL not received.")
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
) -> None:
    url = product["product_url"]
    offer_uid = None
    seller_name = None
    seller_id = None
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

    def status(message: str) -> None:
        print(f"  [{index}] productId={product_id}  -> {message}")

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
            wait_check_delay(stop_event, worker_stop_event, next_check_delay_seconds)
            if worker_should_stop(stop_event, worker_stop_event):
                break
        if not offer_uid:
            try:
                gql_offer_data = fetch_product_offer_graphql(pm, product_id, url, basket_ctx)
            except RotateProxy as e:
                status(f"blocked ({e})")
                pm.rotate()
                consecutive_page_misses = 0
                continue
            except Exception as e:
                print(f"  [{index}] GraphQL offer lookup error: {e}")
                consecutive_page_misses += 1
                if consecutive_page_misses >= PAGE_ROTATE_THRESHOLD:
                    pm.rotate()
                    consecutive_page_misses = 0
                continue

            graph_offer_uid = gql_offer_data.get("offer_uid")
            product_exists = bool(gql_offer_data.get("product_exists"))
            next_check_delay_seconds = (
                CHECK_DELAY_SECONDS
                if product_exists
                else PAGE_DOES_NOT_EXIST_DELAY_SECONDS
            )
            if not graph_offer_uid:
                consecutive_page_misses += 1
                if product_exists:
                    status("stock out")
                else:
                    status("page does not exist")
                if consecutive_page_misses >= PAGE_ROTATE_THRESHOLD:
                    pm.rotate()
                    consecutive_page_misses = 0
                continue

            consecutive_page_misses = 0
            if graph_offer_uid == product.get("last_checked_offer_uid"):
                seller_name = product.get("last_checked_seller_name")
                seller_id = product.get("last_checked_seller_id")
                is_bol_offer = bool(product.get("last_checked_is_bol"))
            else:
                try:
                    seller_data = fetch_offer_seller_graphql(pm, graph_offer_uid, url, basket_ctx)
                except RotateProxy as e:
                    status(f"blocked ({e})")
                    pm.rotate()
                    consecutive_page_misses = 0
                    continue
                except Exception as e:
                    print(f"  [{index}] seller lookup error for offerUid={graph_offer_uid}: {e}")
                    consecutive_page_misses += 1
                    if consecutive_page_misses >= PAGE_ROTATE_THRESHOLD:
                        pm.rotate()
                        consecutive_page_misses = 0
                    continue

                if not seller_data or not seller_data.get("offer_exists"):
                    consecutive_page_misses += 1
                    status("stock out")
                    if consecutive_page_misses >= PAGE_ROTATE_THRESHOLD:
                        pm.rotate()
                        consecutive_page_misses = 0
                    continue

                seller_name = seller_data.get("seller_name")
                seller_id = seller_data.get("seller_id")
                is_bol_offer = is_bol_seller(seller_name, seller_id)
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
                continue

            product["last_skipped_offer_uid"] = None
            offer_uid = graph_offer_uid
            product["offer_uid"] = offer_uid

        if (not basket_ctx.get("basket_id")) and (not warm_basket(pm, basket_ctx)):
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
            print(f"  [{index}] Basket is not ready yet - retrying")
            continue

        if not acquire_purchase_flow_lock(stop_event, worker_stop_event):
            break

        try:
            try:
                cart_resp = add_item_to_cart(pm, url, product_id, offer_uid, basket_ctx)
            except RotateProxy as e:
                status(f"blocked ({e})")
                pm.rotate()
                consecutive_cart_misses = 0
                continue
            except ValueError:
                print(f"  [{index}] Basket ID missing - retrying warm-up")
                basket_ctx.pop("basket_id", None)
                continue
            except Exception as e:
                print(f"  [{index}] cart error: {e}")
                continue

            if not cart_success(cart_resp):
                consecutive_cart_misses += 1
                failure_summary = cart_failure_summary(cart_resp)
                if failure_summary:
                    status(f"cart failed: {failure_summary}")
                else:
                    status("cart failed")
                if consecutive_cart_misses >= CART_ROTATE_THRESHOLD:
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
                    status(f"blocked ({e})")
                    pm.rotate()
                    consecutive_cart_misses += 1
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
                    pm.rotate()
                    consecutive_cart_misses = 0
                continue

            consecutive_cart_misses = 0

            price = item["price"] if item["price"] is not None else "?"
            cart_seller_name = item["seller_name"] or seller_name or "unknown"

            print(f"\n{'=' * 60}")
            print("  CARTED")
            print(f"      Product  : {product_id}")
            print(f"      offerUid : {offer_uid}")
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
                    f"Pay here: {ideal_url}",
                    discord_thread_id,
                    discord_thread_name,
                    background=False,
                )
                success_count = save_payment_url(ideal_url, url, product_id, offer_uid, cart_seller_name)
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
            consecutive_page_misses = 0
            consecutive_cart_misses = 0
            continue
        finally:
            PURCHASE_FLOW_LOCK.release()


def main():
    # ── Load config ───────────────────────────────────────────────
    try:
        cookies = load_cookies(os.path.join(os.getcwd(), COOKIES_FILE))
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}"); sys.exit(1)

    try:
        proxies = load_proxies(os.path.join(os.getcwd(), PROXY_FILE))
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}"); sys.exit(1)

    pm = ProxyManager(proxies, cookies)

    discord_webhook    = load_discord_webhook()
    discord_thread_id, discord_thread_name = load_discord_thread_config()
    if discord_webhook:
        print(f"Discord: webhook loaded" + (f" (thread: {discord_thread_id or discord_thread_name})" if discord_thread_id or discord_thread_name else ""))
    else:
        print(f"Discord: not configured (put URL in {DISCORD_WEBHOOK_FILE})")

    # ── Warm-up: find a working proxy ─────────────────────────────
    warmed = False
    for attempt in range(len(proxies)):
        try:
            r = pm.get("https://www.bol.com/nl/nl/", headers=PAGE_HEADERS)
            warmed = True
            break
        except RotateProxy as e:
            print(f"  Proxy {attempt+1} blocked ({e}), rotating...")
            pm.rotate()
        except Exception as e:
            print(f"  Proxy {attempt+1} error ({e}), rotating...")
            pm.rotate()
    if not warmed:
        print("  WARNING: All proxies blocked on warm-up — continuing anyway")

    # ── Login check ───────────────────────────────────────────────
    email = check_login(pm)
    if email:
        print(f"Logged in as: {email}")
    else:
        print("  NOT logged in or login check failed.")
        print("  Re-export cookies.txt from bol.com while logged in.")
        print("  Continuing anyway — cart will fail if truly not logged in.")

    # ── Load product.csv ──────────────────────────────────────────
    initialize_product_success_counts(PAYMENT_URLS_FILE)
    initialize_product_success_counts(PAYMENT_URLS_FILE)
    initialize_product_success_counts(PAYMENT_URLS_FILE)
    initialize_product_success_counts(PAYMENT_URLS_FILE)
    initialize_product_success_counts(PAYMENT_URLS_FILE)
    ensure_products_csv(PRODUCTS_FILE)
    print("Press Ctrl+C to stop.\n")

    stop_event = threading.Event()
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
                        active_products[url] = _start_product_workers(
                            pm,
                            product,
                            next_product_slot,
                            stop_event,
                            discord_webhook,
                            discord_thread_id,
                            discord_thread_name,
                            all_threads,
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
        print(f"{PRODUCTS_FILE} is empty — add rows and restart."); sys.exit(1)

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
        print("  WARNING: Could not warm basket yet — threads will retry.")

    print("Press Ctrl+C to stop.\n")

    stop_event = threading.Event()
    threads = [
        threading.Thread(
            target=monitor_url_bol_only,
            args=(pm, product, i + 1, basket_ctx, stop_event,
                  discord_webhook, discord_thread_id, discord_thread_name),
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
    try:
        cookies = load_cookies(os.path.join(os.getcwd(), COOKIES_FILE))
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}"); sys.exit(1)

    try:
        proxies = load_proxies(os.path.join(os.getcwd(), PROXY_FILE))
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}"); sys.exit(1)

    pm = ProxyManager(proxies, cookies)

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

    warmed = False
    for attempt in range(len(proxies)):
        try:
            r = pm.get("https://www.bol.com/nl/nl/", headers=PAGE_HEADERS)
            warmed = True
            break
        except RotateProxy as e:
            print(f"  Proxy {attempt+1} blocked ({e}), rotating...")
            pm.rotate()
        except Exception as e:
            print(f"  Proxy {attempt+1} error ({e}), rotating...")
            pm.rotate()
    if not warmed:
        print("  WARNING: All proxies blocked on warm-up â€” continuing anyway")

    email = check_login(pm)
    if email:
        print(f"Logged in as: {email}")
    else:
        print("  âš ï¸  NOT logged in or login check failed.")
        print("  Re-export cookies.txt from bol.com while logged in.")
        print("  Continuing anyway â€” cart will fail if truly not logged in.")

    ensure_products_csv(PRODUCTS_FILE)
    print("Press Ctrl+C to stop.\n")

    stop_event = threading.Event()
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
                        active_products[url] = _start_product_workers(
                            pm,
                            product,
                            next_product_slot,
                            stop_event,
                            discord_webhook,
                            discord_thread_id,
                            discord_thread_name,
                            all_threads,
                        )
                        next_product_slot += 1
                        added_count += 1

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
        print(f"{PRODUCTS_FILE} is empty â€” add rows and restart."); sys.exit(1)

    print(f"Loaded {len(products)} product(s) from {PRODUCTS_FILE}.")

    total_workers = len(products) * WORKERS_PER_PRODUCT
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
                        discord_webhook,
                        discord_thread_id,
                        discord_thread_name,
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
    try:
        run_parallel_main()
    except KeyboardInterrupt:
        print("\nStopped.")
