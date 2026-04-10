package com.leonardo.medialibrary.ui.common

import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import com.leonardo.medialibrary.domain.model.FeedMode
import com.leonardo.medialibrary.domain.model.VideoItem
import com.leonardo.medialibrary.domain.repository.MediaRepository
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

data class FeedUiState(
    val backendUrl: String = "",
    val videos: List<VideoItem> = emptyList(),
    val feedMode: FeedMode = FeedMode.NORMAL,
    val currentIndex: Int = 0,
    val resumePositionMs: Long = 0L,
    val isLoading: Boolean = false,
    val isLoadingMore: Boolean = false,
    val errorMessage: String? = null,
)

class FeedViewModel(
    private val repository: MediaRepository,
) : ViewModel() {
    private val _uiState = MutableStateFlow(FeedUiState())
    val uiState: StateFlow<FeedUiState> = _uiState.asStateFlow()

    private var nextCursor: String? = null
    private val loadMutex = Mutex()

    init {
        refresh()
    }

    fun refresh() {
        viewModelScope.launch {
            _uiState.update { it.copy(isLoading = true, errorMessage = null) }
            runCatching {
                val backendUrl = repository.getBackendUrl()
                if (backendUrl.isBlank()) {
                    error("Backend nao configurado")
                }
                val feedMode = repository.getActiveFeedMode()
                val resume = repository.getPlaybackResumeState()
                val collectedItems = mutableListOf<VideoItem>()
                var next: String? = null
                var pagesLoaded = 0

                do {
                    val cursor = if (pagesLoaded == 0) null else next
                    val page = repository.loadFeed(cursor = cursor, pageSize = 20)
                    collectedItems.addAll(page.items)
                    next = page.nextCursor
                    pagesLoaded += 1
                    val found = resume?.let { state -> collectedItems.any { it.id == state.videoId } } ?: true
                    if (found) {
                        break
                    }
                } while (next != null && pagesLoaded < 3)

                val videos = collectedItems.distinctBy { it.id }
                var targetIndex = 0
                var targetPositionMs = 0L
                if (resume != null && videos.isNotEmpty()) {
                    val idx = videos.indexOfFirst { it.id == resume.videoId }
                    if (idx >= 0) {
                        targetIndex = idx
                        targetPositionMs = resume.positionMs
                    }
                }

                nextCursor = next
                _uiState.update {
                    it.copy(
                        backendUrl = backendUrl,
                        videos = videos,
                        feedMode = feedMode,
                        currentIndex = targetIndex,
                        resumePositionMs = targetPositionMs,
                        isLoading = false,
                        errorMessage = if (videos.isEmpty()) "Nenhum video pronto no feed." else null,
                    )
                }
            }.onFailure { err ->
                _uiState.update {
                    it.copy(
                        isLoading = false,
                        errorMessage = err.message ?: "Falha ao carregar feed",
                    )
                }
            }
        }
    }

    fun moveNext() {
        viewModelScope.launch {
            val state = _uiState.value
            if (state.videos.isEmpty()) return@launch

            val lastIndex = state.videos.lastIndex
            if (state.currentIndex < lastIndex) {
                setIndex(state.currentIndex + 1)
            } else if (nextCursor != null) {
                loadMoreIfNeeded(force = true)
                val newState = _uiState.value
                if (newState.currentIndex < newState.videos.lastIndex) {
                    setIndex(newState.currentIndex + 1)
                }
            }
            loadMoreIfNeeded(force = false)
        }
    }

    fun movePrevious() {
        val state = _uiState.value
        if (state.currentIndex > 0) {
            setIndex(state.currentIndex - 1)
        }
    }

    fun setIndex(index: Int) {
        val state = _uiState.value
        if (state.videos.isEmpty()) return
        val bounded = index.coerceIn(0, state.videos.lastIndex)
        _uiState.update { it.copy(currentIndex = bounded, resumePositionMs = 0L, errorMessage = null) }
        viewModelScope.launch {
            loadMoreIfNeeded(force = false)
        }
    }

    fun onPlayerIndexChanged(playerIndex: Int) {
        if (playerIndex < 0) return
        val state = _uiState.value
        if (playerIndex <= state.videos.lastIndex && playerIndex != state.currentIndex) {
            setIndex(playerIndex)
        }
    }

    fun onPlaybackError(message: String) {
        _uiState.update { it.copy(errorMessage = message) }
    }

    fun persistPlayback(index: Int, positionMs: Long) {
        val state = _uiState.value
        if (index < 0 || index > state.videos.lastIndex) return
        val videoId = state.videos[index].id
        viewModelScope.launch {
            repository.savePlaybackResumeState(videoId = videoId, positionMs = positionMs)
        }
    }

    fun consumeResumePosition() {
        val state = _uiState.value
        if (state.resumePositionMs == 0L) return
        _uiState.update { it.copy(resumePositionMs = 0L) }
    }

    fun toggleFavoriteCurrent() {
        val state = _uiState.value
        if (state.videos.isEmpty()) return
        val currentIndex = state.currentIndex.coerceIn(0, state.videos.lastIndex)
        val currentVideo = state.videos[currentIndex]
        val desiredFavorite = !currentVideo.isFavorite
        updateFavoriteByVideoId(videoId = currentVideo.id, isFavorite = desiredFavorite)

        viewModelScope.launch {
            runCatching {
                repository.setVideoFavorite(videoId = currentVideo.id, isFavorite = desiredFavorite)
            }.onSuccess { confirmed ->
                updateFavoriteByVideoId(videoId = currentVideo.id, isFavorite = confirmed)
            }.onFailure { err ->
                updateFavoriteByVideoId(videoId = currentVideo.id, isFavorite = currentVideo.isFavorite)
                _uiState.update {
                    it.copy(errorMessage = err.message ?: "Falha ao favoritar video")
                }
            }
        }
    }

    fun deleteCurrentVideoPermanently() {
        val state = _uiState.value
        if (state.videos.isEmpty()) return
        val currentIndex = state.currentIndex.coerceIn(0, state.videos.lastIndex)
        val currentVideo = state.videos[currentIndex]

        viewModelScope.launch {
            runCatching {
                val deleted = repository.deleteVideoPermanently(videoId = currentVideo.id)
                if (!deleted) {
                    throw IllegalStateException("Falha ao remover video")
                }
            }.onSuccess {
                removeVideoById(currentVideo.id)
                if (_uiState.value.videos.isEmpty()) {
                    refresh()
                } else {
                    loadMoreIfNeeded(force = false)
                }
            }.onFailure { err ->
                _uiState.update {
                    it.copy(errorMessage = err.message ?: "Falha ao remover video")
                }
            }
        }
    }

    fun markCurrentAsWatched(watchedRatio: Float? = null) {
        val state = _uiState.value
        if (state.videos.isEmpty()) return
        val currentIndex = state.currentIndex.coerceIn(0, state.videos.lastIndex)
        val currentVideo = state.videos[currentIndex]
        if (currentVideo.isWatched) return
        updateWatchedByVideoId(videoId = currentVideo.id, isWatched = true)

        viewModelScope.launch {
            runCatching {
                repository.setVideoWatched(
                    videoId = currentVideo.id,
                    isWatched = true,
                    watchedRatio = watchedRatio,
                )
            }.onSuccess { confirmed ->
                updateWatchedByVideoId(videoId = currentVideo.id, isWatched = confirmed)
                val shouldRemoveFromCurrentFeed = when (_uiState.value.feedMode) {
                    FeedMode.NORMAL, FeedMode.CATEGORIES -> confirmed
                    FeedMode.FAVORITES_ONLY, FeedMode.NEWEST -> false
                }
                if (shouldRemoveFromCurrentFeed) {
                    removeVideoById(currentVideo.id)
                    if (_uiState.value.videos.isEmpty()) {
                        refresh()
                    } else {
                        loadMoreIfNeeded(force = false)
                    }
                }
            }.onFailure { err ->
                updateWatchedByVideoId(videoId = currentVideo.id, isWatched = false)
                _uiState.update {
                    it.copy(errorMessage = err.message ?: "Falha ao marcar video como visto")
                }
            }
        }
    }

    private fun updateFavoriteByVideoId(videoId: Int, isFavorite: Boolean) {
        _uiState.update { state ->
            state.copy(
                videos = state.videos.map { video ->
                    if (video.id == videoId) {
                        video.copy(isFavorite = isFavorite)
                    } else {
                        video
                    }
                }
            )
        }
    }

    private fun updateWatchedByVideoId(videoId: Int, isWatched: Boolean) {
        _uiState.update { state ->
            state.copy(
                videos = state.videos.map { video ->
                    if (video.id == videoId) {
                        video.copy(isWatched = isWatched)
                    } else {
                        video
                    }
                }
            )
        }
    }

    private fun removeVideoById(videoId: Int) {
        _uiState.update { state ->
            val oldVideos = state.videos
            val oldIndex = state.currentIndex.coerceIn(0, (oldVideos.lastIndex).coerceAtLeast(0))
            val removeAt = oldVideos.indexOfFirst { it.id == videoId }
            if (removeAt < 0) {
                return@update state
            }
            val newVideos = oldVideos.filterNot { it.id == videoId }
            if (newVideos.isEmpty()) {
                return@update state.copy(videos = emptyList(), currentIndex = 0, resumePositionMs = 0L)
            }
            val newIndex = when {
                oldIndex < removeAt -> oldIndex
                oldIndex > removeAt -> (oldIndex - 1).coerceIn(0, newVideos.lastIndex)
                else -> oldIndex.coerceIn(0, newVideos.lastIndex)
            }
            state.copy(videos = newVideos, currentIndex = newIndex, resumePositionMs = 0L)
        }
    }

    private suspend fun loadMoreIfNeeded(force: Boolean) {
        loadMutex.withLock {
            val current = _uiState.value
            val shouldLoad = force || current.currentIndex >= (current.videos.size - 4).coerceAtLeast(0)
            if (!shouldLoad || nextCursor == null || current.isLoadingMore) {
                return
            }

            _uiState.update { it.copy(isLoadingMore = true) }
            runCatching {
                repository.loadFeed(cursor = nextCursor, pageSize = 20)
            }.onSuccess { page ->
                nextCursor = page.nextCursor
                val merged = (current.videos + page.items).distinctBy { it.id }
                _uiState.update {
                    it.copy(
                        videos = merged,
                        isLoadingMore = false,
                    )
                }
            }.onFailure { err ->
                _uiState.update {
                    it.copy(
                        isLoadingMore = false,
                        errorMessage = err.message ?: "Falha ao carregar mais videos",
                    )
                }
            }
        }
    }
}

class FeedViewModelFactory(
    private val repository: MediaRepository,
) : ViewModelProvider.Factory {
    @Suppress("UNCHECKED_CAST")
    override fun <T : ViewModel> create(modelClass: Class<T>): T {
        return FeedViewModel(repository) as T
    }
}
