package com.leonardo.medialibrary.domain.repository

import com.leonardo.medialibrary.domain.model.FeedPage
import com.leonardo.medialibrary.domain.model.FeedCategory
import com.leonardo.medialibrary.domain.model.FeedMode
import com.leonardo.medialibrary.domain.model.PlaybackResumeState

interface MediaRepository {
    suspend fun getBackendUrl(): String
    suspend fun saveBackendUrl(rawUrl: String)
    suspend fun testConnection(rawUrl: String): Result<String>
    suspend fun loadFeed(cursor: String?, pageSize: Int): FeedPage
    suspend fun loadFeedCategories(): List<FeedCategory>
    suspend fun getActiveFeedMode(): FeedMode
    suspend fun getSelectedCategory(): String
    suspend fun saveSelectedCategory(category: String)
    suspend fun clearSelectedCategory()
    suspend fun getPlaybackResumeState(): PlaybackResumeState?
    suspend fun savePlaybackResumeState(videoId: Int, positionMs: Long)
    suspend fun setVideoFavorite(videoId: Int, isFavorite: Boolean): Boolean
    suspend fun setVideoWatched(videoId: Int, isWatched: Boolean, watchedRatio: Float? = null): Boolean
    suspend fun deleteVideoPermanently(videoId: Int): Boolean
}
