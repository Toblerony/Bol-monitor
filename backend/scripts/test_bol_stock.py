#!/usr/bin/env python3
"""
Verify Bol.com stock detection on REAL product URLs.

IMPORTANT: Bol does NOT use Target's Redsky API.
The monitor reads bol.com PDP HTML + embedded JSON (availability, sellingPrice,
JSON-LD, add-to-cart markers) — same as this script.

Usage (from backend/):
  python scripts/test_bol_stock.py              # auto-find 2 in-stock + 2 OOS
  python scripts/test_bol_stock.py --url URL    # test one URL
  python scripts/test_bol_stock.py --urls-file urls.txt

Uses Settings → Login to Bol session cookies + proxies from DB when available.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.config import get_settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.services.bol_pdp_parser import (  # noqa: E402
    HEADERS,
    _classify_stock,
    _extract_title,
    extract_all_categories,
    parse_product_page,
    product_status_string,
)
from app.services.bol_session import restore_session_file_from_db, session_file  # noqa: E402
from app.services.proxy_client import get_proxy_pool, parse_proxy_lines  # noqa: E402
from app.services.proxy_service import load_pool_from_db  # noqa: E402
from app.models import MonitoringSetting  # noqa: E402

_CACHED_COOKIE: str | None = None


def _cookie_header() -> str:
    """Load bol.com cookies once (local file, DB restore only if missing)."""
    global _CACHED_COOKIE
    if _CACHED_COOKIE is not None:
        return _CACHED_COOKIE

    path = session_file()
    if not path.is_file() or path.stat().st_size < 20:
        try:
            restore_session_file_from_db()
        except Exception as exc:
            print(f"  Warning: session DB restore failed ({exc})", flush=True)

    if not path.is_file():
        _CACHED_COOKIE = ""
        return _CACHED_COOKIE

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _CACHED_COOKIE = ""
        return _CACHED_COOKIE

    parts: list[str] = []
    for c in data.get("cookies") or []:
        if not isinstance(c, dict):
            continue
        domain = (c.get("domain") or "").lower()
        if "bol.com" not in domain:
            continue
        name, value = c.get("name"), c.get("value")
        if name and value is not None:
            parts.append(f"{name}={value}")
    _CACHED_COOKIE = "; ".join(parts)
    return _CACHED_COOKIE


def _fetch_html(url: str, *, timeout: float = 12.0, pool=None) -> tuple[int, str]:
    headers = {
        **HEADERS,
        "Referer": "https://www.bol.com/nl/nl/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    cookie = _cookie_header()
    if cookie:
        headers["Cookie"] = cookie

    if pool is not None and pool.enabled:
        from app.services.proxy_client import fetch_url

        code, body, err = fetch_url(
            url,
            pool=pool,
            timeout=timeout,
            headers=headers,
            max_attempts=min(2, max(1, pool.count)),
        )
        if err and not body:
            return 0, f"<!-- error: {err} -->"
        return int(code), body.decode("utf-8", errors="ignore")

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = int(getattr(resp, "status", 200) or 200)
            raw = resp.read(1_500_000)
        return code, raw.decode("utf-8", errors="ignore")
    except Exception as exc:
        return 0, f"<!-- error: {exc} -->"


def _extract_jsonld_blocks(html: str) -> list[dict]:
    out: list[dict] = []
    for m in re.finditer(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.I | re.S,
    ):
        try:
            out.append(json.loads((m.group(1) or "").strip()))
        except json.JSONDecodeError:
            continue
    return out


def _find_availability_in_obj(obj, path: str = "") -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            if k.lower() in ("availability", "offeravailability") and isinstance(v, str):
                hits.append((p, v))
            hits.extend(_find_availability_in_obj(v, p))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            hits.extend(_find_availability_in_obj(item, f"{path}[{i}]"))
    return hits


def _page_response_summary(html: str) -> dict:
    """What the monitor effectively reads from the Bol PDP (not Redsky)."""
    summary: dict = {}
    if not html or html.startswith("<!-- error"):
        return summary

    blocks = _extract_jsonld_blocks(html)
    summary["json_ld_blocks"] = len(blocks)
    avail: list[tuple[str, str]] = []
    for b in blocks:
        avail.extend(_find_availability_in_obj(b))
    summary["json_ld_availability"] = avail[:6]

    for pat in (
        r'"availability"\s*:\s*"([^"]+)"',
        r'"offerAvailability"\s*:\s*"([^"]+)"',
        r'"stockAvailability"\s*:\s*"([^"]+)"',
    ):
        m = re.search(pat, html, re.I)
        if m:
            summary.setdefault("embedded_json", []).append(m.group(0)[:120])

    m = re.search(r'"sellingPrice"\s*:\s*\{[^}]{0,300}\}', html)
    if m:
        summary["selling_price_snippet"] = m.group(0)[:180]

    hl = html.lower()
    summary["html_markers"] = {
        "in_winkelwagen": "in winkelwagen" in hl,
        "op_voorraad": "op voorraad" in hl and "niet op voorraad" not in hl,
        "niet_op_voorraad": "niet op voorraad" in hl,
        "ip_geblokkeerd": "ip-geblokkeerd" in hl,
    }
    return summary


def analyze_url(url: str, *, label: str = "", pool=None) -> dict:
    code, html = _fetch_html(url, pool=pool)
    if html.startswith("<!-- error") or code != 200:
        return {
            "label": label,
            "url": url,
            "http_status": code,
            "status": "offline",
            "title": "",
            "error": html,
            "page_response": {},
        }

    parsed = parse_product_page(url, html, http_status=code)
    status = product_status_string(parsed)
    offline, oos, instock = _classify_stock(html)

    return {
        "label": label,
        "url": url,
        "http_status": code,
        "status": status,
        "title": parsed.title,
        "price_text": parsed.price_text,
        "categories": extract_all_categories(html)[:5],
        "offline": offline,
        "online_oos": oos and not instock,
        "in_stock": instock,
        "page_response": _page_response_summary(html),
        "error": parsed.error,
    }


def _print_result(r: dict) -> None:
    print("-" * 70)
    print(f"  [{r.get('label') or 'product'}]")
    print(f"  URL: {r['url']}")
    print(f"  HTTP {r['http_status']}  ->  bot status: {r['status'].upper()}")
    if r.get("title"):
        print(f"  Title: {r['title'][:90]}")
    if r.get("price_text"):
        print(f"  Price: EUR {r['price_text']}")
    if r.get("categories"):
        print(f"  Categories: {' | '.join(r['categories'][:4])}")
    if r.get("error"):
        print(f"  Error: {str(r['error'])[:100]}")

    pr = r.get("page_response") or {}
    if pr:
        print("  Bol PDP response (embedded JSON + HTML — NOT Redsky):")
        for k, v in (pr.get("html_markers") or {}).items():
            if v:
                print(f"    marker: {k}")
        for item in pr.get("embedded_json") or []:
            print(f"    {item}")
        for path, val in pr.get("json_ld_availability") or []:
            print(f"    JSON-LD {path}: {val}")
        if pr.get("selling_price_snippet"):
            print(f"    {pr['selling_price_snippet']}")


def _sitemap_product_urls(limit: int = 80) -> list[str]:
    req = urllib.request.Request(
        "https://www.bol.com/sitemap/nl-nl/product-1",
        headers={**HEADERS, "User-Agent": HEADERS["User-Agent"]},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read(800_000).decode("utf-8", errors="ignore")
    urls = re.findall(r"<loc>(https://www\.bol\.com/nl/nl/p/[^<]+)</loc>", body)
    return urls[:limit]


def auto_find_products(pool=None, max_tries: int = 25) -> list[dict]:
    """Scan real sitemap URLs until 2 in_stock + 2 online_oos found."""
    instock: list[dict] = []
    oos: list[dict] = []
    urls = _sitemap_product_urls(max_tries + 20)
    for i, url in enumerate(urls, start=1):
        if len(instock) >= 2 and len(oos) >= 2:
            break
        if len(instock) + len(oos) >= max_tries:
            break
        if i == 1 or i % 5 == 0:
            print(
                f"  ... scan {i}/{len(urls)} — "
                f"{len(instock)} in-stock, {len(oos)} OOS so far",
                flush=True,
            )
        r = analyze_url(url, pool=pool)
        st = r["status"]
        if st == "offline":
            continue
        if st == "in_stock" and len(instock) < 2:
            r["label"] = f"IN_STOCK #{len(instock) + 1}"
            instock.append(r)
            _print_result(r)
        elif st == "online_oos" and len(oos) < 2:
            r["label"] = f"ONLINE_OOS #{len(oos) + 1}"
            oos.append(r)
            _print_result(r)
    return instock + oos


def _load_pool():
    db = SessionLocal()
    try:
        mon = db.query(MonitoringSetting).first()
        if mon and mon.use_proxies and parse_proxy_lines(mon.proxy_lines or ""):
            return load_pool_from_db(db)
    finally:
        db.close()
    settings = get_settings()
    pf = settings.data_dir / "proxy.txt"
    if pf.is_file():
        lines = pf.read_text(encoding="utf-8")
        return get_proxy_pool(lines, True)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Test Bol stock on real URLs")
    parser.add_argument("--url", action="append", default=[], help="Test specific URL(s)")
    parser.add_argument("--urls-file", type=Path, help="File with one URL per line")
    parser.add_argument("--auto", action="store_true", help="Find 2 in-stock + 2 OOS from sitemap")
    parser.add_argument("--no-proxy", action="store_true", help="Direct fetch (session cookies only)")
    args = parser.parse_args()

    print("=" * 70, flush=True)
    print("  Bol stock test — PDP HTML + embedded JSON")
    print("  (Target uses Redsky API — Bol does NOT)")
    print("=" * 70, flush=True)

    pool = None if args.no_proxy else _load_pool()
    has_cookie = bool(_cookie_header())
    print(f"  Session cookies: {'yes' if has_cookie else 'no (Settings → Login to Bol if 403)'}")
    print(f"  Proxy pool: {'yes' if pool and pool.enabled else 'no'}")
    print()

    if args.auto or (not args.url and not args.urls_file):
        print("  Scanning real bol.com sitemap for 2 IN_STOCK + 2 ONLINE_OOS...")
        print()
        found = auto_find_products(pool=pool)
        print("=" * 70, flush=True)
        print(f"  Found {len(found)} products with clear stock status")
        if len(found) < 4:
            print("  Tip: enable proxies or Settings → Login to Bol if many URLs show offline/403")
        print("=" * 70, flush=True)
        return 0 if found else 1

    urls: list[tuple[str, str]] = []
    for u in args.url:
        urls.append(("custom", u))
    if args.urls_file and args.urls_file.is_file():
        for line in args.urls_file.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("http"):
                urls.append(("file", s))

    for label, url in urls:
        r = analyze_url(url, label=label, pool=pool)
        _print_result(r)

    print("=" * 70, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
