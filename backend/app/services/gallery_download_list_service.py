import json
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AppConfig
from app.services.category_blacklist import load_blacklisted_categories, normalize_category_name

GALLERY_DOWNLOAD_LIST_KEY = "gallery_download_list_v1"
MediaType = Literal["images", "videos"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _item_key(name: str, media_type: str) -> str:
    return f"{normalize_category_name(name)}::{media_type}"


def build_gallery_url(name: str, media_type: MediaType) -> str:
    params = {"name": name.strip()}
    if media_type == "images":
        params["images"] = "1"
    return f"/admin/category?{urlencode(params)}"


def _serialize_item(name: str, media_type: MediaType, added_at: datetime) -> dict:
    display_name = name.strip()
    return {
        "name": display_name,
        "media_type": media_type,
        "added_at": added_at.astimezone(timezone.utc).isoformat(),
        "gallery_url": build_gallery_url(display_name, media_type),
    }


def _load_raw_items(session: Session) -> list[dict]:
    row = session.scalar(select(AppConfig).where(AppConfig.key == GALLERY_DOWNLOAD_LIST_KEY))
    if not row or not row.value:
        return []
    try:
        parsed = json.loads(row.value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _save_raw_items(session: Session, items: list[dict]) -> None:
    serialized = json.dumps(items, ensure_ascii=False)
    row = session.scalar(select(AppConfig).where(AppConfig.key == GALLERY_DOWNLOAD_LIST_KEY))
    if row:
        row.value = serialized
    else:
        session.add(AppConfig(key=GALLERY_DOWNLOAD_LIST_KEY, value=serialized))
    session.commit()


def _parse_added_at(value: object) -> datetime:
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            pass
    return utcnow()


def _normalize_media_type(value: str | None) -> MediaType | None:
    normalized = str(value or "").strip().lower()
    if normalized in {"images", "videos"}:
        return normalized  # type: ignore[return-value]
    return None


def _validate_name(name: str, blacklisted_categories: set[str]) -> str:
    normalized = normalize_category_name(name)
    if not normalized:
        raise ValueError("Nome de galeria invalido.")
    if normalized in blacklisted_categories:
        raise ValueError("Galeria esta na blacklist.")
    return name.strip()


def list_items(session: Session) -> list[dict]:
    items: list[dict] = []
    for raw in _load_raw_items(session):
        name = str(raw.get("name") or "").strip()
        media_type = _normalize_media_type(raw.get("media_type"))
        if not name or not media_type:
            continue
        items.append(_serialize_item(name, media_type, _parse_added_at(raw.get("added_at"))))
    items.sort(key=lambda item: item["added_at"], reverse=True)
    return items


def add_item(
    session: Session,
    name: str,
    media_type: MediaType,
    *,
    blacklisted_categories: set[str] | None = None,
) -> tuple[dict, bool]:
    blacklist = blacklisted_categories if blacklisted_categories is not None else load_blacklisted_categories(session)
    display_name = _validate_name(name, blacklist)
    key = _item_key(display_name, media_type)

    raw_items = _load_raw_items(session)
    for raw in raw_items:
        existing_name = str(raw.get("name") or "").strip()
        existing_type = _normalize_media_type(raw.get("media_type"))
        if not existing_name or not existing_type:
            continue
        if _item_key(existing_name, existing_type) == key:
            return _serialize_item(existing_name, existing_type, _parse_added_at(raw.get("added_at"))), False

    now = utcnow()
    raw_items.append(
        {
            "name": display_name,
            "media_type": media_type,
            "added_at": now.isoformat(),
        }
    )
    _save_raw_items(session, raw_items)
    return _serialize_item(display_name, media_type, now), True


def remove_item(
    session: Session,
    name: str,
    media_type: MediaType,
) -> bool:
    display_name = str(name or "").strip()
    if not display_name:
        return False
    key = _item_key(display_name, media_type)
    raw_items = _load_raw_items(session)
    filtered: list[dict] = []
    removed = False
    for raw in raw_items:
        existing_name = str(raw.get("name") or "").strip()
        existing_type = _normalize_media_type(raw.get("media_type"))
        if not existing_name or not existing_type:
            continue
        if _item_key(existing_name, existing_type) == key:
            removed = True
            continue
        filtered.append(raw)
    if not removed:
        return False
    _save_raw_items(session, filtered)
    return True
