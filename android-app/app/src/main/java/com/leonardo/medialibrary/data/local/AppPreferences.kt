package com.leonardo.medialibrary.data.local

import android.content.Context
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.intPreferencesKey
import androidx.datastore.preferences.core.longPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map

private val Context.dataStore by preferencesDataStore(name = "media_library_preferences")

data class StoredPlaybackState(
    val videoId: Int,
    val positionMs: Long,
)

class AppPreferences(private val context: Context) {
    private object Keys {
        val BACKEND_URL = stringPreferencesKey("backend_url")
        val PROVIDER_NAME = stringPreferencesKey("provider_name")
        val SELECTED_CATEGORY = stringPreferencesKey("selected_category")
        val LAST_VIDEO_ID = intPreferencesKey("last_video_id")
        val LAST_VIDEO_POSITION_MS = longPreferencesKey("last_video_position_ms")
    }

    val backendUrlFlow: Flow<String> = context.dataStore.data.map { prefs ->
        prefs[Keys.BACKEND_URL].orEmpty()
    }

    suspend fun getBackendUrl(): String {
        return backendUrlFlow.first()
    }

    suspend fun saveBackendUrl(rawUrl: String) {
        val normalized = normalizeBackendUrl(rawUrl)
        context.dataStore.edit { prefs ->
            prefs[Keys.BACKEND_URL] = normalized
        }
    }

    suspend fun getProviderName(): String {
        return context.dataStore.data.map { prefs ->
            prefs[Keys.PROVIDER_NAME].orEmpty()
        }.first()
    }

    suspend fun saveProviderName(name: String) {
        context.dataStore.edit { prefs ->
            prefs[Keys.PROVIDER_NAME] = name.trim()
        }
    }

    suspend fun getSelectedCategory(): String {
        return context.dataStore.data.map { prefs ->
            prefs[Keys.SELECTED_CATEGORY].orEmpty()
        }.first()
    }

    suspend fun saveSelectedCategory(category: String) {
        context.dataStore.edit { prefs ->
            prefs[Keys.SELECTED_CATEGORY] = category.trim()
        }
    }

    suspend fun clearSelectedCategory() {
        context.dataStore.edit { prefs ->
            prefs.remove(Keys.SELECTED_CATEGORY)
        }
    }

    suspend fun getPlaybackState(): StoredPlaybackState? {
        val prefs = context.dataStore.data.first()
        val videoId = prefs[Keys.LAST_VIDEO_ID] ?: return null
        val positionMs = prefs[Keys.LAST_VIDEO_POSITION_MS] ?: 0L
        return StoredPlaybackState(videoId = videoId, positionMs = positionMs)
    }

    suspend fun savePlaybackState(videoId: Int, positionMs: Long) {
        if (videoId <= 0) return
        context.dataStore.edit { prefs ->
            prefs[Keys.LAST_VIDEO_ID] = videoId
            prefs[Keys.LAST_VIDEO_POSITION_MS] = positionMs.coerceAtLeast(0L)
        }
    }

    companion object {
        fun normalizeBackendUrl(rawUrl: String): String {
            var url = rawUrl.trim()
            if (url.isBlank()) return ""
            if (!url.startsWith("http://") && !url.startsWith("https://")) {
                url = "http://$url"
            }
            if (!url.endsWith("/")) {
                url += "/"
            }
            if (!url.contains("/api/")) {
                if (url.endsWith("/")) {
                    url += "api/"
                } else {
                    url += "/api/"
                }
            }
            if (!url.endsWith("/")) {
                url += "/"
            }
            return url
        }
    }
}
