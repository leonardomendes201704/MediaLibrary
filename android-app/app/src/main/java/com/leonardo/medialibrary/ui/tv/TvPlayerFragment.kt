package com.leonardo.medialibrary.ui.tv

import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.KeyEvent
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import androidx.fragment.app.Fragment
import androidx.fragment.app.activityViewModels
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.exoplayer.ExoPlayer
import com.leonardo.medialibrary.databinding.FragmentTvPlayerBinding
import com.leonardo.medialibrary.ui.common.FeedUiState
import com.leonardo.medialibrary.ui.common.FeedViewModel
import com.leonardo.medialibrary.ui.common.FeedViewModelFactory
import com.leonardo.medialibrary.ui.common.HostActions
import com.leonardo.medialibrary.ui.common.PlayerProvider
import com.leonardo.medialibrary.ui.common.ServiceLocator
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

class TvPlayerFragment : Fragment(), Player.Listener {
    private var _binding: FragmentTvPlayerBinding? = null
    private val binding get() = _binding!!

    private val viewModel: FeedViewModel by activityViewModels {
        FeedViewModelFactory(ServiceLocator.mediaRepository)
    }

    private lateinit var player: ExoPlayer
    private var lastPlaylistIds: List<Int> = emptyList()
    private val watchedReportedIds = mutableSetOf<Int>()
    private val watchedProbeHandler = Handler(Looper.getMainLooper())
    private val watchedProbeRunnable = object : Runnable {
        override fun run() {
            maybeMarkCurrentAsWatched(force = false)
            watchedProbeHandler.postDelayed(this, WATCHED_PROBE_INTERVAL_MS)
        }
    }

    override fun onCreateView(
        inflater: LayoutInflater,
        container: ViewGroup?,
        savedInstanceState: Bundle?,
    ): View {
        _binding = FragmentTvPlayerBinding.inflate(inflater, container, false)
        return binding.root
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        player = PlayerProvider.get(requireContext())
        player.addListener(this)
        binding.tvPlayerView.player = player
        binding.tvDeleteButton.setOnClickListener {
            viewModel.deleteCurrentVideoPermanently()
            binding.tvRoot.requestFocus()
        }

        binding.tvRoot.requestFocus()
        binding.tvRoot.setOnKeyListener { _, keyCode, event ->
            if (event.action != KeyEvent.ACTION_DOWN) return@setOnKeyListener false
            when (keyCode) {
                KeyEvent.KEYCODE_DPAD_DOWN -> {
                    viewModel.moveNext()
                    true
                }
                KeyEvent.KEYCODE_DPAD_UP -> {
                    viewModel.movePrevious()
                    true
                }
                KeyEvent.KEYCODE_DPAD_CENTER,
                KeyEvent.KEYCODE_ENTER -> {
                    player.playWhenReady = !player.playWhenReady
                    true
                }
                KeyEvent.KEYCODE_MENU -> {
                    (activity as? HostActions)?.openServerConfig()
                    true
                }
                KeyEvent.KEYCODE_DEL,
                KeyEvent.KEYCODE_FORWARD_DEL -> {
                    viewModel.deleteCurrentVideoPermanently()
                    true
                }
                else -> false
            }
        }

        viewLifecycleOwner.lifecycleScope.launch {
            viewLifecycleOwner.repeatOnLifecycle(Lifecycle.State.STARTED) {
                viewModel.uiState.collect { state ->
                    renderState(state)
                }
            }
        }
    }

    override fun onStart() {
        super.onStart()
        player.playWhenReady = true
        watchedProbeHandler.post(watchedProbeRunnable)
    }

    override fun onStop() {
        super.onStop()
        viewModel.persistPlayback(index = player.currentMediaItemIndex, positionMs = player.currentPosition)
        watchedProbeHandler.removeCallbacks(watchedProbeRunnable)
        player.pause()
    }

    override fun onDestroyView() {
        watchedProbeHandler.removeCallbacks(watchedProbeRunnable)
        player.removeListener(this)
        binding.tvPlayerView.player = null
        _binding = null
        super.onDestroyView()
    }

    override fun onMediaItemTransition(mediaItem: MediaItem?, reason: Int) {
        val index = player.currentMediaItemIndex
        if (index >= 0) {
            viewModel.onPlayerIndexChanged(index)
            viewModel.persistPlayback(index = index, positionMs = player.currentPosition)
        }
    }

    override fun onPlaybackStateChanged(playbackState: Int) {
        if (playbackState == Player.STATE_ENDED) {
            maybeMarkCurrentAsWatched(force = true)
            viewModel.moveNext()
        }
    }

    override fun onPlayerError(error: PlaybackException) {
        viewModel.onPlaybackError("Erro de reproducao: ${error.errorCodeName}")
        viewLifecycleOwner.lifecycleScope.launch {
            delay(1000)
            if (player.currentMediaItemIndex >= 0) {
                player.seekToDefaultPosition(player.currentMediaItemIndex)
                player.prepare()
                player.playWhenReady = true
            }
        }
    }

    private fun renderState(state: FeedUiState) {
        if (state.isLoading) {
            binding.tvStatusText.text = "Carregando feed..."
            binding.tvFavoriteIcon.visibility = View.GONE
            renderBadges(category = null, dateValue = null)
            return
        }
        if (state.errorMessage != null && state.videos.isEmpty()) {
            binding.tvStatusText.text = state.errorMessage
            binding.tvFavoriteIcon.visibility = View.GONE
            renderBadges(category = null, dateValue = null)
            return
        }
        if (state.videos.isEmpty()) {
            binding.tvStatusText.text = "Nenhum video pronto no feed."
            binding.tvFavoriteIcon.visibility = View.GONE
            renderBadges(category = null, dateValue = null)
            watchedReportedIds.clear()
            return
        }

        syncPlaylist(state)
        val current = state.currentIndex + 1
        val total = state.videos.size
        binding.tvStatusText.text = "Video $current/$total"

        val currentVideo = state.videos[state.currentIndex.coerceIn(0, state.videos.lastIndex)]
        binding.tvFavoriteIcon.visibility = if (currentVideo.isFavorite) View.VISIBLE else View.GONE
        renderBadges(category = currentVideo.pathCategory, dateValue = currentVideo.pathDate)
    }

    private fun syncPlaylist(state: FeedUiState) {
        val urls = state.videos.map { it.streamUrl }
        val ids = state.videos.map { it.id }
        if (urls.isEmpty()) return

        if (ids != lastPlaylistIds) {
            watchedReportedIds.retainAll(ids.toSet())
            val mediaItems = urls.mapIndexed { idx, url ->
                MediaItem.Builder()
                    .setMediaId(ids[idx].toString())
                    .setUri(url)
                    .build()
            }
            val startIndex = state.currentIndex.coerceIn(0, mediaItems.lastIndex)
            val startPosition = state.resumePositionMs.coerceAtLeast(0L)
            player.setMediaItems(mediaItems, startIndex, startPosition)
            player.prepare()
            player.playWhenReady = true
            lastPlaylistIds = ids
            if (state.resumePositionMs > 0L) {
                viewModel.consumeResumePosition()
            }
            return
        }

        if (player.currentMediaItemIndex != state.currentIndex) {
            player.seekToDefaultPosition(state.currentIndex)
            player.playWhenReady = true
        }
    }

    private fun maybeMarkCurrentAsWatched(force: Boolean) {
        val state = viewModel.uiState.value
        if (state.videos.isEmpty()) return

        val playerIndex = player.currentMediaItemIndex
        val index = if (playerIndex in state.videos.indices) playerIndex else state.currentIndex
        if (index !in state.videos.indices) return

        if (index != state.currentIndex) {
            viewModel.onPlayerIndexChanged(index)
        }

        val currentVideo = state.videos[index]
        if (currentVideo.isWatched || watchedReportedIds.contains(currentVideo.id)) {
            return
        }

        val ratio = if (force) {
            1f
        } else {
            val duration = player.duration
            val position = player.currentPosition
            if (duration <= 0L || position <= 0L) {
                return
            }
            (position.toFloat() / duration.toFloat()).coerceIn(0f, 1f)
        }

        if (force || ratio >= WATCHED_THRESHOLD) {
            watchedReportedIds.add(currentVideo.id)
            viewModel.markCurrentAsWatched(watchedRatio = ratio)
        }
    }

    private fun renderBadges(category: String?, dateValue: String?) {
        val categoryText = category?.trim().orEmpty()
        val dateText = dateValue?.trim().orEmpty()

        if (categoryText.isNotBlank()) {
            binding.tvCategoryBadge.text = categoryText
            binding.tvCategoryBadge.visibility = View.VISIBLE
        } else {
            binding.tvCategoryBadge.visibility = View.GONE
        }

        if (dateText.isNotBlank()) {
            binding.tvDateBadge.text = dateText
            binding.tvDateBadge.visibility = View.VISIBLE
        } else {
            binding.tvDateBadge.visibility = View.GONE
        }
    }

    companion object {
        private const val WATCHED_THRESHOLD = 0.10f
        private const val WATCHED_PROBE_INTERVAL_MS = 1_000L
    }
}
