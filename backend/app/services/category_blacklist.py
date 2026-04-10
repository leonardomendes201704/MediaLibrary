from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AppConfig


CATEGORY_BLACKLIST_KEY = "category_blacklist_v1"


def normalize_category_name(value: str) -> str:
    normalized = value.strip()
    if normalized.lower().startswith("r/"):
        normalized = normalized[2:]
    return normalized.lower()


def extract_path_badges(original_path: str) -> tuple[str | None, str | None]:
    parts = [part for part in Path(original_path).parts if part]
    if len(parts) < 2:
        return None, None

    lower_parts = [part.lower() for part in parts]
    videos_idx = -1
    for idx, value in enumerate(lower_parts):
        if value == "videos":
            videos_idx = idx

    if videos_idx > 0:
        category = parts[videos_idx - 1]
        date_value = None
        if videos_idx + 1 < len(parts) - 1:
            date_value = parts[videos_idx + 1]
        return category, date_value

    file_parent = Path(original_path).parent
    date_value = file_parent.name if file_parent.name else None
    category = file_parent.parent.parent.name if len(file_parent.parents) >= 2 else None
    return category, date_value


def extract_download_root(original_path: str, known_root: str | Path | None = None) -> Path | None:
    source_path = Path(original_path)
    try:
        resolved_source = source_path.resolve(strict=False)
    except OSError:
        resolved_source = source_path

    if known_root is not None:
        configured_root = Path(known_root)
        try:
            resolved_root = configured_root.resolve(strict=False)
        except OSError:
            resolved_root = configured_root
        try:
            resolved_source.relative_to(resolved_root)
            return resolved_root
        except ValueError:
            pass

    parts = [part for part in resolved_source.parts if part]
    if len(parts) < 5:
        return None

    lower_parts = [part.lower() for part in parts]
    videos_idx = -1
    for idx, value in enumerate(lower_parts):
        if value == "videos":
            videos_idx = idx

    if videos_idx <= 0 or videos_idx + 1 >= len(parts) - 1:
        return None

    root_candidate = Path(*parts[: videos_idx - 1])
    root_name = root_candidate.name.strip().lower()
    if "download" not in root_name:
        return None
    return root_candidate


def load_blacklisted_categories(session: Session) -> set[str]:
    row = session.scalar(select(AppConfig).where(AppConfig.key == CATEGORY_BLACKLIST_KEY))
    if not row or not row.value:
        return set()

    try:
        parsed = json.loads(row.value)
    except json.JSONDecodeError:
        return set()

    if not isinstance(parsed, list):
        return set()

    blacklisted: set[str] = set()
    for item in parsed:
        if not isinstance(item, str):
            continue
        normalized = normalize_category_name(item)
        if normalized:
            blacklisted.add(normalized)
    return blacklisted


def save_blacklisted_categories(session: Session, names: list[str] | set[str]) -> set[str]:
    normalized_names: set[str] = set()
    for name in names:
        normalized = normalize_category_name(str(name))
        if normalized:
            normalized_names.add(normalized)

    serialized = json.dumps(sorted(normalized_names), ensure_ascii=False)
    row = session.scalar(select(AppConfig).where(AppConfig.key == CATEGORY_BLACKLIST_KEY))
    if row:
        row.value = serialized
    else:
        session.add(AppConfig(key=CATEGORY_BLACKLIST_KEY, value=serialized))
    session.commit()
    return normalized_names


def add_blacklisted_category(session: Session, name: str) -> set[str]:
    normalized = normalize_category_name(name)
    current = load_blacklisted_categories(session)
    if not normalized:
        return current
    current.add(normalized)
    return save_blacklisted_categories(session, current)
