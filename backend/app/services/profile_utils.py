import json

from app.models import ProductProfile


def profile_to_response(p: ProductProfile, tracked_count: int = 0) -> dict:
    def loads(raw: str) -> list[str]:
        try:
            v = json.loads(raw or "[]")
            return v if isinstance(v, list) else []
        except json.JSONDecodeError:
            return []

    return {
        "id": p.id,
        "name": p.name,
        "title_keywords": loads(p.title_keywords),
        "category_keywords": loads(p.category_keywords),
        "exclude_keywords": loads(p.exclude_keywords),
        "price_min": p.price_min,
        "price_max": p.price_max,
        "is_enabled": p.is_enabled,
        "tracked_count": tracked_count,
    }


def dumps_keywords(items: list[str]) -> str:
    return json.dumps([x.strip() for x in items if x and x.strip()])
