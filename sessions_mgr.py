"""
Multi-slot session pool: one sticky NL proxy + cookie jar per slot, health states, persistence.

Per-slot folder (each slot = one sticky proxy + its own cookie jar):
  sessions/slot_00/proxy.txt    # one line host:port:user:pass (same index as root proxy.txt)
  sessions/slot_00/cookies.txt  # JSON cookie export for that proxy only

.env (login refresh — any of these pairs):
  BOL_EMAIL= / BOL_PASSWORD=
  Email= / Password=              # aliases → mapped to BOL_* at load

Client env:
  USE_SESSION_POOL=1   — enable sessions/ pool mode
  AUTO_SESSION_SLOTS=1 — auto-create sessions/slot_00.. from proxy.txt + copy cookies.txt into each
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import shutil
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

STATE_ACTIVE = "ACTIVE"
STATE_EXPIRED = "EXPIRED"
STATE_BLOCKED = "BLOCKED"
STATE_DEAD = "DEAD"

# Injected by 1.py at startup: (proxies, cookies, sticky_single) -> manager
ProxyManagerFactory = Callable[[list, dict, bool], Any]
_PM_FACTORY: ProxyManagerFactory | None = None

_LOGIN_RECOVERY_FN: Callable[[Any], bool] | None = None


def set_login_recovery(fn: Callable[[Any], bool] | None) -> None:
    """1.py sets (BolSessionSlot) -> True if cookies refreshed via password login."""
    global _LOGIN_RECOVERY_FN
    _LOGIN_RECOVERY_FN = fn


def set_proxy_manager_factory(fn: ProxyManagerFactory | None) -> None:
    global _PM_FACTORY
    _PM_FACTORY = fn


def apply_bol_env_aliases() -> None:
    """If BOL_EMAIL/BOL_PASSWORD unset, copy from common .env key names (Email/Password, etc.)."""
    if not (os.getenv("BOL_EMAIL") or "").strip():
        for k in ("Email", "EMAIL", "BolEmail"):
            v = (os.getenv(k) or "").strip()
            if v:
                os.environ["BOL_EMAIL"] = v
                break
    if not (os.getenv("BOL_PASSWORD") or "").strip():
        for k in ("Password", "PASSWORD", "BolPassword"):
            v = (os.getenv(k) or "").strip()
            if v:
                os.environ["BOL_PASSWORD"] = v
                break


def load_env_file(path: str | None = None) -> None:
    """Light .env parser (no python-dotenv). Does not override existing os.environ."""
    base = path or os.path.join(os.getcwd(), ".env")
    if not os.path.isfile(base):
        apply_bol_env_aliases()
        return
    try:
        with open(base, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except OSError:
        pass
    apply_bol_env_aliases()


def _scrub_proxy_field(raw: str) -> str:
    s = (raw or "").strip()
    s = re.sub(r"\{%[\s\S]*?%\}\s*", "", s)
    return s.strip()


def _scrub_proxy_txt_line_raw(line: str) -> str:
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


def _parse_proxy_txt_line(line: str) -> str | None:
    line = _scrub_proxy_txt_line_raw(line)
    if not line:
        return None
    parts = line.split(":")
    if len(parts) != 4:
        return None
    return _proxy_line_to_url(parts[0], parts[1], parts[2], parts[3])


def load_cookie_jar_json(path: str) -> dict[str, str]:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, list):
        raise ValueError(f"{path} must be a JSON array of cookie objects.")
    return {str(c["name"]): str(c["value"]) for c in raw if isinstance(c, dict) and "name" in c and "value" in c}


def save_cookie_jar_json(path: str, jar: dict[str, str], preserve_shape_path: str | None = None) -> None:
    """Write cookies as JSON array; merge values into existing file shape when possible."""
    template_path = preserve_shape_path or path
    entries: list[dict] = []
    try:
        with open(template_path, encoding="utf-8") as f:
            prev = json.load(f)
        if isinstance(prev, list):
            for c in prev:
                if not isinstance(c, dict) or "name" not in c:
                    continue
                name = str(c["name"])
                c2 = dict(c)
                if name in jar:
                    c2["value"] = jar[name]
                entries.append(c2)
            known = {str(c.get("name")) for c in entries if isinstance(c, dict)}
            for name, value in sorted(jar.items()):
                if name not in known:
                    entries.append(
                        {
                            "domain": ".bol.com",
                            "path": "/",
                            "name": name,
                            "value": value,
                            "secure": True,
                        }
                    )
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        entries = [
            {"domain": ".bol.com", "path": "/", "name": k, "value": v, "secure": True}
            for k, v in sorted(jar.items())
        ]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)


def _read_root_proxy_pairs(proxy_file: str) -> list[tuple[str, str]]:
    """Return [(proxy_url, scrubbed_raw_line), ...] in file order — raw line is what we store in slot proxy.txt."""
    if not os.path.isfile(proxy_file):
        return []
    out: list[tuple[str, str]] = []
    with open(proxy_file, encoding="utf-8") as f:
        for line in f:
            scrubbed = _scrub_proxy_txt_line_raw(line)
            if not scrubbed or scrubbed.startswith("#"):
                continue
            u = _parse_proxy_txt_line(line)
            if u:
                out.append((u, scrubbed))
    return out


def _read_root_proxy_lines(proxy_file: str) -> list[str]:
    return [u for u, _ in _read_root_proxy_pairs(proxy_file)]


def ensure_multi_session_layout(
    cwd: str,
    root_proxy_file: str,
    template_cookie_file: str,
) -> int:
    """
    Create sessions/slot_00 .. slot_N-1 with proxy.txt + cookies.txt per line in root proxy.txt.
    - Writes sessions/slot_XX/proxy.txt (one line) so each folder is self-contained: proxy + jar together.
    - New cookies.txt: if BOL_EMAIL+BOL_PASSWORD (or Email+Password aliases) are set → empty [] so each
      slot can log in through its own proxy; else copy root template_cookie_file when present.
    """
    load_env_file(os.path.join(cwd, ".env"))
    pairs = _read_root_proxy_pairs(root_proxy_file)
    if not pairs:
        print("  [SESSION] ensure_multi_session_layout: no proxy lines in proxy file.", flush=True)
        return 0
    sessions_dir = os.path.join(cwd, "sessions")
    Path(sessions_dir).mkdir(parents=True, exist_ok=True)
    tpl_ok = os.path.isfile(template_cookie_file)
    cred_login = bool(
        (os.getenv("BOL_EMAIL") or "").strip() and (os.getenv("BOL_PASSWORD") or "").strip()
    )
    for i, (_url, raw_line) in enumerate(pairs):
        name = f"slot_{i:02d}"
        d = os.path.join(sessions_dir, name)
        Path(d).mkdir(parents=True, exist_ok=True)
        ppath = os.path.join(d, "proxy.txt")
        try:
            with open(ppath, "w", encoding="utf-8") as pf:
                pf.write(raw_line.strip() + "\n")
        except OSError as e:
            print(f"  [SESSION] write {ppath} failed: {e}", flush=True)
        cpath = os.path.join(d, "cookies.txt")
        if not os.path.isfile(cpath):
            if cred_login:
                with open(cpath, "w", encoding="utf-8") as f:
                    json.dump([], f)
                print(
                    f"  [SESSION] Created empty {cpath} (credentials in .env → per-slot login fills this)",
                    flush=True,
                )
            elif tpl_ok:
                shutil.copy2(template_cookie_file, cpath)
                print(f"  [SESSION] Created {cpath} (from template)", flush=True)
            else:
                with open(cpath, "w", encoding="utf-8") as f:
                    json.dump([], f)
                print(f"  [SESSION] Created empty {cpath} (no template)", flush=True)
    print(f"  [SESSION] {len(pairs)} slot folder(s) with proxy.txt under {sessions_dir}/", flush=True)
    return len(pairs)


class BolSessionSlot:
    """One sticky proxy + cookie jar + ProxyManager. Thread-safe state and disk persistence."""

    def __init__(
        self,
        slot_id: str,
        proxy_url: str,
        cookies: dict[str, str],
        cookies_path: str,
    ) -> None:
        self.slot_id = slot_id
        self.proxy_url = proxy_url
        self.cookies_path = os.path.abspath(cookies_path)
        self.cookies = cookies
        self.state = STATE_ACTIVE
        self.login_failures = 0
        self._lock = threading.Lock()
        self._last_health_ts = 0.0
        self._last_persist_ts = 0.0
        if _PM_FACTORY is None:
            raise RuntimeError("sessions_mgr.set_proxy_manager_factory() was not called")
        self._pm = _PM_FACTORY([proxy_url], cookies, sticky_single=True)

    @property
    def pm(self):
        return self._pm

    def log_line(self, http_code: int | str, url_hint: str, note: str = "") -> None:
        host = self.proxy_url.split("@")[-1][:48] if self.proxy_url else "?"
        msg = (
            f"[SESSION] id={self.slot_id} proxy={host!r} http={http_code} "
            f"state={self.state} url={url_hint[:100]!r}"
        )
        if note:
            msg += f" | {note}"
        print(msg, flush=True)

    def on_http_status(self, code: int, url: str = "") -> None:
        with self._lock:
            if code in (401, 403):
                if self.state != STATE_DEAD:
                    self.state = STATE_BLOCKED
                self.log_line(code, url, "classified BLOCKED")

    def on_thin_product_page(self, html_len: int, url: str = "") -> None:
        """PDP missing buy data / shell HTML — likely cookie–proxy mismatch or bot wall."""
        with self._lock:
            if self.state != STATE_DEAD:
                self.state = STATE_EXPIRED
                self.log_line(200, url, f"weak PDP len={html_len} → EXPIRED")

    def on_login_ok(self, email: str | None) -> None:
        with self._lock:
            if email:
                self.state = STATE_ACTIVE
                self.login_failures = 0
            else:
                self.state = STATE_EXPIRED

    def on_login_redirect(self) -> None:
        """Orders probe shows login page — session expired; do not bump login_failures."""
        with self._lock:
            if self.state != STATE_DEAD:
                self.state = STATE_EXPIRED

    def on_login_failed(self) -> None:
        """Explicit failed re-auth / recovery — counts toward DEAD threshold."""
        with self._lock:
            self.login_failures += 1
            if self.state != STATE_DEAD:
                self.state = STATE_EXPIRED

    def mark_dead(self, reason: str) -> None:
        with self._lock:
            self.state = STATE_DEAD
            self.log_line("-", "", f"DEAD: {reason}")

    def throttled_persist(self, min_interval: float = 45.0) -> None:
        now = time.time()
        if now - self._last_persist_ts < min_interval:
            return
        self._last_persist_ts = now
        try:
            with self._lock:
                save_cookie_jar_json(self.cookies_path, self.cookies, preserve_shape_path=self.cookies_path)
        except OSError as e:
            print(f"  [SESSION {self.slot_id}] persist cookies failed: {e}", flush=True)

    def reload_cookies_from_disk(self) -> None:
        with self._lock:
            self.cookies.clear()
            self.cookies.update(load_cookie_jar_json(self.cookies_path))
        if _PM_FACTORY is None:
            raise RuntimeError("sessions_mgr.set_proxy_manager_factory() was not called")
        self._pm = _PM_FACTORY([self.proxy_url], self.cookies, sticky_single=True)

    def try_recover(self) -> bool:
        """
        Attempt recovery for EXPIRED/BLOCKED. Full bol password login is not shipped here —
        reload cookies from disk (manual refresh) or require BOL_EMAIL/BOL_PASSWORD hook later.
        """
        with self._lock:
            if self.state == STATE_DEAD:
                return False
        email = os.getenv("BOL_EMAIL", "").strip()
        password = os.getenv("BOL_PASSWORD", "").strip()
        if email and password and _LOGIN_RECOVERY_FN is not None:
            try:
                if _LOGIN_RECOVERY_FN(self):
                    self.reload_cookies_from_disk()
                    self.log_line("-", "", "password login refreshed cookies → ACTIVE")
                    with self._lock:
                        self.state = STATE_ACTIVE
                        self.login_failures = 0
                    return True
            except Exception as e:
                print(f"  [SESSION {self.slot_id}] login recovery failed: {e}", flush=True)
        elif email and password:
            print(
                f"  [SESSION {self.slot_id}] BOL_EMAIL/BOL_PASSWORD set but login handler not wired.",
                flush=True,
            )
        try:
            self.reload_cookies_from_disk()
        except Exception as e:
            print(f"  [SESSION {self.slot_id}] reload cookies failed: {e}", flush=True)
            return False
        self.log_line("-", "", "reloaded cookies from disk → ACTIVE probe next")
        with self._lock:
            self.state = STATE_ACTIVE
        return True


class SessionPool:
    def __init__(self, slots: list[BolSessionSlot]) -> None:
        self.slots = slots

    def __len__(self) -> int:
        return len(self.slots)

    def slot_for_index(self, i: int) -> BolSessionSlot:
        return self.slots[i % len(self.slots)]

    @staticmethod
    def from_legacy(proxy_lines: list[str], cookies: dict[str, str], cookies_path: str) -> SessionPool:
        if not proxy_lines:
            raise ValueError("no proxy lines")
        slot = BolSessionSlot("legacy", proxy_lines[0], dict(cookies), cookies_path)
        return SessionPool([slot])

    @staticmethod
    def load_from_sessions_dir(
        cwd: str,
        root_proxy_file: str,
        _fallback_cookies: str,
    ) -> SessionPool | None:
        sessions_dir = os.path.join(cwd, "sessions")
        if not os.path.isdir(sessions_dir):
            return None
        subdirs = sorted(
            d
            for d in os.listdir(sessions_dir)
            if os.path.isdir(os.path.join(sessions_dir, d)) and not d.startswith(".")
        )
        slots: list[BolSessionSlot] = []
        root_lines = _read_root_proxy_lines(root_proxy_file)
        for idx, name in enumerate(subdirs):
            d = os.path.join(sessions_dir, name)
            cpath = os.path.join(d, "cookies.txt")
            if not os.path.isfile(cpath):
                continue
            try:
                jar = load_cookie_jar_json(cpath)
            except (OSError, json.JSONDecodeError, ValueError) as e:
                print(f"  [!] Skip session folder {name!r}: {e}", flush=True)
                continue
            p_local = os.path.join(d, "proxy.txt")
            proxy_url: str | None = None
            if os.path.isfile(p_local):
                with open(p_local, encoding="utf-8") as pf:
                    for line in pf:
                        proxy_url = _parse_proxy_txt_line(line)
                        if proxy_url:
                            break
            if not proxy_url and idx < len(root_lines):
                proxy_url = root_lines[idx]
            if not proxy_url and root_lines:
                proxy_url = root_lines[0]
            if not proxy_url:
                print(f"  [!] Skip session folder {name!r}: no proxy line", flush=True)
                continue
            slots.append(BolSessionSlot(name, proxy_url, jar, cpath))
        if not slots:
            return None
        print(f"  [SESSION] Loaded {len(slots)} slot(s) from {sessions_dir}/", flush=True)
        return SessionPool(slots)
