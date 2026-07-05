#!/usr/bin/env python3
"""
Fast bol.com sitemap monitor.

It stores every known URL in SQLite for quick membership checks, mirrors all
known URLs to all_links.txt, and appends newly discovered URLs to newlinks.txt.

When imported, use ``get_new_product_links()`` for a single silent scan that
returns newly discovered product listing URLs (bol.com ``/p/`` pages).
"""

from __future__ import annotations

import argparse
import csv
import html
import re
import concurrent.futures
import contextlib
import gzip
import http.client
import io
import itertools
import os
import signal
import sqlite3
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urlparse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


DEFAULT_INDEX_URL = "https://www.bol.com/sitemap/nl-nl/"
DEFAULT_DB = "bol_sitemap.sqlite3"
DEFAULT_ALL_LINKS = "all_links.txt"
DEFAULT_NEW_LINKS = "newlinks.txt"
DEFAULT_PROXY_FILE = "proxy.txt"
DEFAULT_PRODUCT_CSV = "product.csv"
USER_AGENT = "Mozilla/5.0 (compatible; SitemapMonitor/1.0; +https://www.bol.com/)"

SLUG_STOPWORDS = frozenset(
    {
        "https",
        "http",
        "www",
        "bol",
        "com",
        "nl",
        "p",
        "prod",
        "product",
        "shop",
        "the",
        "and",
        "for",
        "with",
        "from",
        "new",
        "set",
        "van",
        "de",
        "het",
        "een",
        "voor",
        "met",
        "bij",
        "naar",
        "over",
        "zwart",
        "wit",
        "grijs",
        "large",
        "small",
        "medium",
        "mini",
        "max",
        "plus",
        "editie",
        "edition",
        "stuks",
        "stuk",
        "st",
        "ct",
        "pack",
        "box",
        "case",
        "display",
        "portfolio",
        "binder",
        "sleeve",
        "sleeves",
        "kaarten",
        "kaart",
        "card",
        "cards",
        "booster",
        "boosters",
        "bundle",
        "accessoire",
        "accessoires",
    }
)


@dataclass(frozen=True)
class SitemapEntry:
    loc: str
    lastmod: str


@dataclass(frozen=True)
class FetchResult:
    url: str
    lastmod: str
    urls_path: Path
    parsed_count: int
    etag: str
    content_length: str


@dataclass(frozen=True)
class HeaderResult:
    url: str
    lastmod: str
    etag: str
    content_length: str


class StopRequested(Exception):
    pass


class ProxyPool:
    def __init__(self, proxies: list[str]) -> None:
        self.proxies = proxies
        self._cycle = itertools.cycle(proxies)
        self._lock = threading.Lock()
        self._openers = {
            proxy: urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            )
            for proxy in proxies
        }

    def __len__(self) -> int:
        return len(self.proxies)

    def next(self) -> tuple[str, urllib.request.OpenerDirector]:
        with self._lock:
            proxy = next(self._cycle)
        return proxy, self._openers[proxy]


class Progress:
    def __init__(
        self,
        label: str,
        total: int,
        every: int = 25,
        seconds: float = 5.0,
        silent: bool = False,
    ):
        self.label = label
        self.total = total
        self.every = max(1, every)
        self.seconds = seconds
        self.done = 0
        self._last_print = 0.0
        self.silent = silent
        if not silent:
            print(f"[{utc_now()}] {self.label}: start total={self.total}", flush=True)

    def step(self, extra: str = "", force: bool = False) -> None:
        self.done += 1
        if self.silent:
            return
        now = time.monotonic()
        should_print = (
            force
            or self.done == self.total
            or self.done % self.every == 0
            or now - self._last_print >= self.seconds
        )
        if not should_print:
            return

        self._last_print = now
        suffix = f" | {extra}" if extra else ""
        print(
            f"[{utc_now()}] {self.label}: {self.done}/{self.total}{suffix}",
            flush=True,
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def response_size(headers) -> str:
    content_range = headers.get("Content-Range", "").strip()
    if "/" in content_range:
        total = content_range.rsplit("/", 1)[-1].strip()
        if total and total != "*":
            return total
    return headers.get("Content-Length", "").strip()


def normalize_proxy(line: str) -> str:
    proxy = line.strip()
    if "://" in proxy:
        return proxy
    if "@" in proxy:
        return "http://" + proxy

    parts = proxy.split(":")
    if len(parts) == 4:
        host, port, username, password = parts
        return f"http://{username}:{password}@{host}:{port}"

    return "http://" + proxy


def load_proxy_pool(proxy_file: Path | None) -> ProxyPool | None:
    if proxy_file is None or str(proxy_file) == "" or not proxy_file.exists():
        return None

    proxies = [
        normalize_proxy(line)
        for line in proxy_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not proxies:
        return None

    return ProxyPool(proxies)


def fetch_response(
    url: str,
    timeout: float,
    retries: int,
    method: str = "GET",
    extra_headers: dict[str, str] | None = None,
    proxy_pool: ProxyPool | None = None,
) -> tuple[bytes, str, str]:
    last_error: Exception | None = None
    attempts = retries + 1
    if proxy_pool is not None:
        attempts = max(attempts, len(proxy_pool))

    for attempt in range(attempts):
        proxy_label = ""
        try:
            headers = {
                "Accept": "application/xml,text/xml,*/*",
                "Accept-Encoding": "gzip",
                "User-Agent": USER_AGENT,
            }
            if extra_headers:
                headers.update(extra_headers)
            request = urllib.request.Request(url, headers=headers, method=method)

            if proxy_pool is None:
                response_context = urllib.request.urlopen(request, timeout=timeout)
            else:
                proxy_label, opener = proxy_pool.next()
                response_context = opener.open(request, timeout=timeout)

            with response_context as response:
                body = b"" if method == "HEAD" else response.read()
                encoding = response.headers.get("Content-Encoding", "").lower()
                if encoding == "gzip" or url.endswith(".gz"):
                    body = gzip.decompress(body)
                etag = response.headers.get("ETag", "").strip()
                content_length = response_size(response.headers)
                return body, etag, content_length
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 429 and attempt < attempts - 1:
                if proxy_label:
                    print(
                        f"[{utc_now()}] 429 for {url}; rotating proxy",
                        file=sys.stderr,
                        flush=True,
                    )
                continue
            if attempt < attempts - 1:
                time.sleep(min(2.0 * (attempt + 1), 10.0))
        except (http.client.IncompleteRead, urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < attempts - 1:
                time.sleep(min(2.0 * (attempt + 1), 10.0))
    raise RuntimeError(f"failed to fetch {url} with {method}: {last_error}")


def fetch_headers(
    url: str,
    timeout: float,
    retries: int,
    extra_headers: dict[str, str] | None = None,
    proxy_pool: ProxyPool | None = None,
) -> tuple[str, str]:
    last_error: Exception | None = None
    attempts = retries + 1
    if proxy_pool is not None:
        attempts = max(attempts, len(proxy_pool))

    for attempt in range(attempts):
        proxy_label = ""
        try:
            headers = {
                "Accept": "application/xml,text/xml,*/*",
                "Accept-Encoding": "identity",
                "User-Agent": USER_AGENT,
            }
            if extra_headers:
                headers.update(extra_headers)
            request = urllib.request.Request(url, headers=headers, method="GET")

            if proxy_pool is None:
                response_context = urllib.request.urlopen(request, timeout=timeout)
            else:
                proxy_label, opener = proxy_pool.next()
                response_context = opener.open(request, timeout=timeout)

            with response_context as response:
                etag = response.headers.get("ETag", "").strip()
                content_length = response_size(response.headers)
                return etag, content_length
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 429 and attempt < attempts - 1:
                if proxy_label:
                    print(
                        f"[{utc_now()}] 429 for {url}; rotating proxy",
                        file=sys.stderr,
                        flush=True,
                    )
                continue
            if attempt < attempts - 1:
                time.sleep(min(2.0 * (attempt + 1), 10.0))
        except (http.client.IncompleteRead, urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < attempts - 1:
                time.sleep(min(2.0 * (attempt + 1), 10.0))
    raise RuntimeError(f"failed to read headers for {url}: {last_error}")


def fetch_bytes(
    url: str, timeout: float, retries: int, proxy_pool: ProxyPool | None
) -> bytes:
    body, _etag, _content_length = fetch_response(
        url, timeout, retries, proxy_pool=proxy_pool
    )
    return body


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_sitemap_index(xml_bytes: bytes) -> list[SitemapEntry]:
    entries: list[SitemapEntry] = []
    current_loc = ""
    current_lastmod = ""

    for event, elem in ET.iterparse(io.BytesIO(xml_bytes), events=("end",)):
        name = local_name(elem.tag)
        if name == "loc":
            current_loc = (elem.text or "").strip()
        elif name == "lastmod":
            current_lastmod = (elem.text or "").strip()
        elif name == "sitemap":
            if current_loc:
                entries.append(SitemapEntry(current_loc, current_lastmod))
            current_loc = ""
            current_lastmod = ""
            elem.clear()

    return entries


def iter_url_locs(xml_bytes: bytes) -> Iterable[str]:
    for event, elem in ET.iterparse(io.BytesIO(xml_bytes), events=("end",)):
        if local_name(elem.tag) == "loc" and elem.text:
            yield elem.text.strip()
        elem.clear()


def iter_lines(path: Path) -> Iterable[str]:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            yield line.rstrip("\n")


def connect_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-200000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS links (
            url TEXT PRIMARY KEY,
            first_seen TEXT NOT NULL
        ) WITHOUT ROWID
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS child_sitemaps (
            url TEXT PRIMARY KEY,
            lastmod TEXT,
            etag TEXT,
            content_length TEXT,
            checked_at TEXT NOT NULL
        ) WITHOUT ROWID
        """
    )
    migrate_child_sitemaps(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checked_at TEXT NOT NULL,
            scanned_sitemaps INTEGER NOT NULL,
            new_links INTEGER NOT NULL,
            total_links INTEGER NOT NULL
        )
        """
    )
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS batch_urls (url TEXT PRIMARY KEY)")
    return conn


def migrate_child_sitemaps(conn: sqlite3.Connection) -> None:
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(child_sitemaps)").fetchall()
    }
    if "etag" not in columns:
        conn.execute("ALTER TABLE child_sitemaps ADD COLUMN etag TEXT")
    if "content_length" not in columns:
        conn.execute("ALTER TABLE child_sitemaps ADD COLUMN content_length TEXT")


def count_links(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM links").fetchone()[0])


def known_sitemap_sizes(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute(
        "SELECT url, COALESCE(content_length, '') FROM child_sitemaps"
    )
    return {url: content_length for url, content_length in rows}


def insert_links(
    conn: sqlite3.Connection,
    urls: Iterable[str],
    all_links_path: Path,
    new_links_path: Path,
) -> tuple[int, int, list[str]]:
    first_seen = utc_now()
    batch = [(url,) for url in urls if url]
    if not batch:
        return 0, 0, []

    conn.execute("DELETE FROM batch_urls")
    conn.executemany("INSERT OR IGNORE INTO batch_urls(url) VALUES (?)", batch)

    new_urls = [
        url
        for (url,) in conn.execute(
            """
            SELECT b.url
            FROM batch_urls b
            LEFT JOIN links l ON l.url = b.url
            WHERE l.url IS NULL
            """
        )
    ]

    with all_links_path.open("a", encoding="utf-8") as all_file, new_links_path.open(
        "a", encoding="utf-8"
    ) as new_file:
        for url in new_urls:
            all_file.write(url + "\n")
            new_file.write(url + "\n")

    conn.executemany(
        "INSERT INTO links(url, first_seen) VALUES (?, ?)",
        ((url, first_seen) for url in new_urls),
    )

    return len(new_urls), len(batch), new_urls


def mark_sitemap_checked(
    conn: sqlite3.Connection,
    sitemap_url: str,
    lastmod: str,
    etag: str,
    content_length: str,
    checked_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO child_sitemaps(url, lastmod, etag, content_length, checked_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
            lastmod = excluded.lastmod,
            etag = excluded.etag,
            content_length = excluded.content_length,
            checked_at = excluded.checked_at
        """,
        (sitemap_url, lastmod, etag, content_length, checked_at),
    )


def fetch_child(
    entry: SitemapEntry,
    timeout: float,
    retries: int,
    proxy_pool: ProxyPool | None,
    temp_dir: Path,
) -> FetchResult:
    last_error: Exception | None = None
    attempts = retries + 1
    if proxy_pool is not None:
        attempts = max(attempts, len(proxy_pool))

    for attempt in range(attempts):
        proxy_label = ""
        temp_path: Path | None = None
        try:
            request = urllib.request.Request(
                entry.loc,
                headers={
                    "Accept": "application/xml,text/xml,*/*",
                    "Accept-Encoding": "identity",
                    "User-Agent": USER_AGENT,
                },
                method="GET",
            )

            if proxy_pool is None:
                response_context = urllib.request.urlopen(request, timeout=timeout)
            else:
                proxy_label, opener = proxy_pool.next()
                response_context = opener.open(request, timeout=timeout)

            with response_context as response:
                etag = response.headers.get("ETag", "").strip()
                content_length = response_size(response.headers)
                source = response
                encoding = response.headers.get("Content-Encoding", "").lower()
                if encoding == "gzip" or entry.loc.endswith(".gz"):
                    source = gzip.GzipFile(fileobj=response)

                temp_file = tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    delete=False,
                    dir=temp_dir,
                    prefix="sitemap_urls_",
                    suffix=".txt",
                )
                temp_path = Path(temp_file.name)
                parsed_count = 0
                with temp_file:
                    for _event, elem in ET.iterparse(source, events=("end",)):
                        if local_name(elem.tag) == "loc" and elem.text:
                            temp_file.write(elem.text.strip() + "\n")
                            parsed_count += 1
                        elem.clear()

            return FetchResult(
                entry.loc,
                entry.lastmod,
                temp_path,
                parsed_count,
                etag,
                content_length,
            )
        except urllib.error.HTTPError as exc:
            last_error = exc
            if temp_path is not None:
                with contextlib.suppress(OSError):
                    temp_path.unlink()
            if exc.code == 429 and attempt < attempts - 1:
                if proxy_label:
                    print(
                        f"[{utc_now()}] 429 for {entry.loc}; rotating proxy",
                        file=sys.stderr,
                        flush=True,
                    )
                continue
            if attempt < attempts - 1:
                time.sleep(min(2.0 * (attempt + 1), 10.0))
        except (
            ET.ParseError,
            http.client.IncompleteRead,
            urllib.error.URLError,
            TimeoutError,
            OSError,
        ) as exc:
            last_error = exc
            if temp_path is not None:
                with contextlib.suppress(OSError):
                    temp_path.unlink()
            if attempt < attempts - 1:
                time.sleep(min(2.0 * (attempt + 1), 10.0))

    raise RuntimeError(f"failed to stream {entry.loc}: {last_error}")


def fetch_child_headers(
    entry: SitemapEntry,
    timeout: float,
    retries: int,
    proxy_pool: ProxyPool | None,
) -> HeaderResult:
    etag, content_length = fetch_headers(entry.loc, timeout, retries, proxy_pool=proxy_pool)
    return HeaderResult(entry.loc, entry.lastmod, etag, content_length)


def select_entries_by_size(
    conn: sqlite3.Connection,
    index_entries: list[SitemapEntry],
    workers: int,
    timeout: float,
    retries: int,
    proxy_pool: ProxyPool | None,
    silent: bool = False,
) -> tuple[list[SitemapEntry], int, int, int]:
    known_sizes = known_sitemap_sizes(conn)
    entries_to_scan: list[SitemapEntry] = []
    skipped = 0
    missing_size = 0
    changed_size = 0

    if not silent:
        print(
            f"[{utc_now()}] size check: checking {len(index_entries)} child sitemap "
            f"headers with {workers} workers",
            flush=True,
        )
    progress = Progress(
        "size check", len(index_entries), every=25, seconds=5.0, silent=silent
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(fetch_child_headers, entry, timeout, retries, proxy_pool): entry
            for entry in index_entries
        }
        for future in concurrent.futures.as_completed(futures):
            entry = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                if not silent:
                    print(
                        f"[{utc_now()}] size check failed, will download {entry.loc}: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                entries_to_scan.append(entry)
                missing_size += 1
                progress.step(
                    f"download={len(entries_to_scan)} skip={skipped} "
                    "last=header error"
                )
                continue

            old_size = known_sizes.get(result.url, "")
            if not result.content_length:
                entries_to_scan.append(entry)
                missing_size += 1
                reason = "missing size"
            elif not old_size:
                entries_to_scan.append(entry)
                changed_size += 1
                reason = "new sitemap"
            elif result.content_length != old_size:
                entries_to_scan.append(entry)
                changed_size += 1
                reason = f"size {old_size}->{result.content_length}"
            else:
                skipped += 1
                reason = "same size"

            progress.step(
                f"download={len(entries_to_scan)} skip={skipped} "
                f"missing_size={missing_size} changed_or_new={changed_size} "
                f"last={reason}"
            )

    if not silent:
        print(
            f"[{utc_now()}] size check done: download={len(entries_to_scan)} "
            f"skip={skipped} missing_size={missing_size} changed_or_new={changed_size}",
            flush=True,
        )
    return entries_to_scan, skipped, missing_size, changed_size


def scan_once(
    conn: sqlite3.Connection,
    index_url: str,
    all_links_path: Path,
    new_links_path: Path,
    workers: int,
    timeout: float,
    retries: int,
    proxy_pool: ProxyPool | None,
    silent: bool = False,
) -> tuple[int, int, int, list[str]]:
    started = time.perf_counter()
    checked_at = utc_now()

    if not silent:
        print(
            f"[{utc_now()}] cycle start: full-content check workers={workers} "
            f"timeout={timeout:.1f}s",
            flush=True,
        )
    print(f"[{utc_now()}] fetching sitemap index: {index_url}", flush=True)
    index_body = fetch_bytes(index_url, timeout, retries, proxy_pool)
    if not silent:
        print(
            f"[{utc_now()}] sitemap index downloaded: {len(index_body):,} bytes",
            flush=True,
        )
    index_entries = parse_sitemap_index(index_body)
    if not silent:
        print(
            f"[{utc_now()}] sitemap index parsed: {len(index_entries)} child sitemaps",
            flush=True,
        )
    entries_to_scan, skipped_by_size, missing_size, changed_size = select_entries_by_size(
        conn,
        index_entries,
        workers,
        timeout,
        retries,
        proxy_pool,
        silent=silent,
    )
    if not silent:
        print(
            f"[{utc_now()}] queued {len(entries_to_scan)} child sitemaps for download; "
            f"skipped {skipped_by_size} with unchanged size",
            flush=True,
        )

    new_links = 0
    parsed_links = 0
    scanned = 0
    cycle_new_urls: list[str] = []

    if entries_to_scan:
        if not silent:
            print(
                f"[{utc_now()}] child scan queue: total={len(entries_to_scan)} "
                f"download={len(entries_to_scan)}",
                flush=True,
            )
        download_progress = Progress(
            "child sitemap scan",
            len(entries_to_scan),
            every=1,
            seconds=0.0,
            silent=silent,
        )

        with tempfile.TemporaryDirectory(prefix="bol_sitemap_urls_") as temp_name:
            temp_dir = Path(temp_name)
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(
                        fetch_child,
                        entry,
                        timeout,
                        retries,
                        proxy_pool,
                        temp_dir,
                    ): entry
                    for entry in entries_to_scan
                }

                for future in concurrent.futures.as_completed(futures):
                    entry = futures[future]
                    result: FetchResult | None = None
                    try:
                        result = future.result()
                        with conn:
                            added, parsed, batch_new = insert_links(
                                conn,
                                iter_lines(result.urls_path),
                                all_links_path,
                                new_links_path,
                            )
                            new_links += added
                            parsed_links += parsed
                            cycle_new_urls.extend(batch_new)
                            mark_sitemap_checked(
                                conn,
                                result.url,
                                result.lastmod,
                                result.etag,
                                result.content_length,
                                checked_at,
                            )
                        scanned += 1
                        download_progress.step(
                            f"parsed={parsed:,} new={added:,} run_new={new_links:,} "
                            f"run_parsed={parsed_links:,} url={entry.loc}",
                            force=True,
                        )
                    except Exception as exc:
                        if not silent:
                            print(
                                f"[{utc_now()}] ERROR {entry.loc}: {exc}",
                                file=sys.stderr,
                            )
                    finally:
                        if result is not None:
                            with contextlib.suppress(OSError):
                                result.urls_path.unlink()
    else:
        if not silent:
            print(f"[{utc_now()}] no child sitemaps need scanning this cycle", flush=True)

    total = count_links(conn)
    with conn:
        conn.execute(
            """
            INSERT INTO runs(checked_at, scanned_sitemaps, new_links, total_links)
            VALUES (?, ?, ?, ?)
            """,
            (checked_at, scanned, new_links, total),
        )

    elapsed = time.perf_counter() - started
    if not silent:
        print(
            f"[{utc_now()}] done: index={len(index_entries)} "
            f"skipped_by_size={skipped_by_size} scanned={scanned} "
            f"parsed={parsed_links} new={new_links} "
            f"total={total} elapsed={elapsed:.2f}s",
            flush=True,
        )
    return scanned, new_links, total, cycle_new_urls


# ─────────────────────────────────────────────
#  Category filter (baseline = product.csv)
# ─────────────────────────────────────────────


def _normalize_keyword_token(raw: str) -> str:
    s = unicodedata.normalize("NFKD", (raw or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", s)


SITEMAP_EXTRA_KEYWORDS_FILE = "sitemap_extra_keywords.txt"


def read_extra_sitemap_keyword_tokens(keywords_path: str | Path) -> frozenset[str]:
    """
    Optional GUI/manual tokens (one per line) merged with auto keywords from product.csv.
    File lives next to product.csv (same folder).
    """
    p = Path(keywords_path)
    if not p.is_file():
        return frozenset()
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return frozenset()
    out: set[str] = set()
    for line in raw.splitlines():
        t = _normalize_keyword_token(line.strip())
        if t:
            out.add(t)
    return frozenset(out)


def _product_csv_url_field(fieldnames: list[str] | None) -> str:
    normalized = {
        (name or "").strip().lstrip("\ufeff").lower(): name
        for name in (fieldnames or [])
        if name
    }
    url_field = normalized.get("product_url")
    if not url_field:
        raise ValueError("product.csv must have a 'product_url' header.")
    return url_field


def read_product_urls_from_csv(path: str | Path) -> list[str]:
    p = Path(path)
    if not p.exists():
        return []
    out: list[str] = []
    with p.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        url_field = _product_csv_url_field(reader.fieldnames)
        for row in reader:
            u = (row.get(url_field) or "").strip()
            if u.startswith("http"):
                out.append(u)
    return out


def _extract_slug_from_product_url(url: str) -> str | None:
    m = re.search(r"/p/([^/]+)/", (url or "").strip(), re.IGNORECASE)
    return m.group(1).strip() if m else None


def _slug_tokens(slug: str) -> list[str]:
    if not slug or slug == "-":
        return []
    return [p for p in slug.split("-") if p]


def build_category_keywords_from_csv_urls(product_urls: list[str]) -> frozenset[str]:
    """
    Build a strict keyword set from existing product URLs (slug tokens).
    - Tokens shared by 2+ products are always kept (strong category signal).
    - If none, use the first qualifying token of the first URL with a real slug
      (single-product baseline: same niche as that listing).
    """
    row_tokens: list[list[str]] = []
    for u in product_urls:
        slug = _extract_slug_from_product_url(u)
        if not slug or slug == "-":
            continue
        toks = []
        for part in _slug_tokens(slug):
            t = _normalize_keyword_token(part)
            if not t or t.isdigit():
                continue
            if len(t) < 4:
                continue
            if t in SLUG_STOPWORDS:
                continue
            toks.append(t)
        if toks:
            row_tokens.append(toks)

    if not row_tokens:
        return frozenset()

    n_rows = len(row_tokens)
    freq: dict[str, int] = {}
    for toks in row_tokens:
        for t in set(toks):
            freq[t] = freq.get(t, 0) + 1

    multi = frozenset(t for t, c in freq.items() if c >= 2 and len(t) >= 4)
    if multi:
        return multi

    if n_rows == 1:
        for part in _slug_tokens(_extract_slug_from_product_url(product_urls[0]) or ""):
            t = _normalize_keyword_token(part)
            if len(t) >= 4 and not t.isdigit() and t not in SLUG_STOPWORDS:
                return frozenset({t})
        return frozenset()

    return frozenset()


def _url_path_lower(url: str) -> str:
    try:
        return (urlparse(url).path or "").lower()
    except Exception:
        return ""


def _path_contains_keyword_token(path_lower: str, keyword: str) -> bool:
    if not path_lower or not keyword:
        return False
    return (
        re.search(
            r"(?:^|[/-])" + re.escape(keyword) + r"(?:$|[/-]|\?|#|\d)",
            path_lower,
        )
        is not None
    )


def _title_contains_keyword(title: str, keywords: frozenset[str]) -> bool:
    tnorm = _normalize_keyword_token(re.sub(r"\s+", " ", html.unescape(title or "")))
    if not tnorm:
        return False
    for k in keywords:
        if len(k) < 4:
            continue
        if k in tnorm:
            return True
    return False


def _fetch_product_page_title(
    url: str,
    timeout: float,
    proxy_pool: ProxyPool | None,
) -> str | None:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "*/*;q=0.8"
        ),
        "Accept-Language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        if proxy_pool is None:
            ctx = urllib.request.urlopen(req, timeout=timeout)
        else:
            _label, opener = proxy_pool.next()
            ctx = opener.open(req, timeout=timeout)
        with ctx as resp:
            raw = resp.read(800_000)
        text = raw.decode("utf-8", errors="ignore")
        m = re.search(
            r"<title[^>]*>\s*([^<]{1,500}?)\s*</title>",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if not m:
            return None
        return html.unescape(re.sub(r"\s+", " ", m.group(1)).strip())
    except Exception:
        return None


def filter_new_urls_by_category(
    candidate_urls: list[str],
    product_csv_path: str | Path,
    proxy_pool: ProxyPool | None,
    title_fetch_timeout: float = 12.0,
    keywords: frozenset[str] | None = None,
) -> list[str]:
    """
    Keep only URLs that match at least one baseline keyword from product.csv.
    Logs each decision. Filtering happens before CSV append (caller responsibility).

    If ``keywords`` is passed (from a pre-scan baseline check), CSV is not read again.
    """
    if not candidate_urls:
        return []

    p = Path(product_csv_path)
    if keywords is None:
        try:
            baseline = read_product_urls_from_csv(p)
        except (OSError, ValueError, csv.Error) as exc:
            print(
                f"[FILTER] Cannot read product.csv ({exc}) — rejecting all new sitemap links",
                flush=True,
            )
            return []

        keywords = build_category_keywords_from_csv_urls(baseline)

        if not baseline:
            print(
                "[FILTER] No rows in product.csv — rejecting all new sitemap product links",
                flush=True,
            )
            return []

        if not keywords:
            print(
                "[FILTER] No baseline keywords extracted from product.csv slugs — "
                "rejecting all new sitemap product links",
                flush=True,
            )
            return []

    if not keywords:
        return []

    kw_preview = ", ".join(sorted(keywords)[:24])
    more = "" if len(keywords) <= 24 else f" … (+{len(keywords) - 24} more)"
    print(
        f"[FILTER] Baseline keywords ({len(keywords)}): {kw_preview}{more}",
        flush=True,
    )

    allowed: list[str] = []
    for url in candidate_urls:
        u = (url or "").strip()
        path_l = _url_path_lower(u)
        slug = _extract_slug_from_product_url(u)

        matched = any(_path_contains_keyword_token(path_l, k) for k in keywords)

        if not matched and slug == "-":
            title = _fetch_product_page_title(u, title_fetch_timeout, proxy_pool)
            if title and _title_contains_keyword(title, keywords):
                matched = True

        if matched:
            print(f"[FILTER] Allowed (matched category): {u}", flush=True)
            allowed.append(u)
        else:
            print(f"[FILTER] Skipped (irrelevant category): {u}", flush=True)

    return allowed


def is_bol_product_listing_url(url: str) -> bool:
    """True for bol.com product detail URLs (path contains /p/ and a long numeric id)."""
    u = (url or "").strip()
    if not u.lower().startswith("http"):
        return False
    if "bol.com" not in u.lower():
        return False
    if "/p/" not in u:
        return False
    if re.search(r"/p/[^?\s#]*/(\d{10,})", u):
        return True
    if re.search(r"/p/-/(\d{10,})", u):
        return True
    return bool(re.search(r"/p/[^?\s#]*(\d{10,})", u))


def get_new_product_links(
    index_url: str = DEFAULT_INDEX_URL,
    db_path: str | Path = DEFAULT_DB,
    all_links_path: str | Path = DEFAULT_ALL_LINKS,
    new_links_path: str | Path = DEFAULT_NEW_LINKS,
    proxy_file: str | Path | None = DEFAULT_PROXY_FILE,
    workers: int = 1,
    timeout: float = 30.0,
    proxy_timeout: float | None = None,
    retries: int = 2,
    silent: bool = True,
    product_csv_path: str | Path | None = None,
    category_filter: bool = True,
    title_fetch_timeout: float = 12.0,
    require_extra_sitemap_keywords: bool = False,
) -> list[str]:
    """
    Run one sitemap cycle and return **new** bol.com product listing URLs
    discovered since the SQLite DB was last updated.

    ``silent=True`` suppresses verbose monitor logs (recommended when embedded).

    When ``category_filter`` is True (default), only URLs matching baseline
    keywords from ``product_csv_path`` (default ``product.csv``) are returned.
    If there are no baseline keywords, the sitemap network scan is skipped entirely.

    When ``require_extra_sitemap_keywords`` is True (used by the bot), the scan is
    skipped unless ``SITEMAP_EXTRA_KEYWORDS_FILE`` next to the CSV contains at least
    one non-empty keyword line (GUI Keywords tab → Save). Slug-derived tokens alone
    are not enough in that mode.
    """
    csv_p = Path(product_csv_path) if product_csv_path is not None else Path(
        DEFAULT_PRODUCT_CSV
    )

    precomputed_keywords: frozenset[str] | None = None
    if category_filter:
        try:
            baseline_urls = read_product_urls_from_csv(csv_p)
        except (OSError, ValueError, csv.Error) as exc:
            print(
                f"[FILTER] Cannot read product.csv ({exc}) — skipping sitemap scan",
                flush=True,
            )
            return []
        if not baseline_urls:
            print(
                "[FILTER] No rows in product.csv — skipping sitemap scan",
                flush=True,
            )
            return []
        extra_path = csv_p.parent / SITEMAP_EXTRA_KEYWORDS_FILE
        extra_kw = read_extra_sitemap_keyword_tokens(extra_path)
        if require_extra_sitemap_keywords and not extra_kw:
            if not silent:
                print(
                    f"[FILTER] Sitemap discovery needs at least one line in {extra_path.name} "
                    "(Keywords tab in the app → Save). Skipping scan.",
                    flush=True,
                )
            return []

        precomputed_keywords = build_category_keywords_from_csv_urls(baseline_urls)
        if extra_kw:
            precomputed_keywords = frozenset(precomputed_keywords | extra_kw)
            print(
                f"[FILTER] Merged {len(extra_kw)} extra token(s) from {extra_path.name}",
                flush=True,
            )
        if not precomputed_keywords:
            print(
                "[FILTER] No keywords from product.csv slugs or "
                f"{SITEMAP_EXTRA_KEYWORDS_FILE} — skipping sitemap scan",
                flush=True,
            )
            return []

    db_p = Path(db_path)
    all_p = Path(all_links_path)
    new_p = Path(new_links_path)
    db_p.parent.mkdir(parents=True, exist_ok=True)
    all_p.parent.mkdir(parents=True, exist_ok=True)
    new_p.parent.mkdir(parents=True, exist_ok=True)

    pf = Path(proxy_file) if proxy_file else None
    proxy_pool = load_proxy_pool(pf) if pf and pf.exists() else None
    if proxy_timeout is None:
        proxy_timeout = 8.0
    request_timeout = float(proxy_timeout) if proxy_pool is not None else float(timeout)

    conn = connect_db(db_p)
    try:
        _scanned, _new_count, _total, raw_new = scan_once(
            conn,
            index_url,
            all_p,
            new_p,
            workers,
            request_timeout,
            retries,
            proxy_pool,
            silent=silent,
        )
    finally:
        with contextlib.suppress(Exception):
            conn.close()

    out: list[str] = []
    seen: set[str] = set()
    for u in raw_new:
        if not is_bol_product_listing_url(u):
            continue
        nu = u.strip()
        if nu in seen:
            continue
        seen.add(nu)
        out.append(nu)

    if category_filter and precomputed_keywords is not None:
        out = filter_new_urls_by_category(
            out,
            csv_p,
            proxy_pool,
            title_fetch_timeout=title_fetch_timeout,
            keywords=precomputed_keywords,
        )

    return out


def rebuild_text_export(conn: sqlite3.Connection, all_links_path: Path) -> None:
    tmp_path = all_links_path.with_suffix(all_links_path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        for (url,) in conn.execute("SELECT url FROM links ORDER BY url"):
            handle.write(url + "\n")
    os.replace(tmp_path, all_links_path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download and monitor bol.com sitemap links quickly."
    )
    parser.add_argument("--index-url", default=DEFAULT_INDEX_URL)
    parser.add_argument("--db", type=Path, default=Path(DEFAULT_DB))
    parser.add_argument("--all-links", type=Path, default=Path(DEFAULT_ALL_LINKS))
    parser.add_argument("--new-links", type=Path, default=Path(DEFAULT_NEW_LINKS))
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--proxy-timeout", type=float, default=8.0)
    parser.add_argument(
        "--proxy-file",
        type=Path,
        default=Path(DEFAULT_PROXY_FILE),
        help="Proxy list file. One proxy per line. Use empty string to disable.",
    )
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one check and exit instead of polling forever.",
    )
    parser.add_argument(
        "--rebuild-all-links",
        action="store_true",
        help="Rebuild all_links.txt from the SQLite database before scanning.",
    )
    args = parser.parse_args()

    if args.workers < 1:
        parser.error("--workers must be at least 1")
    if args.interval < 1:
        parser.error("--interval must be at least 1 second")
    if args.proxy_timeout < 1:
        parser.error("--proxy-timeout must be at least 1 second")

    args.db.parent.mkdir(parents=True, exist_ok=True)
    args.all_links.parent.mkdir(parents=True, exist_ok=True)
    args.new_links.parent.mkdir(parents=True, exist_ok=True)

    stop = False

    def request_stop(signum, frame):
        nonlocal stop
        stop = True
        raise StopRequested

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    conn = connect_db(args.db)
    try:
        proxy_pool = load_proxy_pool(args.proxy_file)
        request_timeout = args.proxy_timeout if proxy_pool is not None else args.timeout
        if proxy_pool is not None:
            print(
                f"[{utc_now()}] loaded {len(proxy_pool)} proxies from {args.proxy_file}; "
                f"request timeout={request_timeout:.1f}s",
                flush=True,
            )

        if args.rebuild_all_links:
            rebuild_text_export(conn, args.all_links)

        while True:
            cycle_start = time.perf_counter()
            try:
                scan_once(
                    conn,
                    args.index_url,
                    args.all_links,
                    args.new_links,
                    args.workers,
                    request_timeout,
                    args.retries,
                    proxy_pool,
                    silent=False,
                )
            except StopRequested:
                break
            except Exception as exc:
                print(f"[{utc_now()}] cycle failed: {exc}", file=sys.stderr, flush=True)

            if args.once or stop:
                break

            sleep_for = max(0.0, args.interval - (time.perf_counter() - cycle_start))
            print(
                f"[{utc_now()}] sleeping {sleep_for:.1f}s before next cycle",
                flush=True,
            )
            time.sleep(sleep_for)
    except StopRequested:
        pass
    finally:
        with contextlib.suppress(Exception):
            conn.close()

    print(f"[{utc_now()}] stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
