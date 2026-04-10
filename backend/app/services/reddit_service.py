from __future__ import annotations

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
                listing_paths.insert(0, f"/user/{quote(username, safe='')}/submitted")

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
