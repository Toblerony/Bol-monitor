"""Match bol.com products against user-defined profiles."""
from __future__ import annotations

import json
import re

from app.models import ProductProfile
from app.services.bol_pdp_parser import ProductPageData, normalize_text

SITEMAP_MIN_TOKEN_LEN = 3


def _load_keywords(raw: str) -> list[str]:
    if not raw:
        return []
    raw = raw.strip()
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except json.JSONDecodeError:
            pass
    return [p.strip() for p in re.split(r"[,;\n]", raw) if p.strip()]


def profile_keywords(profile: ProductProfile) -> dict[str, list[str]]:
    return {
        "title": _load_keywords(profile.title_keywords),
        "category": _load_keywords(profile.category_keywords),
        "exclude": _load_keywords(profile.exclude_keywords),
    }


def _compact_token(raw: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", normalize_text(raw))


def keyword_to_sitemap_tokens(keyword: str, *, min_len: int = SITEMAP_MIN_TOKEN_LEN) -> list[str]:
    """
    Turn a profile keyword into slug/title tokens.
    "Pokémon kaarten" → pokemonkaarten, pokemon, kaarten
    """
    out: list[str] = []
    seen: set[str] = set()

    def add(part: str) -> None:
        t = _compact_token(part)
        if len(t) >= min_len and t not in seen:
            seen.add(t)
            out.append(t)

    phrase = (keyword or "").strip()
    if phrase:
        add(phrase)
        for part in re.split(r"[\s,;/\-+&]+", phrase):
            if part.strip():
                add(part.strip())
    return out


def keywords_to_sitemap_tokens(keywords: list[str]) -> frozenset[str]:
    tokens: set[str] = set()
    for word in keywords:
        tokens.update(keyword_to_sitemap_tokens(word))
    return frozenset(tokens)


def sitemap_tokens_match_text(text: str, tokens: frozenset[str]) -> bool:
    """True if any sitemap token appears in normalized text (title, slug, categories)."""
    if not text or not tokens:
        return False
    compact = _compact_token(text)
    for tok in tokens:
        if len(tok) < SITEMAP_MIN_TOKEN_LEN:
            continue
        if tok in compact:
            return True
    return False


def _category_haystack(data: ProductPageData) -> str:
    """Bol breadcrumb / category fields only (not product title)."""
    parts = [
        data.raw_categories_text,
        data.product_type,
        " ".join(data.categories),
    ]
    return normalize_text(" ".join(p for p in parts if p))


def _title_haystack(data: ProductPageData) -> str:
    return normalize_text(data.title)


def _full_haystack(data: ProductPageData) -> str:
    parts = [
        data.title,
        data.raw_categories_text,
        data.brand,
        data.product_type,
        data.release_year,
        " ".join(data.categories),
    ]
    return normalize_text(" ".join(p for p in parts if p))


def keyword_match(text: str, keyword: str) -> bool:
    k = normalize_text(keyword)
    if not k or len(k) < 2:
        return False
    compact = _compact_token(keyword)
    if compact and len(compact) >= 2 and compact in _compact_token(text):
        return True
    return k in text


def matches_profile(profile: ProductProfile, data: ProductPageData) -> bool:
    if not profile.is_enabled:
        return False
    kw = profile_keywords(profile)
    if not kw["title"] and not kw["category"]:
        return False

    full_hay = _full_haystack(data)
    title_hay = _title_haystack(data)
    cat_hay = _category_haystack(data)

    for ex in kw["exclude"]:
        if keyword_match(full_hay, ex) or keyword_match(title_hay, ex):
            return False

    title_ok = True
    if kw["title"]:
        title_ok = any(keyword_match(title_hay, t) for t in kw["title"])

    cat_ok = True
    if kw["category"]:
        # Category keywords match bol breadcrumb / "Je vindt dit artikel in" path
        cat_ok = any(keyword_match(cat_hay, c) or keyword_match(full_hay, c) for c in kw["category"])

    if kw["title"] and kw["category"]:
        if not (title_ok and cat_ok):
            return False
    elif kw["title"] and not title_ok:
        return False
    elif kw["category"] and not cat_ok:
        return False

    price = data.price_value
    if price is not None:
        if profile.price_min is not None and price < profile.price_min:
            return False
        if profile.price_max is not None and price > profile.price_max:
            return False

    return True


def profile_match_keywords_for_sitemap(profiles: list[ProductProfile]) -> frozenset[str]:
    """Build sitemap slug/title filter tokens from all enabled profile keywords."""
    tokens: set[str] = set()
    for p in profiles:
        if not p.is_enabled:
            continue
        kw = profile_keywords(p)
        tokens.update(keywords_to_sitemap_tokens(kw["title"] + kw["category"]))
    return frozenset(tokens)
