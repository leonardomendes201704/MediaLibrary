from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Any, Callable
from urllib.parse import quote

import httpx

from app.core.config import Settings


class RedditServiceError(Exception):
    pass


class RedditConfigError(RedditServiceError):
    pass


class RedditApiError(RedditServiceError):
    pass


class RedditService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._access_token: str | None = None
        self._token_expires_at: datetime = datetime.min.replace(tzinfo=timezone.utc)

    def missing_config_fields(self) -> list[str]:
        missing: list[str] = []
        if not self._settings.reddit_client_id:
            missing.append("REDDIT_CLIENT_ID")
        if not self._settings.reddit_client_secret:
            missing.append("REDDIT_CLIENT_SECRET")
        if not self._settings.reddit_username:
            missing.append("REDDIT_USERNAME")
        if not self._settings.reddit_password:
            missing.append("REDDIT_PASSWORD")
        if not self._settings.reddit_user_agent:
            missing.append("REDDIT_USER_AGENT")
        return missing

    @property
    def is_configured(self) -> bool:
        return len(self.missing_config_fields()) == 0

    def _ensure_configured(self) -> None:
        missing = self.missing_config_fields()
        if missing:
            raise RedditConfigError(
                "Reddit env vars ausentes: " + ", ".join(missing)
            )

    async def _request_access_token(self) -> str:
        self._ensure_configured()
        timeout = httpx.Timeout(float(self._settings.reddit_timeout_seconds))
        auth = (self._settings.reddit_client_id or "", self._settings.reddit_client_secret or "")
        headers = {"User-Agent": self._settings.reddit_user_agent}
        payload = {
            "grant_type": "password",
            "username": self._settings.reddit_username or "",
            "password": self._settings.reddit_password or "",
        }

        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                "https://www.reddit.com/api/v1/access_token",
                auth=auth,
                data=payload,
                headers=headers,
            )
            if response.status_code >= 400:
                raise RedditApiError(
                    f"Falha ao autenticar no Reddit: {response.status_code} {response.text}"
                )
            data = response.json()

        token = str(data.get("access_token") or "").strip()
        expires_in = int(data.get("expires_in") or 0)
        if not token or expires_in <= 0:
            raise RedditApiError("Resposta de token Reddit invalida.")

        # Refresh a little before real expiration.
        self._access_token = token
        self._token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=max(30, expires_in - 60))
        return token

    async def _get_access_token(self) -> str:
        now = datetime.now(timezone.utc)
        if self._access_token and now < self._token_expires_at:
            return self._access_token
        return await self._request_access_token()

    @staticmethod
    def _safe_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _normalize_subscription_item(cls, child_data: dict[str, Any]) -> dict[str, Any] | None:
        subreddit_data = child_data.get("subreddit") if isinstance(child_data.get("subreddit"), dict) else {}
        display_name = str(
            child_data.get("display_name")
            or subreddit_data.get("display_name")
            or subreddit_data.get("display_name_prefixed")
            or ""
        ).strip()
        if display_name.lower().startswith("r/"):
            display_name = display_name[2:]
        if not display_name:
            return None

        title = str(child_data.get("title") or subreddit_data.get("title") or "").strip()
        over18 = bool(child_data.get("over18") if child_data.get("over18") is not None else subreddit_data.get("over_18"))
        subscribers = cls._safe_int(child_data.get("subscribers"))
        if subscribers <= 0:
            subscribers = cls._safe_int(subreddit_data.get("subscribers"))
        url = str(child_data.get("url") or subreddit_data.get("url") or "").strip()

        return {
            "name": display_name,
            "title": title,
            "over18": over18,
            "subscribers": subscribers,
            "url": url,
        }

    async def list_subscribed_subreddits(self) -> list[dict[str, Any]]:
        token = await self._get_access_token()
        timeout = httpx.Timeout(float(self._settings.reddit_timeout_seconds))
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": self._settings.reddit_user_agent,
        }

        after: str | None = None
        seen_after: set[str] = set()
        page = 0
        results: list[dict[str, Any]] = []
        seen_names: set[str] = set()

        async with httpx.AsyncClient(timeout=timeout) as client:
            while True:
                page += 1
                params: dict[str, str | int] = {"limit": 100}
                if after:
                    params["after"] = after

                response = await client.get(
                    "https://oauth.reddit.com/subreddits/mine/subscriber",
                    headers=headers,
                    params=params,
                )
                if response.status_code >= 400:
                    raise RedditApiError(
                        f"Falha ao buscar subreddits assinados: {response.status_code} {response.text}"
                    )
                payload = response.json()
                data = payload.get("data") or {}
                children = data.get("children") or []
                for child in children:
                    child_data = child.get("data") or {}
                    normalized_item = self._normalize_subscription_item(child_data)
                    if not normalized_item:
                        continue
                    name = str(normalized_item["name"]).strip()
                    normalized_name = name.lower()
                    if normalized_name in seen_names:
                        continue
                    seen_names.add(normalized_name)
                    results.append(normalized_item)

                raw_after = data.get("after")
                next_after = str(raw_after).strip() if raw_after else None
                if not next_after:
                    break
                if next_after in seen_after:
                    break
                seen_after.add(next_after)
                after = next_after

        results.sort(key=lambda item: item["name"].lower())
        return results

    @staticmethod
    def _extract_video_urls(post_data: dict[str, Any]) -> list[str]:
        urls: list[str] = []
        seen: set[str] = set()

        preview = post_data.get("preview") or {}
        if isinstance(preview, dict):
            reddit_video_preview = preview.get("reddit_video_preview") or {}
            if isinstance(reddit_video_preview, dict):
                fallback = str(reddit_video_preview.get("fallback_url") or "").strip()
                if fallback:
                    normalized = unescape(fallback)
                    if normalized and normalized not in seen:
                        seen.add(normalized)
                        urls.append(normalized)

        media = post_data.get("media") or {}
        if isinstance(media, dict):
            reddit_video = media.get("reddit_video") or {}
            if isinstance(reddit_video, dict):
                fallback = str(reddit_video.get("fallback_url") or "").strip()
                if fallback:
                    normalized = unescape(fallback)
                    if normalized and normalized not in seen:
                        seen.add(normalized)
                        urls.append(normalized)

        return urls

    _IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
    _REDDIT_IMAGE_HOST_MARKERS = ("i.redd.it", "preview.redd.it", "external-preview.redd.it")

    @classmethod
    def _is_reddit_image_url(cls, value: str) -> bool:
        normalized = value.strip()
        if not normalized:
            return False
        lower = normalized.lower()
        if any(marker in lower for marker in cls._REDDIT_IMAGE_HOST_MARKERS):
            return True
        return lower.endswith(cls._IMAGE_EXTENSIONS)

    @classmethod
    def _extract_image_urls(cls, post_data: dict[str, Any]) -> list[str]:
        if bool(post_data.get("is_video")):
            return []

        urls: list[str] = []
        seen: set[str] = set()

        if bool(post_data.get("is_gallery")):
            gallery_data = post_data.get("gallery_data") or {}
            media_metadata = post_data.get("media_metadata") or {}
            if isinstance(gallery_data, dict) and isinstance(media_metadata, dict):
                items = gallery_data.get("items") or []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    media_id = str(item.get("media_id") or "").strip()
                    if not media_id:
                        continue
                    metadata = media_metadata.get(media_id)
                    if not isinstance(metadata, dict):
                        continue
                    if str(metadata.get("status") or "").strip().lower() != "valid":
                        continue
                    source = metadata.get("s") or {}
                    if not isinstance(source, dict):
                        continue
                    image_url = unescape(str(source.get("u") or "").strip())
                    if image_url and image_url not in seen:
                        seen.add(image_url)
                        urls.append(image_url)
            return urls

        preview = post_data.get("preview") or {}
        if isinstance(preview, dict):
            images = preview.get("images") or []
            if images and isinstance(images[0], dict):
                source = images[0].get("source") or {}
                if isinstance(source, dict):
                    image_url = unescape(str(source.get("u") or "").strip())
                    if image_url and image_url not in seen:
                        seen.add(image_url)
                        urls.append(image_url)

        for field in ("url_overridden_by_dest", "url"):
            candidate = unescape(str(post_data.get(field) or "").strip())
            if not candidate:
                continue
            if cls._is_reddit_image_url(candidate) and candidate not in seen:
                seen.add(candidate)
                urls.append(candidate)

        return urls

    @staticmethod
    def _extract_direct_video_like_urls(post_data: dict[str, Any]) -> list[str]:
        urls: list[str] = []
        seen: set[str] = set()
        candidates = [
            str(post_data.get("url_overridden_by_dest") or "").strip(),
            str(post_data.get("url") or "").strip(),
        ]
        for candidate in candidates:
            if not candidate:
                continue
            normalized = unescape(candidate)
            lower = normalized.lower()
            if lower.endswith(".gifv"):
                normalized = normalized[:-5] + ".mp4"
                lower = normalized.lower()
            if lower.endswith((".mp4", ".m4v", ".mov", ".webm", ".gif")):
                if normalized not in seen:
                    seen.add(normalized)
                    urls.append(normalized)
        return urls

    async def list_latest_video_posts(
        self,
        subreddit: str,
        limit: int | None = 10,
        cutoff_datetime: datetime | None = None,
        max_pages: int | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        normalized_subreddit = subreddit.strip()
        if normalized_subreddit.lower().startswith("r/"):
            normalized_subreddit = normalized_subreddit[2:]
        if not normalized_subreddit:
            return []

        target_limit = max(1, int(limit)) if limit is not None else None
        normalized_cutoff = cutoff_datetime.astimezone(timezone.utc) if cutoff_datetime else None
        token = await self._get_access_token()
        timeout = httpx.Timeout(float(self._settings.reddit_timeout_seconds))
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": self._settings.reddit_user_agent,
        }

        after: str | None = None
        seen_after: set[str] = set()
        results: list[dict[str, Any]] = []
        seen_video_urls: set[str] = set()
        effective_max_pages = max(1, int(max_pages or (200 if normalized_cutoff else 30)))

        listing_paths = [f"/r/{normalized_subreddit}/new"]
        if normalized_subreddit.lower().startswith("u_"):
            username = normalized_subreddit[2:].strip()
            if username:
                listing_paths = [f"/user/{quote(username, safe='')}/submitted"]

        async with httpx.AsyncClient(timeout=timeout) as client:
            for listing_path in listing_paths:
                after = None
                seen_after = set()
                page_count = 0
                while (target_limit is None or len(results) < target_limit) and page_count < effective_max_pages:
                    page_count += 1
                    params: dict[str, str | int] = {"limit": 100}
                    if after:
                        params["after"] = after

                    response = await client.get(
                        f"https://oauth.reddit.com{listing_path}",
                        headers=headers,
                        params=params,
                    )
                    if response.status_code >= 400:
                        continue

                    payload = response.json()
                    data = payload.get("data") or {}
                    children = data.get("children") or []
                    oldest_created_at_on_page: datetime | None = None

                    for child in children:
                        child_data = child.get("data") or {}
                        post_id = str(child_data.get("id") or "").strip()
                        permalink = str(child_data.get("permalink") or "").strip()
                        title = str(child_data.get("title") or "").strip()
                        created_utc_raw = child_data.get("created_utc")
                        try:
                            created_utc = float(created_utc_raw)
                        except (TypeError, ValueError):
                            created_utc = 0.0
                        created_at_utc = (
                            datetime.fromtimestamp(created_utc, tz=timezone.utc)
                            if created_utc > 0
                            else datetime.now(timezone.utc)
                        )
                        if oldest_created_at_on_page is None or created_at_utc < oldest_created_at_on_page:
                            oldest_created_at_on_page = created_at_utc
                        if normalized_cutoff and created_at_utc < normalized_cutoff:
                            continue
                        post_url = f"https://reddit.com{permalink}" if permalink else ""

                        candidates = [
                            *self._extract_video_urls(child_data),
                            *self._extract_direct_video_like_urls(child_data),
                        ]
                        for video_url in candidates:
                            if video_url in seen_video_urls:
                                continue
                            seen_video_urls.add(video_url)
                            results.append(
                                {
                                    "subreddit": normalized_subreddit,
                                    "post_id": post_id,
                                    "title": title,
                                    "post_url": post_url,
                                    "video_url": video_url,
                                    "created_at_utc": created_at_utc.isoformat(),
                                }
                            )
                            if target_limit is not None and len(results) >= target_limit:
                                break
                        if target_limit is not None and len(results) >= target_limit:
                            break

                    raw_after = data.get("after")
                    next_after = str(raw_after).strip() if raw_after else None
                    if progress_callback:
                        progress_callback(
                            {
                                "page": page_count,
                                "found": len(results),
                                "oldest_created_at": oldest_created_at_on_page.isoformat() if oldest_created_at_on_page else None,
                                "reached_cutoff": bool(
                                    normalized_cutoff
                                    and oldest_created_at_on_page
                                    and oldest_created_at_on_page < normalized_cutoff
                                ),
                            }
                        )
                    if normalized_cutoff and oldest_created_at_on_page and oldest_created_at_on_page < normalized_cutoff:
                        break
                    if not next_after:
                        break
                    if next_after in seen_after:
                        break
                    seen_after.add(next_after)
                    after = next_after
                if target_limit is not None and len(results) >= target_limit:
                    break

        return results[:target_limit] if target_limit is not None else results

    async def count_image_posts_in_period(
        self,
        subreddit: str,
        cutoff_datetime: datetime,
        max_pages: int | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> int:
        """Fast path for year image counts: count only, single listing, shared client."""
        normalized_subreddit = subreddit.strip()
        if normalized_subreddit.lower().startswith("r/"):
            normalized_subreddit = normalized_subreddit[2:]
        if not normalized_subreddit:
            return 0

        normalized_cutoff = cutoff_datetime.astimezone(timezone.utc)
        token = await self._get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": self._settings.reddit_user_agent,
        }
        effective_max_pages = max(1, int(max_pages or 180))

        # Prefer the user submitted feed for u_*; avoid scanning /r/u_* as a second pass.
        if normalized_subreddit.lower().startswith("u_"):
            username = normalized_subreddit[2:].strip()
            listing_path = f"/user/{quote(username, safe='')}/submitted" if username else f"/r/{normalized_subreddit}/new"
        else:
            listing_path = f"/r/{normalized_subreddit}/new"

        async def _run(http_client: httpx.AsyncClient) -> int:
            after: str | None = None
            seen_after: set[str] = set()
            seen_image_urls: set[str] = set()
            page_count = 0
            while page_count < effective_max_pages:
                page_count += 1
                params: dict[str, str | int] = {"limit": 100, "raw_json": 1}
                if after:
                    params["after"] = after

                response = None
                for attempt in range(4):
                    response = await http_client.get(
                        f"https://oauth.reddit.com{listing_path}",
                        headers=headers,
                        params=params,
                    )
                    if response.status_code == 429:
                        retry_after = float(response.headers.get("Retry-After") or (1.5 * (attempt + 1)))
                        await asyncio.sleep(min(20.0, max(0.5, retry_after)))
                        continue
                    if response.status_code >= 500:
                        await asyncio.sleep(0.75 * (attempt + 1))
                        continue
                    break
                if response is None or response.status_code >= 400:
                    break

                payload = response.json()
                data = payload.get("data") or {}
                children = data.get("children") or []
                if not children:
                    break

                oldest_created_at_on_page: datetime | None = None
                reached_cutoff = False
                for child in children:
                    child_data = child.get("data") or {}
                    created_utc_raw = child_data.get("created_utc")
                    try:
                        created_utc = float(created_utc_raw)
                    except (TypeError, ValueError):
                        created_utc = 0.0
                    created_at_utc = (
                        datetime.fromtimestamp(created_utc, tz=timezone.utc)
                        if created_utc > 0
                        else datetime.now(timezone.utc)
                    )
                    if oldest_created_at_on_page is None or created_at_utc < oldest_created_at_on_page:
                        oldest_created_at_on_page = created_at_utc
                    if created_at_utc < normalized_cutoff:
                        reached_cutoff = True
                        continue
                    for image_url in self._extract_image_urls(child_data):
                        if image_url in seen_image_urls:
                            continue
                        seen_image_urls.add(image_url)

                if progress_callback:
                    progress_callback(
                        {
                            "page": page_count,
                            "found": len(seen_image_urls),
                            "oldest_created_at": (
                                oldest_created_at_on_page.isoformat() if oldest_created_at_on_page else None
                            ),
                            "reached_cutoff": reached_cutoff,
                        }
                    )
                if reached_cutoff:
                    break

                raw_after = data.get("after")
                next_after = str(raw_after).strip() if raw_after else None
                if not next_after or next_after in seen_after:
                    break
                seen_after.add(next_after)
                after = next_after

            return len(seen_image_urls)

        if client is not None:
            return await _run(client)

        timeout = httpx.Timeout(float(self._settings.reddit_timeout_seconds))
        async with httpx.AsyncClient(timeout=timeout) as owned_client:
            return await _run(owned_client)

    async def list_latest_image_posts(
        self,
        subreddit: str,
        limit: int | None = 10,
        cutoff_datetime: datetime | None = None,
        max_pages: int | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        normalized_subreddit = subreddit.strip()
        if normalized_subreddit.lower().startswith("r/"):
            normalized_subreddit = normalized_subreddit[2:]
        if not normalized_subreddit:
            return []

        target_limit = max(1, int(limit)) if limit is not None else None
        normalized_cutoff = cutoff_datetime.astimezone(timezone.utc) if cutoff_datetime else None
        token = await self._get_access_token()
        timeout = httpx.Timeout(float(self._settings.reddit_timeout_seconds))
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": self._settings.reddit_user_agent,
        }

        after: str | None = None
        seen_after: set[str] = set()
        results: list[dict[str, Any]] = []
        seen_image_urls: set[str] = set()
        effective_max_pages = max(1, int(max_pages or (200 if normalized_cutoff else 30)))

        listing_paths = [f"/r/{normalized_subreddit}/new"]
        if normalized_subreddit.lower().startswith("u_"):
            username = normalized_subreddit[2:].strip()
            if username:
                listing_paths = [f"/user/{quote(username, safe='')}/submitted"]

        async with httpx.AsyncClient(timeout=timeout) as client:
            for listing_path in listing_paths:
                after = None
                seen_after = set()
                page_count = 0
                while (target_limit is None or len(results) < target_limit) and page_count < effective_max_pages:
                    page_count += 1
                    params: dict[str, str | int] = {"limit": 100}
                    if after:
                        params["after"] = after

                    response = await client.get(
                        f"https://oauth.reddit.com{listing_path}",
                        headers=headers,
                        params=params,
                    )
                    if response.status_code >= 400:
                        continue

                    payload = response.json()
                    data = payload.get("data") or {}
                    children = data.get("children") or []
                    oldest_created_at_on_page: datetime | None = None

                    for child in children:
                        child_data = child.get("data") or {}
                        post_id = str(child_data.get("id") or "").strip()
                        permalink = str(child_data.get("permalink") or "").strip()
                        title = str(child_data.get("title") or "").strip()
                        created_utc_raw = child_data.get("created_utc")
                        try:
                            created_utc = float(created_utc_raw)
                        except (TypeError, ValueError):
                            created_utc = 0.0
                        created_at_utc = (
                            datetime.fromtimestamp(created_utc, tz=timezone.utc)
                            if created_utc > 0
                            else datetime.now(timezone.utc)
                        )
                        if oldest_created_at_on_page is None or created_at_utc < oldest_created_at_on_page:
                            oldest_created_at_on_page = created_at_utc
                        if normalized_cutoff and created_at_utc < normalized_cutoff:
                            continue
                        post_url = f"https://reddit.com{permalink}" if permalink else ""

                        for image_url in self._extract_image_urls(child_data):
                            if image_url in seen_image_urls:
                                continue
                            seen_image_urls.add(image_url)
                            results.append(
                                {
                                    "subreddit": normalized_subreddit,
                                    "post_id": post_id,
                                    "title": title,
                                    "post_url": post_url,
                                    "image_url": image_url,
                                    "created_at_utc": created_at_utc.isoformat(),
                                }
                            )
                            if target_limit is not None and len(results) >= target_limit:
                                break
                        if target_limit is not None and len(results) >= target_limit:
                            break

                    raw_after = data.get("after")
                    next_after = str(raw_after).strip() if raw_after else None
                    if progress_callback:
                        progress_callback(
                            {
                                "page": page_count,
                                "found": len(results),
                                "oldest_created_at": oldest_created_at_on_page.isoformat() if oldest_created_at_on_page else None,
                                "reached_cutoff": bool(
                                    normalized_cutoff
                                    and oldest_created_at_on_page
                                    and oldest_created_at_on_page < normalized_cutoff
                                ),
                            }
                        )
                    if normalized_cutoff and oldest_created_at_on_page and oldest_created_at_on_page < normalized_cutoff:
                        break
                    if not next_after:
                        break
                    if next_after in seen_after:
                        break
                    seen_after.add(next_after)
                    after = next_after
                if target_limit is not None and len(results) >= target_limit:
                    break

        return results[:target_limit] if target_limit is not None else results


    async def list_saved_media_posts(
        self,
        *,
        media_type: str = "both",
        max_pages: int | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """List authenticated user saved posts with downloadable media.

        Reddit listings stop around ~1000 items; we paginate until after is null
        or max_pages is reached (default 12 * 100 ~= API ceiling).
        """
        self._ensure_configured()
        normalized_media_type = str(media_type or "both").strip().lower()
        if normalized_media_type not in {"videos", "images", "both"}:
            raise ValueError("media_type invalido. Use videos, images ou both.")

        username = str(self._settings.reddit_username or "").strip()
        if not username:
            raise RedditConfigError("REDDIT_USERNAME ausente.")

        token = await self._get_access_token()
        timeout = httpx.Timeout(float(self._settings.reddit_timeout_seconds))
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": self._settings.reddit_user_agent,
        }
        listing_path = f"/user/{quote(username, safe='')}/saved"
        effective_max_pages = max(1, min(int(max_pages or 12), 20))

        after: str | None = None
        seen_after: set[str] = set()
        video_posts: list[dict[str, Any]] = []
        image_posts: list[dict[str, Any]] = []
        seen_video_urls: set[str] = set()
        seen_image_urls: set[str] = set()
        total_listing_items = 0
        total_posts = 0
        page_count = 0

        async with httpx.AsyncClient(timeout=timeout) as client:
            while page_count < effective_max_pages:
                page_count += 1
                params: dict[str, str | int] = {"limit": 100, "raw_json": 1}
                if after:
                    params["after"] = after

                response = None
                for attempt in range(4):
                    response = await client.get(
                        f"https://oauth.reddit.com{listing_path}",
                        headers=headers,
                        params=params,
                    )
                    if response.status_code == 429:
                        retry_after = float(response.headers.get("Retry-After") or (1.5 * (attempt + 1)))
                        await asyncio.sleep(min(20.0, max(0.5, retry_after)))
                        continue
                    if response.status_code >= 500:
                        await asyncio.sleep(0.75 * (attempt + 1))
                        continue
                    break
                if response is None or response.status_code >= 400:
                    detail = ""
                    if response is not None:
                        detail = f" HTTP {response.status_code}"
                    raise RedditApiError(f"Falha ao listar posts salvos.{detail}")

                payload = response.json()
                data = payload.get("data") or {}
                children = data.get("children") or []
                if not children:
                    break

                for child in children:
                    kind = str(child.get("kind") or "").strip()
                    total_listing_items += 1
                    if kind != "t3":
                        continue
                    total_posts += 1
                    child_data = child.get("data") or {}
                    post_id = str(child_data.get("id") or "").strip()
                    permalink = str(child_data.get("permalink") or "").strip()
                    title = str(child_data.get("title") or "").strip()
                    subreddit = str(child_data.get("subreddit") or "").strip()
                    if subreddit.lower().startswith("r/"):
                        subreddit = subreddit[2:]
                    created_utc_raw = child_data.get("created_utc")
                    try:
                        created_utc = float(created_utc_raw)
                    except (TypeError, ValueError):
                        created_utc = 0.0
                    created_at_utc = (
                        datetime.fromtimestamp(created_utc, tz=timezone.utc)
                        if created_utc > 0
                        else datetime.now(timezone.utc)
                    )
                    post_url = f"https://reddit.com{permalink}" if permalink else ""

                    if normalized_media_type in {"videos", "both"}:
                        candidates = [
                            *self._extract_video_urls(child_data),
                            *self._extract_direct_video_like_urls(child_data),
                        ]
                        for video_url in candidates:
                            if video_url in seen_video_urls:
                                continue
                            seen_video_urls.add(video_url)
                            video_posts.append(
                                {
                                    "subreddit": subreddit,
                                    "post_id": post_id,
                                    "title": title,
                                    "post_url": post_url,
                                    "video_url": video_url,
                                    "created_at_utc": created_at_utc.isoformat(),
                                    "is_reddit_saved": True,
                                }
                            )

                    if normalized_media_type in {"images", "both"}:
                        for image_url in self._extract_image_urls(child_data):
                            if image_url in seen_image_urls:
                                continue
                            seen_image_urls.add(image_url)
                            image_posts.append(
                                {
                                    "subreddit": subreddit,
                                    "post_id": post_id,
                                    "title": title,
                                    "post_url": post_url,
                                    "image_url": image_url,
                                    "created_at_utc": created_at_utc.isoformat(),
                                    "is_reddit_saved": True,
                                }
                            )

                if progress_callback:
                    progress_callback(
                        {
                            "page": page_count,
                            "listing_items": total_listing_items,
                            "posts": total_posts,
                            "videos_found": len(video_posts),
                            "images_found": len(image_posts),
                            "found": len(video_posts) + len(image_posts),
                        }
                    )

                raw_after = data.get("after")
                next_after = str(raw_after).strip() if raw_after else None
                if not next_after or next_after in seen_after:
                    break
                seen_after.add(next_after)
                after = next_after

        return {
            "videos": video_posts,
            "images": image_posts,
            "total_listing_items": total_listing_items,
            "total_posts": total_posts,
            "pages_scanned": page_count,
            "api_limit_likely_reached": total_listing_items >= 900,
        }
