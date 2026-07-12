"""Fetch and parse bol.com product pages (HTTP)."""
from __future__ import annotations

import html as html_lib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any
from urllib.request import Request

from app.services.proxy_client import ProxyPool, fetch_url

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
}


@dataclass
class ProductPageData:
    url: str
    http_status: int = 0
    title: str = ""
    price_text: str | None = None
    price_value: float | None = None
    categories: list[str] = field(default_factory=list)
    brand: str = ""
    product_type: str = ""
    release_year: str = ""
    offline: bool = True
    in_stock: bool = False
    online_oos: bool = False
    raw_categories_text: str = ""
    error: str = ""


def normalize_text(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip()


def parse_price_value(price_text: str | None) -> float | None:
    if not price_text:
        return None
    m = re.search(r"([0-9]+)[.,]([0-9]{2})", price_text.replace(" ", ""))
    if not m:
        m = re.search(r"([0-9]+)", price_text.replace(" ", ""))
        if m:
            return float(m.group(1))
        return None
    return float(f"{m.group(1)}.{m.group(2)}")


def extract_product_id(url: str) -> str | None:
    m = re.search(r"/(\d{10,})", url or "")
    return m.group(1) if m else None


def _fetch_html(
    url: str,
    timeout: float = 15.0,
    opener=None,
    pool: ProxyPool | None = None,
) -> tuple[int, str]:
    if pool is not None and pool.enabled:
        code, body, err = fetch_url(
            url,
            pool=pool,
            timeout=timeout,
            headers={**HEADERS, "Referer": "https://www.bol.com/nl/nl/"},
            max_attempts=max(3, pool.count),
        )
        if err and not body:
            return 0, f"<!-- error: {err} -->"
        return int(code), body.decode("utf-8", errors="ignore")

    req = Request(url, headers={**HEADERS, "Referer": "https://www.bol.com/nl/nl/"}, method="GET")
    try:
        from urllib.request import urlopen

        if opener is not None:
            resp = opener.open(req, timeout=timeout)
        else:
            resp = urlopen(req, timeout=timeout)
        with resp as r:
            code = getattr(r, "status", 200) or 200
            raw = r.read(1_200_000)
        return int(code), raw.decode("utf-8", errors="ignore")
    except Exception as exc:
        return 0, f"<!-- error: {exc} -->"


def _extract_title(page_html: str) -> str:
    m = re.search(r"<title[^>]*>\s*([^<]{1,500}?)\s*</title>", page_html, re.I | re.S)
    if not m:
        return ""
    t = html_lib.unescape(re.sub(r"\s+", " ", m.group(1)).strip())
    for suffix in (" | bol.com", " - bol.com", " | Bol"):
        if t.lower().endswith(suffix.lower()):
            t = t[: -len(suffix)].strip()
    return t


def _extract_spec_field(page_html: str, label: str) -> str:
    patterns = [
        rf">{re.escape(label)}<[^>]*>[^<]*</[^>]+>\s*<[^>]+>([^<]+)<",
        rf"{re.escape(label)}[^<]*</[^>]+>\s*<[^>]+>([^<]+)<",
        rf'"{re.escape(label)}"\s*:\s*"([^"]+)"',
        rf'"{re.escape(label.lower())}"\s*:\s*"([^"]+)"',
    ]
    for pat in patterns:
        m = re.search(pat, page_html, re.I | re.S)
        if m:
            return html_lib.unescape(m.group(1).strip())
    return ""


def _walk_jsonld(node: Any, cats: list[str], seen: set[str]) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_jsonld(item, cats, seen)
        return
    if not isinstance(node, dict):
        return

    node_type = node.get("@type")
    types = [node_type] if isinstance(node_type, str) else list(node_type or [])

    if "BreadcrumbList" in types:
        for el in node.get("itemListElement") or []:
            if not isinstance(el, dict):
                continue
            name = el.get("name")
            if isinstance(name, str) and name.strip():
                n = name.strip()
                if n not in seen:
                    seen.add(n)
                    cats.append(n)
            item = el.get("item")
            if isinstance(item, dict):
                iname = item.get("name")
                if isinstance(iname, str) and iname.strip():
                    n = iname.strip()
                    if n not in seen:
                        seen.add(n)
                        cats.append(n)

    category = node.get("category")
    if isinstance(category, str):
        for part in re.split(r"[>/|•]+", category):
            p = part.strip()
            if p and p not in seen:
                seen.add(p)
                cats.append(p)
    elif isinstance(category, list):
        for item in category:
            if isinstance(item, str) and item.strip():
                p = item.strip()
                if p not in seen:
                    seen.add(p)
                    cats.append(p)
            elif isinstance(item, dict):
                name = item.get("name")
                if isinstance(name, str) and name.strip():
                    p = name.strip()
                    if p not in seen:
                        seen.add(p)
                        cats.append(p)

    for value in node.values():
        if isinstance(value, (dict, list)):
            _walk_jsonld(value, cats, seen)


def _extract_jsonld_categories(page_html: str) -> list[str]:
    cats: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        page_html,
        re.I | re.S,
    ):
        raw = (m.group(1) or "").strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        _walk_jsonld(payload, cats, seen)
    return cats


def extract_all_categories(page_html: str) -> list[str]:
    """HTML breadcrumb + JSON-LD BreadcrumbList / category fields (bol.com structure)."""
    cats: list[str] = []
    seen: set[str] = set()
    for c in _extract_categories(page_html) + _extract_jsonld_categories(page_html):
        if c and c not in seen:
            seen.add(c)
            cats.append(c)
    return cats[:20]


def _extract_categories(page_html: str) -> list[str]:
    cats: list[str] = []
    block = ""
    m = re.search(
        r"(Je vindt dit artikel in|Categorie(?:ën|en))[\s\S]{0,8000}",
        page_html,
        re.I,
    )
    if m:
        block = m.group(0)
    cat_field = _extract_spec_field(page_html, "Categorieën") or _extract_spec_field(page_html, "Categorien")
    if cat_field:
        for part in re.split(r"[•|,/]", cat_field):
            p = part.strip()
            if p and p not in cats:
                cats.append(p)
    if block:
        for part in re.split(r"[•|]", block):
            p = re.sub(r"<[^>]+>", "", part).strip()
            if len(p) > 2 and len(p) < 80 and p not in cats:
                if not re.match(r"^(Je vindt|Categorie)", p, re.I):
                    cats.append(p)
    return cats[:20]


def fetch_page_category_hints(
    url: str,
    timeout: float = 12.0,
    pool: ProxyPool | None = None,
) -> tuple[str, list[str]]:
    """Light fetch for sitemap pre-filter: title + bol category breadcrumbs."""
    code, html_text = _fetch_html(url, timeout=timeout, pool=pool)
    if html_text.startswith("<!-- error") or code != 200:
        return "", []
    return _extract_title(html_text), extract_all_categories(html_text)


def _classify_stock(page_html: str) -> tuple[bool, bool, bool]:
    """Returns (offline, online_oos, in_stock)."""
    hl = page_html.lower()
    title = _extract_title(page_html).lower()

    offline_markers = (
        "pagina niet gevonden",
        "could not find",
        "sorry, we kunnen deze pagina niet vinden",
        "ip-geblokkeerd",
        "access denied",
    )
    if any(x in title for x in ("niet gevonden", "not found", "404")):
        return True, False, False
    if any(x in hl[:80000] for x in offline_markers):
        if "in winkelwagen" not in hl and "op voorraad" not in hl[:120000]:
            return True, False, False

    json_in_stock = bool(re.search(r'"availability"\s*:\s*"InStock"', page_html, re.I))
    json_out_of_stock = bool(re.search(r'"availability"\s*:\s*"OutOfStock"', page_html, re.I))
    has_atc = "in winkelwagen" in hl or "toevoegen aan winkelwagen" in hl

    if json_out_of_stock and not has_atc and not json_in_stock:
        return False, True, False
    if has_atc or json_in_stock:
        return False, False, True

    no_stock = any(
        x in hl
        for x in (
            "niet op voorraad",
            "niet leverbaar",
            "tijdelijk niet leverbaar",
            "uitverkocht",
            "niet verkrijgbaar",
            '"availability":"outofstock"',
            "no_stock",
        )
    )
    if no_stock:
        return False, True, False
    if "op voorraad" in hl:
        return False, False, True
    return False, True, False


def _extract_price(page_html: str) -> str | None:
    m = re.search(
        r'"sellingPrice"\s*:\s*\{[^}]*"amount"\s*:\s*"?([0-9]+[.,][0-9]{2})"?',
        page_html,
    )
    if m:
        return m.group(1).replace(".", ",")
    pe = re.search(r'(?:content|aria-label)="[^"]*€\s*([0-9]+[.,][0-9]{2})', page_html[:400000], re.I)
    if pe:
        return pe.group(1)
    return None


def parse_product_page(url: str, page_html: str, http_status: int = 200) -> ProductPageData:
    out = ProductPageData(url=url, http_status=http_status)
    if http_status != 200 or not page_html or page_html.startswith("<!-- error"):
        out.offline = True
        out.error = "fetch_failed" if http_status != 200 else "empty"
        return out

    out.title = _extract_title(page_html)
    out.price_text = _extract_price(page_html)
    out.price_value = parse_price_value(out.price_text)
    out.categories = extract_all_categories(page_html)
    out.raw_categories_text = " • ".join(out.categories)
    out.brand = _extract_spec_field(page_html, "Merk")
    out.product_type = _extract_spec_field(page_html, "Type product") or _extract_spec_field(
        page_html, "Type"
    )
    out.release_year = _extract_spec_field(page_html, "Jaar van uitgave")

    offline, oos, instock = _classify_stock(page_html)
    out.offline = offline
    out.online_oos = oos and not instock
    out.in_stock = instock
    return out


def fetch_product_page(
    url: str,
    timeout: float = 15.0,
    opener=None,
    pool: ProxyPool | None = None,
) -> ProductPageData:
    code, html_text = _fetch_html(url, timeout=timeout, opener=opener, pool=pool)
    if html_text.startswith("<!-- error"):
        return ProductPageData(url=url, http_status=code, offline=True, error=html_text)
    return parse_product_page(url, html_text, http_status=code)


def product_status_string(data: ProductPageData) -> str:
    if data.offline:
        return "offline"
    if data.in_stock:
        return "in_stock"
    if data.online_oos:
        return "online_oos"
    return "unknown"
