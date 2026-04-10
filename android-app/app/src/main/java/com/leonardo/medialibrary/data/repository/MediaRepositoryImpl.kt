package com.leonardo.medialibrary.data.repository

import com.leonardo.medialibrary.data.local.AppPreferences
import com.leonardo.medialibrary.data.remote.BackendApiFactory
import com.leonardo.medialibrary.data.remote.FavoriteUpdateRequestDto
import com.leonardo.medialibrary.data.remote.VideoDto
import com.leonardo.medialibrary.data.remote.WatchedUpdateRequestDto
import com.leonardo.medialibrary.domain.model.FeedCategory
import com.leonardo.medialibrary.domain.model.FeedPage
import com.leonardo.medialibrary.domain.model.FeedMode
import com.leonardo.medialibrary.domain.model.PlaybackResumeState
import com.leonardo.medialibrary.domain.model.ProviderAccessRules
import com.leonardo.medialibrary.domain.model.VideoItem
import com.leonardo.medialibrary.domain.repository.MediaRepository
import java.net.URI

class MediaRepositoryImpl(
    private val preferences: AppPreferences,
    private val apiFactory: BackendApiFactory,
) : MediaRepository {
    private val randomFeedEnabled: Boolean = true
    private val hideFavoritedFromNormalFeed: Boolean = true
    private val hideWatchedFromNormalFeed: Boolean = true
    private val newestOrderKey: String = "detected_desc"
    private val favoritesOrderKey: String = "favorite_desc"

    override suspend fun getBackendUrl(): String {
        return preferences.getBackendUrl()
    }

    override suspend fun saveBackendUrl(rawUrl: String) {
        preferences.saveBackendUrl(rawUrl)
    }

    override suspend fun testConnection(rawUrl: String): Result<String> {
        return runCatching {
            val normalized = AppPreferences.normalizeBackendUrl(rawUrl)
            val api = apiFactory.create(normalized)
            val health = api.health()
            if (health.status.lowercase() != "ok") {
                error("Backend respondeu com status inválido: ${health.status}")
            }
            normalized
        }
    }

    override suspend fun loadFeed(cursor: String?, pageSize: Int): FeedPage {
        val baseUrl = preferences.getBackendUrl()
        require(baseUrl.isNotBlank()) { "Backend URL not configured" }
        val mode = getActiveFeedMode()
        val selectedCategory = preferences.getSelectedCategory().trim()
        val options = feedRequestOptions(mode, selectedCategory)
        val api = apiFactory.create(baseUrl)
        val payload = api.feed(
            cursor = cursor,
            pageSize = pageSize,
            randomOrder = options.randomOrder,
            excludeFavorites = options.excludeFavorites,
            onlyFavorites = options.onlyFavorites,
            excludeWatched = options.excludeWatched,
            onlyWatched = options.onlyWatched,
            orderBy = options.orderBy,
            category = options.category,
        )
        val items = payload.items.map { dto ->
            dto.toDomain(baseUrl)
        }
        return FeedPage(items = items, nextCursor = payload.nextCursor)
    }

    override suspend fun loadFeedCategories(): List<FeedCategory> {
        val baseUrl = preferences.getBackendUrl()
        require(baseUrl.isNotBlank()) { "Backend URL not configured" }
        val api = apiFactory.create(baseUrl)
        return api.feedCategories()
            .map { dto ->
                FeedCategory(name = dto.category.trim(), count = dto.count.coerceAtLeast(0))
            }
            .filter { it.name.isNotBlank() && it.count > 0 }
            .sortedWith(compareByDescending<FeedCategory> { it.count }.thenBy { it.name.lowercase() })
    }

    override suspend fun getActiveFeedMode(): FeedMode {
        val providerName = preferences.getProviderName()
        return ProviderAccessRules.resolveMode(providerName) ?: FeedMode.NORMAL
    }

    override suspend fun getSelectedCategory(): String {
        return preferences.getSelectedCategory()
    }

    override suspend fun saveSelectedCategory(category: String) {
        preferences.saveSelectedCategory(category)
    }

    override suspend fun clearSelectedCategory() {
        preferences.clearSelectedCategory()
    }

    private fun feedRequestOptions(mode: FeedMode, selectedCategory: String): FeedRequestOptions {
        return when (mode) {
            FeedMode.NORMAL -> FeedRequestOptions(
                randomOrder = randomFeedEnabled,
                excludeFavorites = hideFavoritedFromNormalFeed,
                onlyFavorites = false,
                excludeWatched = hideWatchedFromNormalFeed,
                onlyWatched = false,
                orderBy = null,
                category = null,
            )

            FeedMode.FAVORITES_ONLY -> FeedRequestOptions(
                randomOrder = false,
                excludeFavorites = false,
                onlyFavorites = true,
                excludeWatched = false,
                onlyWatched = false,
                orderBy = favoritesOrderKey,
                category = null,
            )

            FeedMode.NEWEST -> FeedRequestOptions(
                randomOrder = false,
                excludeFavorites = hideFavoritedFromNormalFeed,
                onlyFavorites = false,
                excludeWatched = false,
                onlyWatched = false,
                orderBy = newestOrderKey,
                category = null,
            )

            FeedMode.CATEGORIES -> FeedRequestOptions(
                randomOrder = randomFeedEnabled,
                excludeFavorites = hideFavoritedFromNormalFeed,
                onlyFavorites = false,
                excludeWatched = hideWatchedFromNormalFeed,
                onlyWatched = false,
                orderBy = null,
                category = selectedCategory.ifBlank {
                    error("Categoria nao selecionada.")
                },
            )
        }
    }

    private data class FeedRequestOptions(
        val randomOrder: Boolean,
        val excludeFavorites: Boolean,
        val onlyFavorites: Boolean,
        val excludeWatched: Boolean,
        val onlyWatched: Boolean,
        val orderBy: String?,
        val category: String?,
    )

    override suspend fun getPlaybackResumeState(): PlaybackResumeState? {
        val local = preferences.getPlaybackState() ?: return null
        return PlaybackResumeState(
            videoId = local.videoId,
            positionMs = local.positionMs.coerceAtLeast(0L),
        )
    }

    override suspend fun savePlaybackResumeState(videoId: Int, positionMs: Long) {
        preferences.savePlaybackState(videoId = videoId, positionMs = positionMs.coerceAtLeast(0L))
    }

    override suspend fun setVideoFavorite(videoId: Int, isFavorite: Boolean): Boolean {
        val baseUrl = preferences.getBackendUrl()
        require(baseUrl.isNotBlank()) { "Backend URL not configured" }
        val api = apiFactory.create(baseUrl)
        val response = api.setFavorite(
            videoId = videoId,
            payload = FavoriteUpdateRequestDto(isFavorite = isFavorite),
        )
        if (!response.success || response.videoId != videoId) {
            error("Falha ao atualizar favorito no backend")
        }
        return response.isFavorite
    }

    override suspend fun setVideoWatched(videoId: Int, isWatched: Boolean, watchedRatio: Float?): Boolean {
        val baseUrl = preferences.getBackendUrl()
        require(baseUrl.isNotBlank()) { "Backend URL not configured" }
        val api = apiFactory.create(baseUrl)
        val normalizedRatio = watchedRatio?.coerceIn(0f, 1f)
        val response = api.setWatched(
            videoId = videoId,
            payload = WatchedUpdateRequestDto(
                isWatched = isWatched,
                watchedRatio = normalizedRatio,
            ),
        )
        if (!response.success || response.videoId != videoId) {
            error("Falha ao atualizar visto no backend")
        }
        return response.isWatched
    }

    override suspend fun deleteVideoPermanently(videoId: Int): Boolean {
        val baseUrl = preferences.getBackendUrl()
        require(baseUrl.isNotBlank()) { "Backend URL not configured" }
        val api = apiFactory.create(baseUrl)
        val response = api.deleteVideo(videoId = videoId, permanent = true)
        if (!response.success) {
            error("Falha ao excluir video no backend")
        }
        return response.success
    }

    private fun VideoDto.toDomain(baseUrl: String): VideoItem {
        val resolvedStream = resolveMediaUrl(
            original = streamUrl,
            fallback = "videos/$id/stream",
            baseUrl = baseUrl,
        )
        return VideoItem(
            id = id,
            streamUrl = resolvedStream,
            thumbnailUrl = resolveMediaUrl(
                original = thumbnailUrl,
                fallback = "videos/$id/thumbnail",
                baseUrl = baseUrl,
            ),
            durationSeconds = durationSeconds,
            width = width,
            height = height,
            status = status,
            isFavorite = isFavorite,
            isWatched = isWatched,
            pathCategory = pathCategory,
            pathDate = pathDate,
        )
    }

    private fun resolveMediaUrl(original: String?, fallback: String, baseUrl: String): String {
        val value = original?.trim().orEmpty()
        val baseUri = URI.create(baseUrl)
        val origin = "${baseUri.scheme}://${baseUri.host}${if (baseUri.port > 0) ":${baseUri.port}" else ""}"
        if (value.startsWith("http://") || value.startsWith("https://")) {
            return normalizeAbsoluteUrl(value, baseUri)
        }
        return when {
            value.startsWith("/") -> "$origin$value"
            value.isNotBlank() -> baseUrl + value.removePrefix("/")
            else -> baseUrl + fallback
        }
    }

    private fun normalizeAbsoluteUrl(raw: String, baseUri: URI): String {
        val uri = URI.create(raw)
        val host = uri.host?.lowercase().orEmpty()
        if (host == "0.0.0.0" || host == "127.0.0.1" || host == "localhost") {
            val normalizedHost = baseUri.host
            val normalizedPort = if (baseUri.port > 0) ":${baseUri.port}" else ""
            return "${baseUri.scheme}://$normalizedHost$normalizedPort${uri.rawPath}${if (uri.rawQuery != null) "?${uri.rawQuery}" else ""}"
        }
        return raw
    }
}
