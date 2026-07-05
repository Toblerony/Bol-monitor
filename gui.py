#!/usr/bin/env python3
"""
Bol monitor control panel: edit product.csv, proxy.txt, Discord files;
optional keywords; simple settings; Start/Stop with logs.
"""
from __future__ import annotations

import contextlib
import csv
import io
import json
import os
import queue
import re
import subprocess
import sys
import threading
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

SCRIPT_DIR = Path(__file__).resolve().parent
BOT_SCRIPT = SCRIPT_DIR / "1.py"

_LOG_SLOT_TAG_RE = re.compile(r"^\[S([1-9]\d*)\]")
_LOG_SETUP_BOL_ACCOUNT_RE = re.compile(r"^\[SETUP\].*?\bbol_account=(\d+)\b")
_LOG_BRACKET_S_ANYWHERE_RE = re.compile(r"\[S([1-9]\d*)\]")
_LOG_SESSION_JSON_RE = re.compile(r"\bsession_([1-9]\d*)\.json\b")


def _dotenv_file_truthy(key: str, default_false: bool = True) -> bool:
    """Read SCRIPT_DIR/.env for ``key`` (first match). Used so GUI log routing matches browser defaults."""
    path = SCRIPT_DIR / ".env"
    if not path.is_file():
        return not default_false
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return not default_false
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() != key:
            continue
        return v.strip().strip('"').strip("'").lower() in ("1", "true", "yes", "on")
    return not default_false

PRODUCTS_PATH = SCRIPT_DIR / "product.csv"
PROXY_PATH = SCRIPT_DIR / "proxy.txt"
SESSION_DIR = SCRIPT_DIR / "cookies"
DISCORD_WEBHOOK_PATH = SCRIPT_DIR / "discord_webhook.txt"
DISCORD_THREAD_ID_PATH = SCRIPT_DIR / "discord_thread_id.txt"
DISCORD_THREAD_NAME_PATH = SCRIPT_DIR / "discord_thread_name.txt"
EXTRA_KEYWORDS_PATH = SCRIPT_DIR / "sitemap_extra_keywords.txt"
GUI_SETTINGS_PATH = SCRIPT_DIR / "gui_settings.json"

APP_BG = "#e8eaed"
CARD_BG = "#ffffff"
INNER_BG = "#f3f4f6"
MONITOR_TAB_UNSEL_BG = "#cbd5e1"
MONITOR_TAB_SEL_BG = "#2563eb"
MONITOR_TAB_UNSEL_FG = "#0f172a"
MONITOR_TAB_SEL_FG = "#ffffff"
ACCENT_BLUE = "#2563eb"
ACCENT_RED = "#dc2626"
ACCENT_GRAY = "#6b7280"

HOW_IT_WORKS_TEXT = """HOW IT WORKS

FILES (same folder as gui.py / 1.py)
  • product.csv — Up to 3 product URLs (Products tab); each row picks a Bol login (**Email1…Email3**). Rows may share the
    same account if you want — **bol_account N** always means proxy.txt **line N** + **session_N.json** + **EmailN**, no matter
    whether that row is first or third in the list.
  • proxy.txt — One host:port:user:pass per line. Line N is the tunnel for bol_account N (default: **only** that line —
    no auto-switch to other rows; product URL in CSV does not change which line is used). Optional:
    BOL_BROWSER_ALLOW_PROXY_FAILOVER_OTHER_LINES=1 lets browser mode rotate through other lines after login loss.
  • cookies/session_<n>.json + session_<n>.fingerprint.txt + session_<n>.binding.json — Saved login per **bol_account**;
    fingerprint matches **that account's tunnel** (proxy line N); binding stores product URL + bol_account +
    a hash of that row's Email/Password from .env — change CSV row, login choice, **or** those .env values and Start
    clears that session so you log in again (sticky PDP / proxy binding unchanged).
  • .env — Email1/Password1 … Email3/Password3 (or legacy Email/Password for account 1). Optional browser-only vars
    (homepage/CMP/login tuning) live here; see browser_mode.py env names if you need them.
  • discord_webhook.txt (+ optional thread fields) — Alerts.

SLOT BINDING (browser bot)
  • **Parallel or single — same rule:** the Bol login you pick on each product row (**Email1 / Email2 / Email3**)
    writes **bol_account** into product.csv. That row’s PDP opens only in **session_N.json + proxy line N + EmailN**
    — **N is the account you chose, not the row number.** Parallel runs sort worker windows by bol_account (account 1 left,
    account 3 right) so the window matches **S1 / S2 / S3**. Put **BOL_BROWSER_WORKERS_CSV_ROW_ORDER=1** in .env only if you
    want workers strictly in CSV row order instead.
  • Up to BOL_BROWSER_MAX_PARALLEL Chromium windows (Settings). Logs use **[SN]** where N = bol_account.
  • Default **BOL_BROWSER_STICKY_SLOT_PRODUCT_URL=0**: same Chromium/login/proxy stay sticky; each CSV reload resolves the
    URL from rows matching that worker's bol_account (multiple rows per account: pick index among those rows stays stable).
  • Set **BOL_BROWSER_STICKY_SLOT_PRODUCT_URL=1** to freeze each worker's PDP URL to the launch snapshot (live CSV edits
    ignored until restart).

BOT FLOW
  • Headed Playwright Chromium: per-slot login if needed → poll assigned PDP → add to cart when available → checkout
    in the same window. Payment URL can be recorded; Discord notifications use your webhook files.

DELAYS — SETTINGS TAB (gui_settings.json, applied when you press Start)
  These values are passed into the bot as environment variables.

  PDP polling (random each time, uniform between min and max):
    • BOL_BROWSER_POLL_OOS_MIN / BOL_BROWSER_POLL_OOS_MAX — delay between checks while the PDP is **loaded** (page up)
      but you are still waiting: **out of stock**, **Not available**, or **before** the yellow add-to-cart shows / ATC retries.
      Defaults 6 and 9 — Settings → PDP polling. **MIN is also enforced** between PDP snapshots (you cannot poll faster than MIN).
    • BOL_BROWSER_FULL_RELOAD_EVERY_N_POLLS — full ``page.goto`` every N polls (default 3); between those, light DOM reads only
      (less visual flashing; stock still checked each poll).
    • BOL_BROWSER_POLL_OFFLINE_MIN / BOL_BROWSER_POLL_OFFLINE_MAX — when the listing looks **offline**, thin shell, or errors
      (defaults 40 and 60).

  Checkout login (mid-flow Inloggen on login.bol.com):
    • BOL_CHECKOUT_LOGIN_PAUSE_BEFORE_FILL_SEC — wait after the form appears before typing credentials.
    • BOL_CHECKOUT_LOGIN_PAUSE_BEFORE_SUBMIT_SEC — wait after filling before clicking Inloggen.
    • BOL_CHECKOUT_LOGIN_SETTLE_SEC — short pause after submit while navigation begins.
    • BOL_CHECKOUT_LOGIN_MAX_REDIRECT_POLL_SEC — how long to poll the URL for a checkout redirect.
    • BOL_CHECKOUT_LOGIN_SILENT_EXTEND_SEC — extra quiet polling if redirect is still unclear.
    • BOL_BROWSER_CHECKOUT_LOGIN_WAIT_SEC — upper bound for manual login if auto-fill cannot complete.

  Other knobs on the same tab: USE_PROXIES, BOL_BROWSER_MAX_PARALLEL (1–3), sitemap scan interval/workers/index
  (only if sitemap is enabled).

DELAYS — NOT IN THE GUI
  Homepage load, cookie/CMP dismiss, and login-field timings are controlled only through .env variables read by
  browser_mode.py (names prefixed like BOL_BROWSER_*). Defaults there are tuned for parallel slots; adjust there if
  you need slower/faster login behaviour.

LOGS
  • Tabs are sorted **Acc 1 · Acc 2 · Acc 3** left to right (Email1 / Email2 / Email3 logs), **not** by which product URL
    is first in the CSV. Lines **[SN]** (start or anywhere), **`session_N.json`**, and **`bol_account=N`** in **[SETUP]** rows
    route to **Acc N**. Untagged lines follow the last routed tab.

TIP
  • Stop the bot before swapping proxy lines or session files if you want a clean identity change; then Start again.
"""

DEFAULT_SETTINGS: dict = {
    "USE_PROXIES": True,
    "BOL_DIAGNOSTIC_LOG": False,
    "SITEMAP_ENABLED": True,
    "BOL_BROWSER_POLL_OOS_MIN": "6",
    "BOL_BROWSER_POLL_OOS_MAX": "9",
    "BOL_BROWSER_FULL_RELOAD_EVERY_N_POLLS": "3",
    "BOL_BROWSER_POLL_OFFLINE_MIN": "40",
    "BOL_BROWSER_POLL_OFFLINE_MAX": "60",
    "BOL_CHECKOUT_LOGIN_PAUSE_BEFORE_FILL_SEC": "0.12",
    "BOL_CHECKOUT_LOGIN_PAUSE_BEFORE_SUBMIT_SEC": "0.75",
    "BOL_CHECKOUT_LOGIN_SETTLE_SEC": "0.42",
    "BOL_CHECKOUT_LOGIN_MAX_REDIRECT_POLL_SEC": "22",
    "BOL_CHECKOUT_LOGIN_SILENT_EXTEND_SEC": "12",
    "BOL_BROWSER_CHECKOUT_LOGIN_WAIT_SEC": "120",
    "BOL_BROWSER_MAX_PARALLEL": "3",
    "SITEMAP_SCAN_INTERVAL_SECS": "45",
    "SITEMAP_WORKERS": "1",
    "BOL_SITEMAP_INDEX": "https://www.bol.com/sitemap/nl-nl/",
}

_ACCOUNT_COMBO_PREFIX_RE = re.compile(r"^(\d+)\s*[—\-]")


def _parallel_slot_cap(settings: dict) -> int:
    raw = str(
        settings.get("BOL_BROWSER_MAX_PARALLEL", DEFAULT_SETTINGS["BOL_BROWSER_MAX_PARALLEL"])
    ).strip()
    try:
        return min(3, max(1, int(float(raw))))
    except ValueError:
        return 3


_BOOL_SETTING_KEYS = frozenset(
    {
        "SITEMAP_ENABLED",
        "USE_PROXIES",
        "BOL_DIAGNOSTIC_LOG",
    }
)


def _load_json_settings() -> dict:
    if not GUI_SETTINGS_PATH.is_file():
        return dict(DEFAULT_SETTINGS)
    try:
        data = json.loads(GUI_SETTINGS_PATH.read_text(encoding="utf-8"))
        out = dict(DEFAULT_SETTINGS)
        out.update({k: data[k] for k in DEFAULT_SETTINGS if k in data})
        for k in _BOOL_SETTING_KEYS:
            if k in out:
                out[k] = bool(out[k])
        return out
    except (OSError, json.JSONDecodeError, TypeError):
        return dict(DEFAULT_SETTINGS)


def _save_json_settings(data: dict) -> None:
    to_write = {k: data.get(k, DEFAULT_SETTINGS[k]) for k in DEFAULT_SETTINGS}
    GUI_SETTINGS_PATH.write_text(json.dumps(to_write, indent=2), encoding="utf-8")


def ensure_product_csv_exists() -> None:
    if not PRODUCTS_PATH.exists():
        PRODUCTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with PRODUCTS_PATH.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["product_url", "bol_account"])
            w.writeheader()


def _gui_load_env_map() -> dict[str, str]:
    path = SCRIPT_DIR / ".env"
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k:
            out[k] = v
    return out


def _gui_pick_email_slot(env: dict[str, str], slot: int) -> str:
    if slot == 0:
        for k in (
            "Email1",
            "BOL_EMAIL1",
            "email1",
            "BOL_EMAIL",
            "Email",
            "email",
        ):
            v = (env.get(k) or "").strip()
            if v:
                return v
        return ""
    if slot == 1:
        for k in ("Email2", "BOL_EMAIL2", "email2"):
            v = (env.get(k) or "").strip()
            if v:
                return v
        return ""
    for k in ("Email3", "BOL_EMAIL3", "email3"):
        v = (env.get(k) or "").strip()
        if v:
            return v
    return ""


def _gui_mask_email_hint(email: str, *, local_prefix: int = 12) -> str:
    """
    Show enough of the start of the address to tell accounts apart in the UI;
    still hide the tail of a long local part.
    """
    e = (email or "").strip()
    if not e:
        return "(not set)"
    cap = max(4, min(24, int(local_prefix)))
    if "@" in e:
        local, _, domain = e.partition("@")
        if not local:
            return f"***@{domain}"
        if len(local) <= cap:
            return f"{local}@{domain}"
        return f"{local[:cap]}***@{domain}"
    if len(e) <= cap:
        return e
    return f"{e[:cap]}***"


def read_product_rows_from_disk() -> list[tuple[str, int]]:
    ensure_product_csv_exists()
    try:
        raw = PRODUCTS_PATH.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return []
    out: list[tuple[str, int]] = []
    try:
        reader = csv.DictReader(io.StringIO(raw))
        fn = reader.fieldnames or []
        norm = {(n or "").strip().lstrip("\ufeff").lower(): n for n in fn if n}
        key_u = norm.get("product_url")
        key_a = norm.get("bol_account") or norm.get("account")
        if key_u:
            seen: set[str] = set()
            for i, row in enumerate(reader):
                u = (row.get(key_u) or "").strip()
                if not u.startswith("http"):
                    continue
                if u in seen:
                    continue
                seen.add(u)
                acc_raw = (row.get(key_a) or "").strip() if key_a else ""
                if acc_raw:
                    try:
                        an = int(float(acc_raw))
                        an = min(3, max(1, an))
                    except ValueError:
                        an = min(3, i + 1)
                else:
                    an = min(3, i + 1)
                out.append((u, an))
            if out:
                return out
    except csv.Error:
        pass

    seen2: set[str] = set()
    loose: list[tuple[str, int]] = []
    for line in raw.splitlines():
        u = line.strip().strip('"').strip()
        if u.lower().startswith(("http://", "https://")) and u not in seen2:
            seen2.add(u)
            loose.append((u, min(3, len(loose) + 1)))
    return loose


def read_product_urls_from_disk() -> list[str]:
    return [u for u, _ in read_product_rows_from_disk()]


def log_route_bol_accounts(parallel_cap: int) -> list[int]:
    """
    Same worker → bol_account mapping as browser_mode (first N http rows, capped).
    Used so log tab labels and [SN] routing match session_N.json / EmailN (N is bol_account, not tab index).
    """
    pc = min(3, max(1, int(parallel_cap)))
    rows = read_product_rows_from_disk()
    if not rows:
        return [min(3, i + 1) for i in range(pc)]
    n = min(len(rows), pc)
    accs: list[int] = []
    for i in range(n):
        j = min(i, len(rows) - 1)
        accs.append(rows[j][1])
    return accs


def log_tabs_sorted_worker_order(accs: list[int]) -> tuple[list[int], dict[int, int]]:
    """
    Tabs left→right by **bol_account** (1, then 2, then 3) — not CSV row order.
    Returns (worker_index_order_per_tab, bol_account → tab_index). Duplicate accounts share one tab index.
    """
    n = len(accs)
    worker_order = sorted(range(n), key=lambda wi: accs[wi])
    sid_to_tab: dict[int, int] = {}
    for tab_i, wi in enumerate(worker_order):
        sid_to_tab.setdefault(accs[wi], tab_i)
    return worker_order, sid_to_tab


def write_product_rows(rows: list[tuple[str, int]]) -> None:
    seen: set[str] = set()
    ordered: list[tuple[str, int]] = []
    for u, acc in rows:
        u = (u or "").strip()
        if not u.startswith("http") or u in seen:
            continue
        seen.add(u)
        try:
            acc_n = int(acc)
        except (TypeError, ValueError):
            acc_n = 1
        acc_n = min(3, max(1, acc_n))
        ordered.append((u, acc_n))
    with PRODUCTS_PATH.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["product_url", "bol_account"])
        w.writeheader()
        for u, acc in ordered:
            w.writerow({"product_url": u, "bol_account": str(acc)})


class BolControlApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Bol monitor")
        self.minsize(960, 700)
        self.geometry("1120x820")

        self._proc: subprocess.Popen[str] | None = None
        self._reader_thread: threading.Thread | None = None
        self._stop_reader = threading.Event()
        self._log_queue: queue.Queue[tuple[int, str]] = queue.Queue()
        self._settings = _load_json_settings()
        _lc = _parallel_slot_cap(self._settings)
        self._log_line_counts: list[int] = [0] * _lc
        self._stdout_route_hint: list[int] = [0]
        self._log_route_bol_accounts: list[int] = []
        self._log_sid_to_tab_idx: dict[int, int] = {}
        self._log_tabs: list[scrolledtext.ScrolledText] = []
        self._product_row_widgets: list[dict] = []
        self._btn_add_product_row: ttk.Button | None = None

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._drain_log_queue)

        self._reload_products_list()
        self._reload_proxies_text()
        self._reload_session_tab()
        self._reload_discord_fields()
        self._reload_keywords_text()
        self._gui_log("Files loaded. Edit tabs → Save → Start bot.\n")


    def _flat_btn(
        self,
        parent: tk.Widget,
        text: str,
        command,
        bg: str,
        fg: str = "white",
        state: str = tk.NORMAL,
    ) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activeforeground=fg,
            activebackground=bg,
            relief=tk.FLAT,
            bd=0,
            highlightthickness=0,
            padx=18,
            pady=8,
            cursor="hand2",
            font=("Segoe UI", 9, "bold"),
            state=state,
        )

    def _build_ui(self) -> None:
        try:
            ttk.Style().theme_use("clam")
        except tk.TclError:
            pass
        st = ttk.Style()
        st.configure("App.TFrame", background=APP_BG)
        st.configure("TFrame", background=APP_BG)
        st.configure("TLabel", background=APP_BG)
        st.configure("TLabelframe", background=CARD_BG)
        st.configure("TLabelframe.Label", background=CARD_BG, foreground="#111827", font=("Segoe UI", 10, "bold"))
        st.configure("Monitor.TFrame", background=CARD_BG)
        st.configure("Monitor.TLabel", background=CARD_BG, foreground="#334155")

        self.configure(bg=APP_BG)

        root = tk.Frame(self, bg=APP_BG, padx=14, pady=12)
        root.pack(fill=tk.BOTH, expand=True)

        header = tk.Frame(root, bg=APP_BG)
        self._header_frame = header
        header.pack(fill=tk.X, pady=(0, 10))

        left_head = tk.Frame(header, bg=APP_BG)
        left_head.pack(side=tk.LEFT, fill=tk.Y)
        self._bol_logo_label = tk.Label(left_head, bg=APP_BG)
        self._bol_logo_label.pack(side=tk.LEFT, padx=(0, 10))
        self._bol_photo: tk.PhotoImage | None = None
        self.after(80, self._load_bol_logo_image)

        titles = tk.Frame(left_head, bg=APP_BG)
        titles.pack(side=tk.LEFT, fill=tk.Y)
        tk.Label(
            titles,
            text="Bol monitor",
            bg=APP_BG,
            fg="#111827",
            font=("Segoe UI", 16, "bold"),
        ).pack(anchor=tk.W)
        tk.Label(
            titles,
            text="Edit files → Start bot",
            bg=APP_BG,
            fg="#6b7280",
            font=("Segoe UI", 9),
        ).pack(anchor=tk.W)

        right_head = tk.Frame(header, bg=APP_BG)
        right_head.pack(side=tk.RIGHT)

        self.status_var = tk.StringVar(value="Bot: stopped")
        tk.Label(
            right_head,
            textvariable=self.status_var,
            bg=APP_BG,
            fg="#374151",
            font=("Segoe UI", 9, "bold"),
        ).pack(side=tk.LEFT, padx=(0, 14))

        self.btn_start = self._flat_btn(right_head, "Start bot", self._start_bot, ACCENT_BLUE)
        self.btn_start.pack(side=tk.LEFT, padx=4)
        self.btn_stop = self._flat_btn(
            right_head, "Stop bot", self._stop_bot, ACCENT_RED, state=tk.DISABLED
        )
        self.btn_stop.pack(side=tk.LEFT, padx=4)
        self._flat_btn(right_head, "Clear log tab", self._clear_log, ACCENT_GRAY).pack(side=tk.LEFT, padx=4)

        body_outer = tk.Frame(root, bg=APP_BG)
        body_outer.pack(fill=tk.BOTH, expand=True)

        self._main_canvas = tk.Canvas(body_outer, bg=APP_BG, highlightthickness=0)
        main_sb = ttk.Scrollbar(body_outer, orient=tk.VERTICAL, command=self._main_canvas.yview)
        self._main_canvas.configure(yscrollcommand=main_sb.set)
        main_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._main_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        main_inner = tk.Frame(self._main_canvas, bg=APP_BG)
        self._main_inner = main_inner
        self._main_inner_win = self._main_canvas.create_window((0, 0), window=main_inner, anchor="nw")

        main_inner.bind("<Configure>", lambda _e: self._sync_main_scrollregion())

        def _main_canvas_configure(event) -> None:
            if getattr(event, "width", 0) > 1:
                with contextlib.suppress(tk.TclError):
                    self._main_canvas.itemconfigure(self._main_inner_win, width=int(event.width))
            self.after_idle(self._sync_main_scrollregion)

        self._main_canvas.bind("<Configure>", _main_canvas_configure)

        monitor_block = tk.Frame(main_inner, bg=APP_BG, height=460)
        monitor_block.pack(fill=tk.X, pady=(0, 8))
        monitor_block.pack_propagate(False)

        mid = ttk.Panedwindow(monitor_block, orient=tk.HORIZONTAL)
        mid.pack(fill=tk.BOTH, expand=True)
        self._mid_hp = mid

        left_card = ttk.Labelframe(mid, text="Files", padding=8)
        mid.add(left_card, weight=7)

        right_card = ttk.Labelframe(mid, text="How it works", padding=8)
        mid.add(right_card, weight=3)

        self._monitor_pages: dict[str, tk.Frame] = {}
        self._monitor_tab_buttons: dict[str, tk.Button] = {}
        self._monitor_tab_key: str | None = None

        self._monitor_tab_row = tk.Frame(left_card, bg=CARD_BG)
        self._monitor_tab_row.pack(fill=tk.X, pady=(0, 6))

        self._monitor_content = tk.Frame(left_card, bg=CARD_BG)
        self._monitor_content.pack(fill=tk.BOTH, expand=True)

        inner_help = tk.Frame(right_card, bg=INNER_BG, highlightthickness=1, highlightbackground="#e5e7eb")
        inner_help.pack(fill=tk.BOTH, expand=True)
        help_scroll = scrolledtext.ScrolledText(
            inner_help,
            wrap=tk.WORD,
            font=("Segoe UI", 10),
            bg=INNER_BG,
            fg="#1f2937",
            relief=tk.FLAT,
            padx=8,
            pady=8,
        )
        help_scroll.pack(fill=tk.BOTH, expand=True)
        help_scroll.insert("1.0", HOW_IT_WORKS_TEXT)
        help_scroll.configure(state=tk.DISABLED)

        self._tab_products()
        self._tab_proxies()
        self._tab_session()
        self._tab_discord()
        self._tab_keywords()
        self._tab_settings()
        self._monitor_tab_select("products")

        self._log_labelframe = ttk.Labelframe(main_inner, text="", padding=6)
        self._log_labelframe.pack(fill=tk.BOTH, expand=True, pady=(0, 4))

        inner_log = tk.Frame(self._log_labelframe, bg=INNER_BG, highlightthickness=1, highlightbackground="#e5e7eb")
        inner_log.pack(fill=tk.BOTH, expand=True)

        self._log_notebook = ttk.Notebook(inner_log)
        self._log_notebook.pack(fill=tk.BOTH, expand=True)

        self._populate_log_notebook_tabs(_parallel_slot_cap(self._settings))

        self.bind_all("<MouseWheel>", self._route_mousewheel)
        self.bind_all("<Button-4>", self._route_mousewheel)
        self.bind_all("<Button-5>", self._route_mousewheel)

        self.after(200, self._set_mid_sash)
        self.after(280, self._sync_main_scrollregion)

    def _set_mid_sash(self) -> None:
        try:
            self.update_idletasks()
            w = int(self._mid_hp.winfo_width())
            if w < 200:
                self.after(120, self._set_mid_sash)
                return
            self._mid_hp.sashpos(0, int(w * 0.70))
        except tk.TclError:
            pass
        self.after_idle(self._sync_main_scrollregion)

    def _mousewheel_units(self, event) -> int:
        n = getattr(event, "num", None)
        if n == 4:
            return -1
        if n == 5:
            return 1
        d = int(getattr(event, "delta", 0) or 0)
        if d == 0:
            return 0
        sign = -1 if d > 0 else 1
        steps = max(1, abs(d) // 120)
        return sign * steps

    def _widget_under_pointer(self, event) -> tk.Widget | None:
        try:
            w = self.winfo_containing(event.x_root, event.y_root)
        except tk.TclError:
            w = None
        if w is None:
            try:
                w = event.widget
            except tk.TclError:
                return None
        return w

    def _is_under_header(self, w: tk.Widget | None) -> bool:
        hf = getattr(self, "_header_frame", None)
        if hf is None or w is None:
            return False
        cur: tk.Widget | None = w
        while cur is not None:
            if cur is hf:
                return True
            try:
                cur = cur.master  # type: ignore[assignment]
            except tk.TclError:
                break
        return False

    def _is_descendant_of(self, w: tk.Widget | None, ancestor: tk.Widget | None) -> bool:
        if w is None or ancestor is None:
            return False
        cur: tk.Widget | None = w
        while cur is not None:
            if cur is ancestor:
                return True
            try:
                cur = cur.master  # type: ignore[assignment]
            except tk.TclError:
                break
        return False

    def _route_mousewheel(self, event) -> str | None:
        d = self._mousewheel_units(event)
        if d == 0:
            return None
        w = self._widget_under_pointer(event)
        if w is None or self._is_under_header(w):
            return None
        try:
            if w.winfo_toplevel() is not self:
                return None
        except tk.TclError:
            return None
        while w is not None:
            if isinstance(w, tk.Text):
                try:
                    if str(w.cget("state")).lower() != "disabled":
                        return None
                except tk.TclError:
                    return None
                for lw in getattr(self, "_log_tabs", None) or []:
                    if self._is_descendant_of(w, lw):
                        w.yview_scroll(int(d) * 2, "units")
                        return "break"
                try:
                    w = w.master
                except tk.TclError:
                    break
                continue

            if getattr(w, "_bol_tab_scroll", False) and isinstance(w, tk.Canvas):
                if w.bbox("all"):
                    w.yview_scroll(int(d), "units")
                return "break"

            try:
                w = w.master
            except tk.TclError:
                break
        try:
            self._main_canvas.yview_scroll(int(d) * 3, "units")
        except tk.TclError:
            pass
        return "break"

    def _register_monitor_tab(self, key: str, label: str, tab: tk.Frame) -> None:
        tab.place(relx=0, rely=0, relwidth=1, relheight=1)
        tab.place_forget()
        self._monitor_pages[key] = tab
        btn = tk.Button(
            self._monitor_tab_row,
            text=label,
            command=lambda k=key: self._monitor_tab_select(k),
            relief=tk.FLAT,
            bd=0,
            highlightthickness=0,
            padx=14,
            pady=8,
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        )
        btn.pack(side=tk.LEFT, padx=(0, 4))
        self._monitor_tab_buttons[key] = btn

    def _monitor_tab_select(self, key: str) -> None:
        if key not in self._monitor_pages:
            return
        for k, fr in self._monitor_pages.items():
            if k == key:
                fr.place(relx=0, rely=0, relwidth=1, relheight=1)
            else:
                fr.place_forget()
        self._monitor_tab_key = key
        for k, btn in self._monitor_tab_buttons.items():
            sel = k == key
            btn.configure(
                bg=MONITOR_TAB_SEL_BG if sel else MONITOR_TAB_UNSEL_BG,
                fg=MONITOR_TAB_SEL_FG if sel else MONITOR_TAB_UNSEL_FG,
                activebackground=MONITOR_TAB_SEL_BG if sel else MONITOR_TAB_UNSEL_BG,
                activeforeground=MONITOR_TAB_SEL_FG if sel else MONITOR_TAB_UNSEL_FG,
            )
        sync = getattr(self._monitor_pages[key], "_bol_tab_scroll_sync", None)
        if callable(sync):
            self.after_idle(sync)
        self.after_idle(self._sync_main_scrollregion)

    def _sync_main_scrollregion(self, _event=None) -> None:
        try:
            self.update_idletasks()
            win = self._main_inner_win
            cw = max(40, int(self._main_canvas.winfo_width()))
            self._main_canvas.itemconfigure(win, width=cw)
            rh = max(1, int(self._main_inner.winfo_reqheight()))
            self._main_canvas.itemconfigure(win, height=rh)
            bbox = self._main_canvas.bbox("all")
            if bbox:
                _x1, _y1, x2, y2 = bbox
                self._main_canvas.configure(scrollregion=(0, 0, x2, y2 + 20))
        except tk.TclError:
            pass

    def _monitor_tab_footer_and_scroll(self, tab: tk.Frame) -> tuple[tk.Frame, tk.Frame]:
        footer = tk.Frame(tab, bg=CARD_BG)
        footer.pack(side=tk.BOTTOM, fill=tk.X, pady=(2, 0))
        line = tk.Frame(tab, height=1, bg="#94a3b8", highlightthickness=0)
        line.pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 6))

        holder = tk.Frame(tab, bg=CARD_BG)
        holder.pack(fill=tk.BOTH, expand=True)

        canvas = tk.Canvas(holder, bg=CARD_BG, highlightthickness=0)
        sb = ttk.Scrollbar(holder, orient=tk.VERTICAL, command=canvas.yview)
        inner = tk.Frame(canvas, bg=CARD_BG)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _sync_tab_canvas(_event=None) -> None:
            pad = 4
            canvas.update_idletasks()
            cw = max(40, int(canvas.winfo_width()) - pad)
            ch = max(80, int(canvas.winfo_height()) - pad)
            rh = int(inner.winfo_reqheight())
            ih = max(ch, rh)
            canvas.itemconfigure(win, width=cw, height=ih)
            bbox = canvas.bbox("all")
            if bbox:
                canvas.configure(scrollregion=bbox)

        inner.bind("<Configure>", _sync_tab_canvas)
        canvas.bind("<Configure>", lambda _e: _sync_tab_canvas())
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas._bol_tab_scroll = True  # noqa: SLF001 — wheel routes here in _route_mousewheel

        tab._bol_tab_scroll_sync = _sync_tab_canvas  # noqa: SLF001 — refreshed on tab switch

        return inner, footer

    def _tab_products(self) -> None:
        tab = tk.Frame(self._monitor_content, bg=CARD_BG, padx=8, pady=8)
        inner, footer = self._monitor_tab_footer_and_scroll(tab)

        inner.grid_columnconfigure(0, weight=1)
        inner.grid_rowconfigure(1, weight=1)
        ttk.Label(
            inner,
            text=(
                f"Saved as {PRODUCTS_PATH.name} — up to 3 https URLs; pick Bol login 1–3 per row "
                "(Email1…Email3 in .env). **bol_account N** ↔ proxy line N ↔ session_N.json (not tied to row position)."
            ),
            font=("Segoe UI", 9),
            style="Monitor.TLabel",
            wraplength=560,
        ).grid(row=0, column=0, sticky="ew", padx=4, pady=(0, 4))

        body = ttk.Frame(inner, style="Monitor.TFrame")
        body.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        body.grid_columnconfigure(0, weight=1)

        hdr = ttk.Frame(body, style="Monitor.TFrame")
        hdr.grid(row=0, column=0, sticky="ew")
        hdr.grid_columnconfigure(0, weight=1)
        ttk.Label(hdr, text="Product URL", style="Monitor.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(hdr, text="Login (.env)", style="Monitor.TLabel").grid(
            row=0, column=1, sticky="w", padx=(12, 0)
        )
        ttk.Label(hdr, text="", style="Monitor.TLabel", width=4).grid(
            row=0, column=2, sticky="e", padx=(6, 0)
        )

        self._product_rows_container = ttk.Frame(body, style="Monitor.TFrame")
        self._product_rows_container.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self._product_rows_container.grid_columnconfigure(0, weight=1)

        fr = ttk.Frame(footer, style="Monitor.TFrame")
        fr.pack(fill=tk.X, padx=2, pady=(2, 0))
        ttk.Button(fr, text="Save to product.csv", command=self._products_save).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        self._btn_add_product_row = ttk.Button(
            fr, text="+ Add URL", command=self._products_add_row_click
        )
        self._btn_add_product_row.pack(side=tk.LEFT, padx=(0, 6))
        self._register_monitor_tab("products", "Products", tab)

    def _tab_proxies(self) -> None:
        tab = tk.Frame(self._monitor_content, bg=CARD_BG, padx=8, pady=8)
        inner, footer = self._monitor_tab_footer_and_scroll(tab)

        inner.grid_columnconfigure(0, weight=1)
        inner.grid_rowconfigure(1, weight=1)
        ttk.Label(
            inner,
            text=(
                f"{PROXY_PATH.name} — host:port:user:pass per line. 1st URL uses line 1, 2nd URL line 2, … "
                "(max 4 parallel browser contexts)."
            ),
            style="Monitor.TLabel",
            wraplength=520,
        ).grid(row=0, column=0, sticky="ew", padx=4, pady=(0, 4))
        self.txt_proxies = scrolledtext.ScrolledText(inner, height=10, font=("Consolas", 9), wrap=tk.NONE)
        self.txt_proxies.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)

        fr = ttk.Frame(footer, style="Monitor.TFrame")
        fr.pack(fill=tk.X, padx=2, pady=(2, 0))
        ttk.Button(fr, text="Save", command=self._proxies_save).pack(side=tk.LEFT, padx=(0, 6))
        self._register_monitor_tab("proxies", "Proxies", tab)

    def _tab_session(self) -> None:
        tab = tk.Frame(self._monitor_content, bg=CARD_BG, padx=8, pady=8)
        inner, footer = self._monitor_tab_footer_and_scroll(tab)

        inner.grid_columnconfigure(0, weight=1)
        inner.grid_rowconfigure(1, weight=1)
        ttk.Label(
            inner,
            text=(
                "Login session (storage_state + proxy fingerprint).\n"
                "session_<id>.json + session_<id>.fingerprint.txt + session_<id>.binding.json "
                "(proxy + product/login binding + credential fingerprint).\n"
                "Reset sessions — removes all of the above; list refreshes. Next Start = fresh logins (proxy.txt unchanged)."
            ),
            font=("Segoe UI", 9),
            style="Monitor.TLabel",
            wraplength=540,
        ).grid(row=0, column=0, sticky="ew", padx=4, pady=(0, 6))
        self.txt_session_info = scrolledtext.ScrolledText(
            inner, height=12, font=("Consolas", 9), wrap=tk.WORD, state=tk.DISABLED
        )
        self.txt_session_info.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)

        fr = ttk.Frame(footer, style="Monitor.TFrame")
        fr.pack(fill=tk.X, padx=2, pady=(2, 0))
        ttk.Button(fr, text="Reset sessions", command=self._sessions_reset_all).pack(side=tk.LEFT)
        self._register_monitor_tab("session", "Session", tab)

    def _tab_discord(self) -> None:
        tab = tk.Frame(self._monitor_content, bg=CARD_BG, padx=8, pady=8)
        inner, footer = self._monitor_tab_footer_and_scroll(tab)

        inner.grid_columnconfigure(0, weight=1)
        ttk.Label(inner, text="Webhook URL", style="Monitor.TLabel").grid(
            row=0, column=0, sticky="w", padx=4, pady=(0, 2)
        )
        self.entry_discord_webhook = ttk.Entry(inner)
        self.entry_discord_webhook.grid(row=1, column=0, sticky="ew", padx=4, pady=(0, 8))

        ttk.Label(inner, text="Thread ID (optional)", style="Monitor.TLabel").grid(
            row=2, column=0, sticky="w", padx=4, pady=(0, 2)
        )
        self.entry_discord_thread_id = ttk.Entry(inner)
        self.entry_discord_thread_id.grid(row=3, column=0, sticky="ew", padx=4, pady=(0, 8))

        ttk.Label(inner, text="Thread name (optional)", style="Monitor.TLabel").grid(
            row=4, column=0, sticky="w", padx=4, pady=(0, 2)
        )
        self.entry_discord_thread_name = ttk.Entry(inner)
        self.entry_discord_thread_name.grid(row=5, column=0, sticky="ew", padx=4, pady=(0, 8))

        fr = ttk.Frame(footer, style="Monitor.TFrame")
        fr.pack(fill=tk.X, padx=2, pady=(2, 0))
        ttk.Button(fr, text="Save", command=self._discord_save).pack(side=tk.LEFT, padx=(0, 6))
        self._register_monitor_tab("discord", "Discord", tab)

    def _tab_keywords(self) -> None:
        tab = tk.Frame(self._monitor_content, bg=CARD_BG, padx=8, pady=8)
        inner, footer = self._monitor_tab_footer_and_scroll(tab)

        inner.grid_columnconfigure(0, weight=1)
        inner.grid_rowconfigure(1, weight=1)
        ttk.Label(
            inner,
            text=(
                f"Optional sitemap keywords — {EXTRA_KEYWORDS_PATH.name}. "
                "One word per line. Save if you use the sitemap tab in 1.py."
            ),
            font=("Segoe UI", 9),
            style="Monitor.TLabel",
            wraplength=520,
        ).grid(row=0, column=0, sticky="ew", padx=4, pady=(0, 4))

        self.txt_keywords = scrolledtext.ScrolledText(inner, height=10, font=("Consolas", 10))
        self.txt_keywords.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)

        fr = ttk.Frame(footer, style="Monitor.TFrame")
        fr.pack(fill=tk.X, padx=2, pady=(2, 0))
        ttk.Button(fr, text="Save", command=self._keywords_save).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(fr, text="Preview auto keywords from CSV", command=self._keywords_preview).pack(
            side=tk.LEFT
        )
        self._register_monitor_tab("keywords", "Keywords", tab)

    def _tab_settings(self) -> None:
        tab = tk.Frame(self._monitor_content, bg=CARD_BG, padx=8, pady=8)
        inner, footer = self._monitor_tab_footer_and_scroll(tab)
        inner.grid_columnconfigure(1, weight=1)

        self._settings_entries = {}
        s = self._settings
        self.var_use_proxies = tk.BooleanVar(value=bool(s.get("USE_PROXIES", True)))
        self.var_bol_diagnostic = tk.BooleanVar(value=bool(s.get("BOL_DIAGNOSTIC_LOG", False)))
        self.var_sitemap_enabled = tk.BooleanVar(value=bool(s.get("SITEMAP_ENABLED", True)))

        def hint(r: int, text: str) -> int:
            ttk.Label(
                inner,
                text=text,
                font=("Segoe UI", 8),
                foreground="#64748b",
                style="Monitor.TLabel",
            ).grid(row=r, column=0, columnspan=2, sticky="w", padx=4, pady=(0, 8))
            return r + 1

        def row(r: int, label: str, key: str, w: int = 14, sub: str | None = None) -> int:
            ttk.Label(inner, text=label, style="Monitor.TLabel").grid(
                row=r, column=0, sticky="nw", padx=4, pady=3
            )
            e = ttk.Entry(inner, width=w)
            dv = DEFAULT_SETTINGS[key]
            if not isinstance(dv, bool):
                cur = s.get(key, dv)
                e.insert(0, "" if cur == "" or cur is None else str(cur))
            e.grid(row=r, column=1, sticky="ew", padx=4, pady=3)
            self._settings_entries[key] = e
            r += 1
            if sub:
                r = hint(r, sub)
            return r

        def sec(r: int, title: str) -> int:
            ttk.Label(
                inner,
                text=title,
                font=("Segoe UI", 10, "bold"),
                style="Monitor.TLabel",
            ).grid(row=r, column=0, columnspan=2, sticky="w", padx=4, pady=(14, 6))
            return r + 1

        r = 0
        ttk.Label(
            inner,
            text="Saved to gui_settings.json and applied when you Start (Playwright browser bot).",
            font=("Segoe UI", 9),
            foreground="#444",
            style="Monitor.TLabel",
            wraplength=540,
        ).grid(row=r, column=0, columnspan=2, sticky="ew", padx=4, pady=(0, 6))
        r += 1

        r = sec(r, "On / off")
        ttk.Checkbutton(
            inner,
            text="Use proxy.txt (bol_account N ↔ proxy line N ↔ session_N). Off = direct IP; same layout — full monitor + checkout per worker.",
            variable=self.var_use_proxies,
        ).grid(row=r, column=0, columnspan=2, sticky="w", padx=4, pady=2)
        r += 1
        ttk.Checkbutton(
            inner,
            text="Extra console [DIAG] lines",
            variable=self.var_bol_diagnostic,
        ).grid(row=r, column=0, columnspan=2, sticky="w", padx=4, pady=2)
        r += 1
        ttk.Checkbutton(
            inner,
            text="Background sitemap (CLI / advanced — optional)",
            variable=self.var_sitemap_enabled,
        ).grid(row=r, column=0, columnspan=2, sticky="w", padx=4, pady=2)
        r += 1

        r = sec(r, "Browser slots")
        r = row(
            r,
            "Max Chromium BOL_BROWSER_MAX_PARALLEL (1–3)",
            "BOL_BROWSER_MAX_PARALLEL",
            4,
            "Up to N workers start (N = min(CSV http rows, this cap)). Same code every tab — each row's bol_account picks "
            "proxy line + session + EmailN. Need proxy.txt line M if any row uses bol_account M. Lower this cap or "
            "disable slot flags to run fewer windows.",
        )

        r = sec(r, "PDP polling — random delay each time (seconds)")
        r = hint(
            r,
            "Loaded PDP (page open): one pair of min/max for **all** monitoring — out of stock, Not available, waiting for "
            "yellow ATC, and short ATC retries. Uniform random seconds between each poll.",
        )
        r = row(
            r,
            "Loaded PDP min BOL_BROWSER_POLL_OOS_MIN",
            "BOL_BROWSER_POLL_OOS_MIN",
            8,
            None,
        )
        r = row(r, "Loaded PDP max BOL_BROWSER_POLL_OOS_MAX", "BOL_BROWSER_POLL_OOS_MAX", 8)
        r = hint(
            r,
            "Random delay between polls; **MIN is a hard floor** between PDP snapshots (same slot). Default 6–9 s.",
        )
        r = row(
            r,
            "Full reload every N polls BOL_BROWSER_FULL_RELOAD_EVERY_N_POLLS",
            "BOL_BROWSER_FULL_RELOAD_EVERY_N_POLLS",
            4,
            "1 = every poll does page.goto (heavy). 3 = full reload every 3rd poll; others use light DOM (same stock logic).",
        )
        r = row(
            r,
            "Offline min BOL_BROWSER_POLL_OFFLINE_MIN",
            "BOL_BROWSER_POLL_OFFLINE_MIN",
            8,
            None,
        )
        r = row(r, "Offline max BOL_BROWSER_POLL_OFFLINE_MAX", "BOL_BROWSER_POLL_OFFLINE_MAX", 8)
        r = hint(r, "Typical range 40–60 s between polls when the page failed / looks offline.")

        r = sec(r, "Checkout login (mid-flow Inloggen on login.bol.com)")
        r = row(
            r,
            "Pause before fill (s)",
            "BOL_CHECKOUT_LOGIN_PAUSE_BEFORE_FILL_SEC",
            8,
            "Wait after login form appears before typing email/password.",
        )
        r = row(
            r,
            "Pause before Inloggen click (s)",
            "BOL_CHECKOUT_LOGIN_PAUSE_BEFORE_SUBMIT_SEC",
            8,
            "Wait after fields filled before pressing Inloggen.",
        )
        r = row(
            r,
            "After submit settle (s)",
            "BOL_CHECKOUT_LOGIN_SETTLE_SEC",
            8,
            "Sleep after navigation starts, before URL polling.",
        )
        r = row(
            r,
            "Redirect poll window (s)",
            "BOL_CHECKOUT_LOGIN_MAX_REDIRECT_POLL_SEC",
            8,
            "Max time to poll URL after login for checkout redirect.",
        )
        r = row(
            r,
            "Silent extend if unclear (s)",
            "BOL_CHECKOUT_LOGIN_SILENT_EXTEND_SEC",
            8,
            "Extra quiet polling if redirect not detected yet.",
        )
        r = row(
            r,
            "Manual login wait cap (s)",
            "BOL_BROWSER_CHECKOUT_LOGIN_WAIT_SEC",
            8,
            "If auto-fill fails: max wait for you to finish login in the window.",
        )

        r = sec(r, "Sitemap (only if enabled above)")
        r = row(r, "Scan interval (s)", "SITEMAP_SCAN_INTERVAL_SECS", 6)
        r = row(r, "Workers", "SITEMAP_WORKERS", 6)
        r = row(r, "Index URL", "BOL_SITEMAP_INDEX", 40)

        inner.grid_rowconfigure(r, weight=1)
        tk.Frame(inner, bg=CARD_BG, height=1).grid(row=r, column=0, columnspan=2, sticky="nsew")

        fr = ttk.Frame(footer, style="Monitor.TFrame")
        fr.pack(fill=tk.X, padx=2, pady=(2, 0))
        ttk.Button(fr, text="Save", command=self._settings_save_click).pack(side=tk.LEFT, padx=(0, 6))
        self._register_monitor_tab("settings", "Settings", tab)

    def _bol_account_combo_labels(self) -> tuple[str, str, str]:
        """Dropdown text only — masked emails; bol_account 1..3 is derived via `_account_num_from_combo_text`."""
        env = _gui_load_env_map()
        return (
            _gui_mask_email_hint(_gui_pick_email_slot(env, 0)),
            _gui_mask_email_hint(_gui_pick_email_slot(env, 1)),
            _gui_mask_email_hint(_gui_pick_email_slot(env, 2)),
        )

    def _account_num_from_combo_text(self, display: str) -> int:
        """Map visible combo string → bol_account column (1–3) for product.csv / bot."""
        hints = self._bol_account_combo_labels()
        d = (display or "").strip()
        for i, h in enumerate(hints):
            if d == (h or "").strip():
                return i + 1
        m = _ACCOUNT_COMBO_PREFIX_RE.match(d)
        if m:
            return min(3, max(1, int(m.group(1))))
        env = _gui_load_env_map()
        for slot in range(3):
            full = (_gui_pick_email_slot(env, slot) or "").strip()
            if full and d == full:
                return slot + 1
        return 1

    def _combo_row_account(self, w: dict) -> int:
        return self._account_num_from_combo_text(w["combo"].get())

    def _refresh_product_account_combos(self) -> None:
        """Every row may pick Email1/2/3 freely — **same account on multiple rows is OK** (parallel tabs share
        session_N.json). We never silently rewrite row A to Email1 just because row B also chose Email3."""
        labels = self._bol_account_combo_labels()
        label_for = {1: labels[0], 2: labels[1], 3: labels[2]}
        rows = self._product_row_widgets
        all_accounts = (1, 2, 3)
        vals = [label_for[a] for a in all_accounts]
        for w in rows:
            cur = self._combo_row_account(w)
            cur = min(3, max(1, int(cur)))
            cb = w["combo"]
            cb.configure(values=vals)
            cb.set(label_for[cur])

    def _sync_add_url_button_state(self) -> None:
        btn = self._btn_add_product_row
        if btn is None:
            return
        if len(self._product_row_widgets) >= 3:
            btn.state(["disabled"])
        else:
            btn.state(["!disabled"])

    def _clear_product_rows(self) -> None:
        for w in self._product_row_widgets:
            w["frame"].destroy()
        self._product_row_widgets.clear()
        self._sync_add_url_button_state()

    def _regrid_product_rows(self) -> None:
        for i, w in enumerate(self._product_row_widgets):
            w["frame"].grid(row=i, column=0, sticky="ew", pady=(0, 6))

    def _remove_product_row(self, row: dict) -> None:
        try:
            self._product_row_widgets.remove(row)
        except ValueError:
            return
        row["frame"].destroy()
        self._regrid_product_rows()
        self._sync_add_url_button_state()
        self._refresh_product_account_combos()
        if not self._product_row_widgets:
            self._add_product_row("", 1)

    def _add_product_row(self, url: str = "", account: int = 1) -> None:
        if len(self._product_row_widgets) >= 3:
            return
        parent = self._product_rows_container
        r = len(self._product_row_widgets)
        fr = ttk.Frame(parent, style="Monitor.TFrame")
        fr.grid(row=r, column=0, sticky="ew", pady=(0, 6))
        fr.grid_columnconfigure(0, weight=1)

        ent = ttk.Entry(fr, font=("Segoe UI", 9))
        ent.grid(row=0, column=0, sticky="ew")
        if url:
            ent.insert(0, url)

        labels = self._bol_account_combo_labels()
        cb = ttk.Combobox(fr, width=52, state="readonly", values=list(labels))
        cb.grid(row=0, column=1, padx=(8, 0), sticky="e")
        cb.bind("<<ComboboxSelected>>", lambda _e: self._refresh_product_account_combos())

        row_ref: dict = {"frame": fr, "entry": ent, "combo": cb}
        del_btn = ttk.Button(
            fr,
            text="×",
            width=3,
            command=lambda rr=row_ref: self._remove_product_row(rr),
        )
        del_btn.grid(row=0, column=2, padx=(6, 0), sticky="e")
        row_ref["del_btn"] = del_btn

        self._product_row_widgets.append(row_ref)
        self._sync_add_url_button_state()
        self._refresh_product_account_combos()
        want = min(3, max(1, int(account)))
        vals = tuple(cb.cget("values"))
        if labels[want - 1] in vals:
            cb.set(labels[want - 1])
            self._refresh_product_account_combos()

    def _product_rows_from_editor(self) -> list[tuple[str, int]]:
        out: list[tuple[str, int]] = []
        for w in self._product_row_widgets:
            u = w["entry"].get().strip()
            if not u.startswith("http"):
                continue
            acc = self._combo_row_account(w)
            out.append((u, acc))
        return out

    def _reload_products_list(self) -> None:
        rows = read_product_rows_from_disk()
        self._clear_product_rows()
        display = rows[:3]
        if len(rows) > 3:
            self._gui_log(
                f"Reloaded products: {PRODUCTS_PATH.name} has {len(rows)} row(s); "
                f"editor shows first 3 — Save overwrites file with those rows only.\n",
            )
        if not display:
            self._add_product_row("", 1)
            self._gui_log("Reloaded product editor (empty — one blank row).\n")
            return
        for u, acc in display:
            self._add_product_row(u, acc)
        self._refresh_product_account_combos()
        self._gui_log(f"Reloaded product editor ({len(display)} URL row(s)) from disk.\n")

    def _products_add_row_click(self) -> None:
        if len(self._product_row_widgets) >= 3:
            return
        taken: set[int] = set()
        for w in self._product_row_widgets:
            taken.add(self._combo_row_account(w))
        acc = next((a for a in (1, 2, 3) if a not in taken), 1)
        self._add_product_row("", acc)

    def _products_save(self) -> None:
        seen: set[str] = set()
        ordered: list[tuple[str, int]] = []
        for u, acc in self._product_rows_from_editor():
            if u in seen:
                continue
            seen.add(u)
            ordered.append((u, acc))
        if not ordered:
            if not messagebox.askyesno(
                "Empty",
                "No rows with http URLs. Save anyway? (will clear product.csv to header only.)",
            ):
                return
        try:
            write_product_rows(ordered)
        except OSError as e:
            messagebox.showerror("Save failed", str(e))
            return
        self._gui_log(f"Saved {PRODUCTS_PATH.name} ({len(ordered)} product row(s)).\n")

    def _sessions_reset_all(self) -> None:
        if not messagebox.askyesno(
            "Reset all sessions",
            "Delete all saved browser session files in the cookies folder?\n\n"
            "Next Start will ask for fresh login per session file. "
            "proxy.txt is unchanged — bol_account N still maps to proxy line N.",
        ):
            return
        patterns = (
            "session_*.json",
            "session_*.fingerprint.txt",
            "session_*.binding.json",
            "session_*.proxy.fp",
        )
        removed = 0
        try:
            SESSION_DIR.mkdir(parents=True, exist_ok=True)
            for pat in patterns:
                for p in SESSION_DIR.glob(pat):
                    try:
                        p.unlink()
                        removed += 1
                    except OSError:
                        pass
        except OSError as e:
            messagebox.showerror("Reset failed", str(e))
            return
        self._reload_session_tab()
        self._gui_log(f"Sessions reset: removed {removed} file(s) under {SESSION_DIR}.\n")
        messagebox.showinfo(
            "Sessions reset",
            f"Removed {removed} file(s). Start the bot to log in again.",
        )

    def _reload_session_tab(self) -> None:
        lines = [
            f"Folder: {SESSION_DIR}",
            "",
            "Files (created by the bot after login):",
            "",
        ]
        try:
            if SESSION_DIR.is_dir():
                js = sorted(SESSION_DIR.glob("session_*.json"))
                fps = sorted(SESSION_DIR.glob("session_*.fingerprint.txt"))
                binds = sorted(SESSION_DIR.glob("session_*.binding.json"))
                legacy = sorted(SESSION_DIR.glob("session_*.proxy.fp"))
                all_files = sorted(
                    set(js) | set(fps) | set(binds) | set(legacy), key=lambda x: x.name
                )
                if all_files:
                    for p in all_files:
                        try:
                            sz = p.stat().st_size
                            lines.append(f"  • {p.name}  ({sz:,} bytes)")
                        except OSError:
                            lines.append(f"  • {p.name}")
                else:
                    lines.append(
                        "  (none yet — first Start creates session_<id>.json + fingerprint + binding sidecars)"
                    )
            else:
                lines.append("  (folder created on first login save)")
        except OSError as e:
            lines.append(f"  Error: {e}")
        lines.extend(
            [
                "",
                "Reset login: Stop bot → delete session_*.json (+ .fingerprint.txt + .binding.json) → Start.",
                "",
                "Env: BOL_BROWSER_MAX_PARALLEL caps workers; each bol_account N uses session_N.json (not “all session 1”).",
            ]
        )
        body = "\n".join(lines) + "\n"
        self.txt_session_info.configure(state=tk.NORMAL)
        self.txt_session_info.delete("1.0", tk.END)
        self.txt_session_info.insert("1.0", body)
        self.txt_session_info.configure(state=tk.DISABLED)

    def _reload_proxies_text(self) -> None:
        self._set_text_widget(self.txt_proxies, _read_text(PROXY_PATH))

    def _proxies_save(self) -> None:
        _write_text(PROXY_PATH, self.txt_proxies.get("1.0", tk.END))
        self._gui_log(f"Saved {PROXY_PATH.name}.\n")

    def _reload_discord_fields(self) -> None:
        self.entry_discord_webhook.delete(0, tk.END)
        self.entry_discord_webhook.insert(0, _read_one_line(DISCORD_WEBHOOK_PATH))
        self.entry_discord_thread_id.delete(0, tk.END)
        self.entry_discord_thread_id.insert(0, _read_one_line(DISCORD_THREAD_ID_PATH))
        self.entry_discord_thread_name.delete(0, tk.END)
        self.entry_discord_thread_name.insert(0, _read_one_line(DISCORD_THREAD_NAME_PATH))

    def _discord_save(self) -> None:
        _write_one_line(DISCORD_WEBHOOK_PATH, self.entry_discord_webhook.get())
        _write_one_line(DISCORD_THREAD_ID_PATH, self.entry_discord_thread_id.get())
        _write_one_line(DISCORD_THREAD_NAME_PATH, self.entry_discord_thread_name.get())
        self._gui_log("Saved Discord webhook / thread files.\n")

    def _reload_keywords_text(self) -> None:
        self._set_text_widget(self.txt_keywords, _read_text(EXTRA_KEYWORDS_PATH))

    def _keywords_save(self) -> None:
        _write_text(EXTRA_KEYWORDS_PATH, self.txt_keywords.get("1.0", tk.END))
        self._gui_log(f"Saved {EXTRA_KEYWORDS_PATH.name}.\n")

    def _keywords_preview(self) -> None:
        try:
            from monitor import build_category_keywords_from_csv_urls, read_product_urls_from_csv
        except Exception as e:
            messagebox.showerror("Import", f"Could not load monitor.py: {e}")
            return
        try:
            urls = read_product_urls_from_csv(PRODUCTS_PATH)
            kws = build_category_keywords_from_csv_urls(urls)
        except Exception as e:
            messagebox.showerror("Preview failed", str(e))
            return
        preview = ", ".join(sorted(kws)[:100])
        if len(kws) > 100:
            preview += f"\n… and {len(kws) - 100} more"
        messagebox.showinfo(f"Auto keywords ({len(kws)} total)", preview or "(empty — add product URLs)")

    def _settings_save_click(self) -> None:
        self._persist_settings_from_ui()
        self._gui_log(f"Saved {GUI_SETTINGS_PATH.name}.\n")

    def _persist_settings_from_ui(self) -> None:
        s: dict = {}
        for k, default in DEFAULT_SETTINGS.items():
            if isinstance(default, bool):
                if k == "USE_PROXIES":
                    s[k] = self.var_use_proxies.get()
                elif k == "BOL_DIAGNOSTIC_LOG":
                    s[k] = self.var_bol_diagnostic.get()
                elif k == "SITEMAP_ENABLED":
                    s[k] = self.var_sitemap_enabled.get()
                else:
                    s[k] = bool(default)
            elif k == "BOL_BROWSER_MAX_PARALLEL":
                raw = self._settings_entries[k].get().strip()
                try:
                    v = int(float(raw)) if raw else int(float(default))
                    s[k] = str(min(3, max(1, v)))
                except ValueError:
                    s[k] = default
            else:
                val = self._settings_entries[k].get().strip()
                s[k] = val if val else default
        self._settings = s
        _save_json_settings(s)

    def _child_env(self) -> dict[str, str]:
        self._persist_settings_from_ui()
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8:replace"
        for k, v in self._settings.items():
            if isinstance(v, bool):
                env[k] = "1" if v else "0"
            else:
                env[k] = str(v)
        return env

    def _load_bol_logo_image(self) -> None:
        label = self._bol_logo_label

        def fallback() -> None:
            label.configure(
                text=" bol ",
                font=("Segoe UI", 8, "bold"),
                fg="white",
                bg="#0064c8",
            )

        def fetch() -> None:
            blob: bytes | None = None
            try:
                import urllib.request

                url = "https://www.google.com/s2/favicons?domain=bol.com&sz=64"
                req = urllib.request.Request(url, headers={"User-Agent": "BolMonitorGUI/1"})
                with urllib.request.urlopen(req, timeout=6) as resp:
                    blob = resp.read()
            except Exception:
                blob = None

            def apply() -> None:
                try:
                    if blob:
                        self._bol_photo = tk.PhotoImage(data=blob)
                        label.configure(image=self._bol_photo)
                    else:
                        fallback()
                except Exception:
                    fallback()

            try:
                self.after(0, apply)
            except tk.TclError:
                pass

        threading.Thread(target=fetch, daemon=True).start()

    def _stdout_line_tab_index(self, line: str, hint: list[int]) -> int:
        s = line.rstrip("\r\n")
        max_idx = max(0, len(self._log_tabs) - 1)
        sid_map = getattr(self, "_log_sid_to_tab_idx", None) or {}

        def route_by_bol_account(sid: int) -> int:
            if sid in sid_map:
                idx = min(max_idx, int(sid_map[sid]))
                hint[0] = idx
                return idx
            idx = min(max_idx, sid - 1)
            hint[0] = idx
            return idx

        m = _LOG_SLOT_TAG_RE.match(s)
        if m:
            return route_by_bol_account(max(1, int(m.group(1))))
        m = _LOG_SETUP_BOL_ACCOUNT_RE.match(s)
        if m:
            return route_by_bol_account(max(1, int(m.group(1))))
        # Lines without a leading [Sn] still often name session_N.json or contain [Sn] mid-line — route by bol_account.
        m = _LOG_BRACKET_S_ANYWHERE_RE.search(s)
        if m:
            return route_by_bol_account(max(1, int(m.group(1))))
        m = _LOG_SESSION_JSON_RE.search(s)
        if m:
            return route_by_bol_account(max(1, int(m.group(1))))
        return min(max_idx, hint[0])

    def _populate_log_notebook_tabs(self, n_slots: int) -> None:
        n_slots = min(3, max(1, int(n_slots)))
        acc_route = log_route_bol_accounts(n_slots)
        while len(acc_route) < n_slots:
            acc_route.append(len(acc_route) + 1)
        self._log_route_bol_accounts = acc_route[:n_slots]
        _order, self._log_sid_to_tab_idx = log_tabs_sorted_worker_order(
            self._log_route_bol_accounts
        )
        self._log_tab_acc_labels = [
            self._log_route_bol_accounts[wi] for wi in _order
        ]

        log_hdr = (
            f"Logs ({n_slots} tab{'s' if n_slots != 1 else ''} · "
            "tabs left→Acc 1, Acc 2, Acc 3 — not product URL row order)"
        )

        if len(self._log_tabs) == n_slots:
            for tab_pos, wi in enumerate(_order):
                acc = self._log_route_bol_accounts[wi]
                self._log_notebook.tab(tab_pos, text=f" Acc {acc} ")
            self._log_tab_acc_labels = [
                self._log_route_bol_accounts[wi] for wi in _order
            ]
            self._log_labelframe.configure(text=log_hdr)
            return

        for tid in list(self._log_notebook.tabs()):
            tab = self._log_notebook.nametowidget(tid)
            self._log_notebook.forget(tid)
            tab.destroy()
        self._log_tabs.clear()
        self._log_line_counts = [0] * n_slots

        log_kw = dict(
            wrap=tk.NONE,
            font=("Consolas", 9),
            bg="#fafafa",
            fg="#111827",
            insertbackground="#111827",
            relief=tk.FLAT,
            padx=8,
            pady=8,
            height=20,
        )
        for tab_pos, wi in enumerate(_order):
            acc = self._log_route_bol_accounts[wi]
            tab_fr = tk.Frame(self._log_notebook, bg=INNER_BG)
            self._log_notebook.add(tab_fr, text=f" Acc {acc} ")
            st = scrolledtext.ScrolledText(tab_fr, **log_kw)
            st.pack(fill=tk.BOTH, expand=True)
            st.configure(state=tk.DISABLED)
            self._log_tabs.append(st)

        self.log = self._log_tabs[0]
        self._stdout_route_hint[0] = 0
        self._log_labelframe.configure(text=log_hdr)

    def _gui_log(self, text: str) -> None:
        t = text.rstrip("\n")
        if not t.startswith("[GUI]"):
            t = f"[GUI] {t}"
        self._log_queue.put((0, t + "\n"))

    def _append_log_tab(self, tab_idx: int, text: str) -> None:
        if not self._log_tabs:
            return
        tab_idx = min(len(self._log_tabs) - 1, max(0, tab_idx))
        w = self._log_tabs[tab_idx]
        w.configure(state=tk.NORMAL)
        w.insert(tk.END, text)
        if not text.endswith("\n"):
            w.insert(tk.END, "\n")
        extra = text.count("\n") + (0 if text.endswith("\n") else 1)
        self._log_line_counts[tab_idx] += extra
        if self._log_line_counts[tab_idx] > 12000:
            w.delete("1.0", "2500.0")
            self._log_line_counts[tab_idx] = max(1, int(w.index("end-1c").split(".")[0]))
        w.see(tk.END)
        w.configure(state=tk.DISABLED)

    def _clear_log(self) -> None:
        if not self._log_tabs:
            return
        try:
            idx = int(self._log_notebook.index(self._log_notebook.select()))
        except tk.TclError:
            idx = 0
        idx = min(len(self._log_tabs) - 1, max(0, idx))
        w = self._log_tabs[idx]
        w.configure(state=tk.NORMAL)
        w.delete("1.0", tk.END)
        self._log_line_counts[idx] = 0
        w.configure(state=tk.DISABLED)
        acc_l = getattr(self, "_log_tab_acc_labels", []) or []
        acc_h = acc_l[idx] if idx < len(acc_l) else idx + 1
        self._gui_log(f"Log cleared (tab Acc {acc_h}).\n")

    def _drain_log_queue(self) -> None:
        try:
            while True:
                item = self._log_queue.get_nowait()
                if isinstance(item, tuple) and len(item) == 2:
                    tab_i, text = item
                    self._append_log_tab(int(tab_i), text)
                else:
                    self._append_log_tab(0, str(item))
        except queue.Empty:
            pass
        self.after(80, self._drain_log_queue)

    def _set_running_ui(self, running: bool) -> None:
        self.btn_start.configure(state=tk.DISABLED if running else tk.NORMAL)
        self.btn_stop.configure(state=tk.NORMAL if running else tk.DISABLED)
        self.status_var.set("Bot: running" if running else "Bot: stopped")

    def _log_start_binding_summary(self) -> None:
        """Plain-language: which CSV bol_account → which session/proxy/email (parallel cap applied)."""
        cap = _parallel_slot_cap(self._settings)
        rows = read_product_rows_from_disk()
        if not rows:
            self._gui_log(
                "product.csv has no http URLs — save Products tab first or bot will exit.\n",
            )
            return
        n = min(len(rows), cap)
        triples: list[tuple[int, str, int]] = []
        for i in range(n):
            j = min(i, len(rows) - 1)
            url, acc = rows[j]
            triples.append((acc, url, j))
        if not _dotenv_file_truthy("BOL_BROWSER_WORKERS_CSV_ROW_ORDER"):
            triples.sort(key=lambda t: (t[0], t[2]))
        lines: list[str] = []
        for wi, (acc, url, _) in enumerate(triples):
            short = url if len(url) <= 52 else url[:49] + "…"
            lines.append(
                f"W{wi + 1}: **only Acc {acc}** → session_{acc}.json + proxy line {acc} + Email{acc} | {short}"
            )
        mode = "ONE Chromium (single)" if n == 1 else f"{n} Chromium windows (parallel)"
        self._gui_log(f"{mode}. Row binding — {' · '.join(lines)}\n")

    def _start_bot(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            messagebox.showinfo("Already running", "Stop the bot first.")
            return
        if not BOT_SCRIPT.is_file():
            messagebox.showerror("Missing file", str(BOT_SCRIPT))
            return
        self._persist_settings_from_ui()
        self._populate_log_notebook_tabs(_parallel_slot_cap(self._settings))

        self._stop_reader.clear()
        self._stdout_route_hint[0] = 0
        self._log_start_binding_summary()
        self._gui_log("Starting 1.py …\n")

        creationflags = 0
        if sys.platform == "win32":
            creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)

        try:
            self._proc = subprocess.Popen(
                [sys.executable, "-u", str(BOT_SCRIPT)],
                cwd=str(SCRIPT_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
                env=self._child_env(),
            )
        except Exception as e:
            self._proc = None
            messagebox.showerror("Start failed", str(e))
            self._gui_log(f"Start failed: {e}\n")
            return

        self._set_running_ui(True)

        def reader() -> None:
            assert self._proc and self._proc.stdout
            try:
                for line in iter(self._proc.stdout.readline, ""):
                    if self._stop_reader.is_set():
                        break
                    if line:
                        tab_i = self._stdout_line_tab_index(line, self._stdout_route_hint)
                        self._log_queue.put((tab_i, line))
            except Exception as e:
                self._log_queue.put((0, f"\n[GUI] log reader error: {e}\n"))
            finally:
                with contextlib.suppress(Exception):
                    self._proc.stdout.close()

        self._reader_thread = threading.Thread(target=reader, daemon=True)
        self._reader_thread.start()
        self.after(400, self._watch_process_exit)

    def _watch_process_exit(self) -> None:
        if self._proc is None:
            return
        code = self._proc.poll()
        if code is None:
            self.after(400, self._watch_process_exit)
            return
        self._gui_log(f"Bot process exited (code {code}).\n")
        self._proc = None
        self._set_running_ui(False)

    def _stop_bot(self) -> None:
        if self._proc is None or self._proc.poll() is not None:
            self._proc = None
            self._set_running_ui(False)
            return
        self._gui_log("Stop requested.\n")
        self._stop_reader.set()
        with contextlib.suppress(Exception):
            self._proc.terminate()
        with contextlib.suppress(Exception):
            self._proc.wait(timeout=5)
        with contextlib.suppress(Exception):
            if self._proc.poll() is None:
                self._proc.kill()
        self._proc = None
        self._set_running_ui(False)

    def _on_close(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            if not messagebox.askokcancel("Quit", "Bot is still running. Stop it and exit?"):
                return
            self._stop_bot()
        self.destroy()

    def _set_text_widget(self, w: scrolledtext.ScrolledText, content: str) -> None:
        w.configure(state=tk.NORMAL)
        w.delete("1.0", tk.END)
        w.insert("1.0", content)
        w.configure(state=tk.NORMAL)


def _read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.replace("\r\n", "\n"), encoding="utf-8")


def _read_one_line(path: Path) -> str:
    t = _read_text(path).strip().splitlines()
    return t[0] if t else ""


def _write_one_line(path: Path, value: str) -> None:
    v = (value or "").strip()
    if v:
        _write_text(path, v + "\n")
    elif path.exists():
        path.write_text("", encoding="utf-8")


def main() -> None:
    BolControlApp().mainloop()


if __name__ == "__main__":
    main()
