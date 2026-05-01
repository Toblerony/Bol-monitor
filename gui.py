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

# stdout lines from browser_mode: ``[S1]`` … ``[S3]`` → log tabs 1–3; ``[SETUP] Slot N:`` → tab N.
_LOG_SLOT_TAG_RE = re.compile(r"^\[S([1-9]\d*)\]")
_LOG_SETUP_SLOT_LINE_RE = re.compile(r"^\[SETUP\]\s*Slot\s+(\d+)\s*:", re.I)

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

Files next to gui.py / 1.py:
  • product.csv — Bol product URLs (Products tab).
  • proxy.txt — line N ↔ Nth http URL in product.csv (order), up to 3 Chromium slots (BOL_BROWSER_MAX_PARALLEL).
    cookies/session_<n>.json + session_<n>.fingerprint.txt (sha256 + comment); wrong proxy line clears session on Start.
  • proxies.txt — optional failover pool (same format): browser mode only, when login/session breaks (not offline PDP).
  • Accounts — slot 1 uses Email1/Password1 (else Email/Password), slot 2 Email2/Password2, slot 3 Email3/Password3 (.env).
  • Session — auto-saved after login. First run: NL homepage → Inloggen per slot.
  • discord_webhook.txt (+ optional thread ids) — payment / cart alerts.

Bot flow (always Playwright Chromium, visible window): login → monitor PDPs → add to cart → checkout in the
same browser. Payment URL is saved and Discord gets URL + item details.

Polling is randomized (not fixed seconds):
  • Online / in-stock path: random delay between BOL_BROWSER_POLL_ONLINE_MIN … MAX (default 2–5 s).
  • Offline / thin PDP / errors: random delay between BOL_BROWSER_POLL_OFFLINE_MIN … MAX (default 40–60 s).

Checkout login (when Bol asks for Inloggen mid-checkout): delays come from Settings —
pause before filling email/password, pause before pressing Inloggen, settle wait after redirect.

Edit → Save → Start bot. Stop before changing proxy or session files.
"""

# Written to gui_settings.json and passed as env to 1.py (browser bot).
DEFAULT_SETTINGS: dict = {
    "USE_PROXIES": True,
    "BOL_DIAGNOSTIC_LOG": False,
    "SITEMAP_ENABLED": True,
    "BOL_BROWSER_POLL_ONLINE_MIN": "2",
    "BOL_BROWSER_POLL_ONLINE_MAX": "5",
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
            w = csv.DictWriter(f, fieldnames=["product_url"])
            w.writeheader()


def read_product_urls_from_disk() -> list[str]:
    ensure_product_csv_exists()
    try:
        raw = PRODUCTS_PATH.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return []
    urls: list[str] = []
    try:
        reader = csv.DictReader(io.StringIO(raw))
        fn = reader.fieldnames or []
        norm = {(n or "").strip().lstrip("\ufeff").lower(): n for n in fn if n}
        key = norm.get("product_url")
        if key:
            for row in reader:
                u = (row.get(key) or "").strip()
                if u.startswith("http"):
                    urls.append(u)
            if urls:
                return urls
    except csv.Error:
        urls = []

    seen: set[str] = set()
    loose: list[str] = []
    for line in raw.splitlines():
        u = line.strip().strip('"').strip()
        if u.lower().startswith(("http://", "https://")) and u not in seen:
            seen.add(u)
            loose.append(u)
    return loose


def write_product_urls(urls: list[str]) -> None:
    seen: set[str] = set()
    ordered: list[str] = []
    for u in urls:
        u = (u or "").strip()
        if not u.startswith("http") or u in seen:
            continue
        seen.add(u)
        ordered.append(u)
    with PRODUCTS_PATH.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["product_url"])
        w.writeheader()
        for u in ordered:
            w.writerow({"product_url": u})


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
        self._log_line_counts: list[int] = [0, 0, 0]
        self._stdout_route_hint: list[int] = [0]
        self._log_tabs: list[scrolledtext.ScrolledText] = []

        self._settings = _load_json_settings()

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._drain_log_queue)

        self._reload_products_list()
        self._reload_proxies_text()
        self._reload_session_tab()
        self._reload_discord_fields()
        self._reload_keywords_text()
        self._gui_log("Files loaded. Edit tabs → Save → Start bot.\n")

    # ── UI ───────────────────────────────────────────────────────

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

        # — Header (title + status + actions) —
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

        # — One vertical scroll (header fixed): Monitor + How it works, then Logs —
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

        # Fixed height so tabs + help get predictable space; scroll down for full Logs.
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

        log_card = ttk.Labelframe(main_inner, text="Logs (Tab 1–3 ↔ slots 1–3)", padding=6)
        log_card.pack(fill=tk.BOTH, expand=True, pady=(0, 4))

        inner_log = tk.Frame(log_card, bg=INNER_BG, highlightthickness=1, highlightbackground="#e5e7eb")
        inner_log.pack(fill=tk.BOTH, expand=True)

        self._log_notebook = ttk.Notebook(inner_log)
        self._log_notebook.pack(fill=tk.BOTH, expand=True)

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
        for i in range(3):
            tab_fr = tk.Frame(self._log_notebook, bg=INNER_BG)
            self._log_notebook.add(tab_fr, text=f" Tab {i + 1} ")
            st = scrolledtext.ScrolledText(tab_fr, **log_kw)
            st.pack(fill=tk.BOTH, expand=True)
            st.configure(state=tk.DISABLED)
            self._log_tabs.append(st)

        self.log = self._log_tabs[0]

        # Wheel: use winfo_containing + small-delta handling (trackpads often send |delta| < 120).
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

            # Monitor tab inner canvas (Session, Bot settings, …): wheel scrolls this pane, not the main page.
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
        """Keep embedded inner height in sync so the scrollbar reaches the real bottom."""
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
        """Fixed footer (buttons always visible) + scrollable inner area above."""
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
                f"Saved as {PRODUCTS_PATH.name} — one https URL per line "
                "(or CSV with column product_url). Save after edits."
            ),
            font=("Segoe UI", 9),
            style="Monitor.TLabel",
        ).grid(row=0, column=0, sticky="ew", padx=4, pady=(0, 4))

        self.txt_products = scrolledtext.ScrolledText(
            inner,
            height=10,
            font=("Consolas", 9),
            wrap=tk.NONE,
            undo=True,
        )
        self.txt_products.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)

        fr = ttk.Frame(footer, style="Monitor.TFrame")
        fr.pack(fill=tk.X, padx=2, pady=(2, 0))
        ttk.Button(fr, text="Save to product.csv", command=self._products_save).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(fr, text="Import .txt (append new URLs)", command=self._products_import_txt).pack(
            side=tk.LEFT, padx=(0, 6)
        )
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
        ttk.Button(fr, text="Import from file…", command=self._proxies_import).pack(side=tk.LEFT)
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
                "session_<id>.json + session_<id>.fingerprint.txt (open in Notepad — hex + proxy hint)."
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
        ttk.Button(fr, text="Refresh list", command=self._reload_session_tab).pack(side=tk.LEFT)
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
            text="Use proxy.txt (CSV ↔ proxy line ↔ session). Off = direct IP; same slot layout — each tab runs the full S1-style monitor + checkout flow.",
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
            "All slot indices 1…N start together (N = min(rows, proxies, this cap)). Same code every tab — only "
            "row N, proxy line N, session_n.json, and EmailN/PasswordN differ. To run fewer browsers, remove "
            "extra CSV/proxy lines or lower this number — do not mix “only S3” with one window (that broke flow).",
        )

        r = sec(r, "PDP polling — random delay each time (seconds)")
        r = hint(
            r,
            "Between checks the bot sleeps a random value between min and max (uniform). "
            "Online = PDP looks available / add-to-cart possible. Offline = errors, thin page, or offline.",
        )
        r = row(
            r,
            "Online min BOL_BROWSER_POLL_ONLINE_MIN",
            "BOL_BROWSER_POLL_ONLINE_MIN",
            8,
            None,
        )
        r = row(r, "Online max BOL_BROWSER_POLL_ONLINE_MAX", "BOL_BROWSER_POLL_ONLINE_MAX", 8)
        r = hint(r, "Typical range 2–5 s between polls when the listing looks buyable.")
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

    # ── Products ─────────────────────────────────────────────────

    def _urls_from_products_editor(self) -> list[str]:
        raw = self.txt_products.get("1.0", tk.END)
        seen: set[str] = set()
        out: list[str] = []
        for line in raw.splitlines():
            u = line.strip()
            if not u.startswith("http"):
                continue
            if u in seen:
                continue
            seen.add(u)
            out.append(u)
        return out

    def _reload_products_list(self) -> None:
        urls = read_product_urls_from_disk()
        body = "\n".join(urls)
        if body:
            body += "\n"
        self._set_text_widget(self.txt_products, body)
        self._gui_log(f"Reloaded product editor ({len(urls)} URL(s)) from disk.\n")

    def _products_save(self) -> None:
        urls = self._urls_from_products_editor()
        if not urls:
            if not messagebox.askyesno(
                "Empty",
                "No lines starting with http. Save anyway? (will clear product.csv to header only.)",
            ):
                return
        try:
            write_product_urls(urls)
        except OSError as e:
            messagebox.showerror("Save failed", str(e))
            return
        self._gui_log(f"Saved {PRODUCTS_PATH.name} ({len(urls)} product URL(s)).\n")

    def _products_import_txt(self) -> None:
        path = filedialog.askopenfilename(
            title="Import URLs",
            filetypes=[("Text", "*.txt"), ("All", "*.*")],
        )
        if not path:
            return
        try:
            raw = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            messagebox.showerror("Read failed", str(e))
            return
        existing = set(self._urls_from_products_editor())
        block: list[str] = []
        for line in raw.splitlines():
            u = line.strip()
            if u.startswith("http") and u not in existing:
                existing.add(u)
                block.append(u)
        if not block:
            messagebox.showinfo("Import", "No new http URLs in that file.")
            return
        insert = "\n".join(block) + "\n"
        self.txt_products.insert(tk.END, insert)
        self.txt_products.see(tk.END)
        self._gui_log(f"Appended {len(block)} URL(s) from file (press Save to write CSV).\n")

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
                legacy = sorted(SESSION_DIR.glob("session_*.proxy.fp"))
                all_files = sorted(set(js) | set(fps) | set(legacy), key=lambda x: x.name)
                if all_files:
                    for p in all_files:
                        try:
                            sz = p.stat().st_size
                            lines.append(f"  • {p.name}  ({sz:,} bytes)")
                        except OSError:
                            lines.append(f"  • {p.name}")
                else:
                    lines.append(
                        "  (none yet — first Start creates session_<id>.json + session_<id>.fingerprint.txt)"
                    )
            else:
                lines.append("  (folder created on first login save)")
        except OSError as e:
            lines.append(f"  Error: {e}")
        lines.extend(
            [
                "",
                "Reset login: Stop bot → delete session_*.json (+ session_*.fingerprint.txt) → Start.",
                "",
                "Env: BOL_BROWSER_MAX_PARALLEL (default 3) caps Chromium count; sessions session_1… from bind.",
            ]
        )
        body = "\n".join(lines) + "\n"
        self.txt_session_info.configure(state=tk.NORMAL)
        self.txt_session_info.delete("1.0", tk.END)
        self.txt_session_info.insert("1.0", body)
        self.txt_session_info.configure(state=tk.DISABLED)

    # ── Proxies / discord / keywords ───────────────────────────────

    def _reload_proxies_text(self) -> None:
        self._set_text_widget(self.txt_proxies, _read_text(PROXY_PATH))

    def _proxies_save(self) -> None:
        _write_text(PROXY_PATH, self.txt_proxies.get("1.0", tk.END))
        self._gui_log(f"Saved {PROXY_PATH.name}.\n")

    def _proxies_import(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("Text", "*.txt"), ("All", "*.*")])
        if path:
            self._set_text_widget(self.txt_proxies, Path(path).read_text(encoding="utf-8", errors="replace"))
            self._gui_log(f"Loaded proxies from {path} (not saved until you press Save).\n")

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
        # Stable UTF-8 for 1.py on Windows (avoids cp1252 print / pipe issues).
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8:replace"
        for k, v in self._settings.items():
            if isinstance(v, bool):
                env[k] = "1" if v else "0"
            else:
                env[k] = str(v)
        return env

    def _load_bol_logo_image(self) -> None:
        try:
            import urllib.request

            url = "https://www.google.com/s2/favicons?domain=bol.com&sz=64"
            req = urllib.request.Request(url, headers={"User-Agent": "BolMonitorGUI/1"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                blob = resp.read()
            self._bol_photo = tk.PhotoImage(data=blob)
            self._bol_logo_label.configure(image=self._bol_photo)
        except Exception:
            self._bol_logo_label.configure(
                text=" bol ",
                font=("Segoe UI", 8, "bold"),
                fg="white",
                bg="#0064c8",
            )

    # ── Log / bot process ─────────────────────────────────────────

    @staticmethod
    def _stdout_line_tab_index(line: str, hint: list[int]) -> int:
        """Map browser stdout line to log tab 0..2 from ``[Sn]`` or ``[SETUP] Slot n:``."""
        s = line.rstrip("\r\n")
        m = _LOG_SLOT_TAG_RE.match(s)
        if m:
            sid = min(3, max(1, int(m.group(1))))
            idx = sid - 1
            hint[0] = idx
            return idx
        m = _LOG_SETUP_SLOT_LINE_RE.match(s)
        if m:
            sid = min(3, max(1, int(m.group(1))))
            idx = sid - 1
            hint[0] = idx
            return idx
        return hint[0]

    def _gui_log(self, text: str) -> None:
        t = text.rstrip("\n")
        if not t.startswith("[GUI]"):
            t = f"[GUI] {t}"
        self._log_queue.put((0, t + "\n"))

    def _append_log_tab(self, tab_idx: int, text: str) -> None:
        tab_idx = min(2, max(0, tab_idx))
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
        try:
            idx = int(self._log_notebook.index(self._log_notebook.select()))
        except tk.TclError:
            idx = 0
        idx = min(2, max(0, idx))
        w = self._log_tabs[idx]
        w.configure(state=tk.NORMAL)
        w.delete("1.0", tk.END)
        self._log_line_counts[idx] = 0
        w.configure(state=tk.DISABLED)
        self._gui_log(f"Log cleared (Tab {idx + 1}).\n")

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

    def _start_bot(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            messagebox.showinfo("Already running", "Stop the bot first.")
            return
        if not BOT_SCRIPT.is_file():
            messagebox.showerror("Missing file", str(BOT_SCRIPT))
            return
        self._persist_settings_from_ui()

        self._stop_reader.clear()
        self._stdout_route_hint[0] = 0
        self._gui_log("Starting 1.py ...\n")

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
                        tab_i = BolControlApp._stdout_line_tab_index(line, self._stdout_route_hint)
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

    # ── text helpers ─────────────────────────────────────────────

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
