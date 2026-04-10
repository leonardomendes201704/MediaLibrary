package com.leonardo.medialibrary.ui.mobile

import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.GestureDetector
import android.view.LayoutInflater
import android.view.MotionEvent
import android.view.View
import android.view.ViewGroup
import androidx.core.view.GestureDetectorCompat
import androidx.fragment.app.Fragment
import androidx.fragment.app.activityViewModels
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.PlaybackParameters
import androidx.media3.common.Player
import androidx.media3.exoplayer.ExoPlayer
import com.leonardo.medialibrary.R
import com.leonardo.medialibrary.databinding.FragmentMobileFeedBinding
import com.leonardo.medialibrary.domain.model.VideoItem
import com.leonardo.medialibrary.ui.common.FeedUiState
import com.leonardo.medialibrary.ui.common.FeedViewModel
import com.leonardo.medialibrary.ui.common.FeedViewModelFactory
import com.leonardo.medialibrary.ui.common.HostActions
import com.leonardo.medialibrary.ui.common.PlayerProvider
import com.leonardo.medialibrary.ui.common.ServiceLocator
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlin.math.abs

class MobileFeedFragment : Fragment() {
    private var _binding: FragmentMobileFeedBinding? = null
    private val binding get() = _binding!!

    private val viewModel: FeedViewModel by activityViewModels {
        FeedViewModelFactory(ServiceLocator.mediaRepository)
    }

    private lateinit var playerA: ExoPlayer
    private lateinit var playerB: ExoPlayer
    private val listenerA = SlotPlayerListener(PlayerSlot.A)
    private val listenerB = SlotPlayerListener(PlayerSlot.B)

    private lateinit var gestureDetector: GestureDetectorCompat
    private var holdSpeedEnabled = false

    private var activeSlot: PlayerSlot = PlayerSlot.A
    private var activePlaylistIndex: Int = -1
    private var standbyStartIndex: Int = -1
    private var currentVideos: List<VideoItem> = emptyList()
    private var lastPlaylistIds: List<Int> = emptyList()
    private val watchedReportedIds = mutableSetOf<Int>()

    private val playbackProbeHandler = Handler(Looper.getMainLooper())
    private val playbackProbeRunnable = object : Runnable {
        override fun run() {
            maybeMarkCurrentAsWatched(force = false)
            playbackProbeHandler.postDelayed(this, WATCHED_PROBE_INTERVAL_MS)
        }
    }

    override fun onCreateView(
        inflater: LayoutInflater,
        container: ViewGroup?,
        savedInstanceState: Bundle?,
    ): View {
        _binding = FragmentMobileFeedBinding.inflate(inflater, container, false)
        return binding.root
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        playerA = PlayerProvider.create(requireContext())
        playerB = PlayerProvider.create(requireContext())
        playerA.addListener(listenerA)
        playerB.addListener(listenerB)
        binding.mobilePlayerViewA.player = playerA
        binding.mobilePlayerViewB.player = playerB
        updateVisiblePlayerSurface(animated = false)

        gestureDetector = GestureDetectorCompat(requireContext(), object : GestureDetector.SimpleOnGestureListener() {
            override fun onDown(e: MotionEvent): Boolean = true

            override fun onSingleTapConfirmed(e: MotionEvent): Boolean {
                val player = activePlayer()
                player.playWhenReady = !player.playWhenReady
                return true
            }

            override fun onDoubleTap(e: MotionEvent): Boolean {
                viewModel.toggleFavoriteCurrent()
                return true
            }

            override fun onLongPress(e: MotionEvent) {
                setHoldSpeedEnabled(true)
            }

            override fun onFling(
                e1: MotionEvent?,
                e2: MotionEvent,
                velocityX: Float,
                velocityY: Float,
            ): Boolean {
                if (e1 == null) return false
                val dy = e1.y - e2.y
                val dx = e1.x - e2.x
                val isVertical = abs(dy) > abs(dx)
                if (!isVertical || abs(dy) < 120 || abs(velocityY) < 400) {
                    return false
                }
                if (dy > 0) {
                    viewModel.moveNext()
                } else {
                    viewModel.movePrevious()
                }
                return true
            }
        })

        binding.mobileRoot.setOnTouchListener { _, motionEvent ->
            val handled = gestureDetector.onTouchEvent(motionEvent)
            when (motionEvent.actionMasked) {
                MotionEvent.ACTION_UP,
                MotionEvent.ACTION_CANCEL -> setHoldSpeedEnabled(false)
            }
            handled
        }
        binding.mobileSettingsButton.setOnClickListener {
            (activity as? HostActions)?.openServerConfig()
        }
        binding.mobileDeleteButton.setOnClickListener {
            viewModel.deleteCurrentVideoPermanently()
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
        activePlayer().playWhenReady = true
        playbackProbeHandler.post(playbackProbeRunnable)
    }

    override fun onStop() {
        super.onStop()
        if (activePlaylistIndex >= 0) {
            viewModel.persistPlayback(index = activePlaylistIndex, positionMs = activePlayer().currentPosition)
        }
        playbackProbeHandler.removeCallbacks(playbackProbeRunnable)
        setHoldSpeedEnabled(false)
        playerA.pause()
        playerB.pause()
    }

    override fun onDestroyView() {
        playbackProbeHandler.removeCallbacks(playbackProbeRunnable)
        playerA.removeListener(listenerA)
        playerB.removeListener(listenerB)
        binding.mobilePlayerViewA.player = null
        binding.mobilePlayerViewB.player = null
        PlayerProvider.release(playerA)
        PlayerProvider.release(playerB)
        _binding = null
        super.onDestroyView()
    }

    private fun renderState(state: FeedUiState) {
        if (state.isLoading) {
            binding.mobileStatusText.text = "Carregando feed..."
            binding.mobileFavoriteIcon.visibility = View.GONE
            renderBadges(category = null, dateValue = null)
            return
        }
        if (state.errorMessage != null && state.videos.isEmpty()) {
            binding.mobileStatusText.text = "${state.errorMessage} - ${getString(R.string.tap_to_retry)}"
            binding.mobileRoot.setOnClickListener { viewModel.refresh() }
            binding.mobileFavoriteIcon.visibility = View.GONE
            renderBadges(category = null, dateValue = null)
            return
        }
        binding.mobileRoot.setOnClickListener(null)

        if (state.videos.isEmpty()) {
            binding.mobileStatusText.text = "Nenhum video disponivel."
            binding.mobileFavoriteIcon.visibility = View.GONE
            renderBadges(category = null, dateValue = null)
            clearPlayers()
            return
        }

        syncPlayers(state)
        val current = state.currentIndex + 1
        val total = state.videos.size
        val loadingMore = if (state.isLoadingMore) " - pre-carregando..." else ""
        binding.mobileStatusText.text = "Video $current/$total$loadingMore"

        val currentVideo = state.videos[state.currentIndex.coerceIn(0, state.videos.lastIndex)]
        binding.mobileFavoriteIcon.visibility = if (currentVideo.isFavorite) View.VISIBLE else View.GONE
        renderBadges(category = currentVideo.pathCategory, dateValue = currentVideo.pathDate)
    }

    private fun syncPlayers(state: FeedUiState) {
        currentVideos = state.videos
        val ids = currentVideos.map { it.id }
        val playlistChanged = ids != lastPlaylistIds
        if (playlistChanged) {
            lastPlaylistIds = ids
            watchedReportedIds.retainAll(ids.toSet())
            activePlaylistIndex = -1
            standbyStartIndex = -1
        }

        if (currentVideos.isEmpty()) {
            clearPlayers()
            return
        }

        val targetIndex = state.currentIndex.coerceIn(0, currentVideos.lastIndex)
        if (activePlaylistIndex == -1) {
            prepareActiveForIndex(targetIndex, state.resumePositionMs)
            prepareStandbyForIndex(targetIndex + 1)
            if (state.resumePositionMs > 0L) {
                viewModel.consumeResumePosition()
            }
            return
        }

        if (targetIndex == standbyStartIndex && standbyPlayer().mediaItemCount > 0) {
            swapToStandby(targetIndex)
            prepareStandbyForIndex(targetIndex + 1)
            return
        }

        if (targetIndex != activePlaylistIndex) {
            prepareActiveForIndex(targetIndex, 0L)
            prepareStandbyForIndex(targetIndex + 1)
            return
        }

        if (activePlayer().playbackState == Player.STATE_IDLE) {
            prepareActiveForIndex(targetIndex, 0L)
            prepareStandbyForIndex(targetIndex + 1)
        } else {
            activePlayer().playWhenReady = true
            applyPlaybackSpeed(activePlayer())
        }
    }

    private fun prepareActiveForIndex(index: Int, startPositionMs: Long) {
        if (index !in currentVideos.indices) {
            return
        }
        val player = activePlayer()
        val item = toMediaItem(currentVideos[index])
        player.setMediaItem(item, startPositionMs.coerceAtLeast(0L))
        player.prepare()
        player.playWhenReady = true
        applyPlaybackSpeed(player)
        activePlaylistIndex = index
        updateVisiblePlayerSurface(animated = false)
        maybeMarkCurrentAsWatched(force = false)
    }

    private fun prepareStandbyForIndex(startIndex: Int) {
        val standby = standbyPlayer()
        if (startIndex !in currentVideos.indices) {
            standby.stop()
            standby.clearMediaItems()
            standbyStartIndex = -1
            return
        }

        val preloadItems = buildList {
            for (idx in startIndex..(startIndex + PRELOAD_NEXT_COUNT - 1)) {
                if (idx in currentVideos.indices) {
                    add(toMediaItem(currentVideos[idx]))
                }
            }
        }
        if (preloadItems.isEmpty()) {
            standby.stop()
            standby.clearMediaItems()
            standbyStartIndex = -1
            return
        }

        standby.setMediaItems(preloadItems, 0, 0L)
        standby.prepare()
        standby.playWhenReady = false
        standby.playbackParameters = PlaybackParameters(1f)
        standbyStartIndex = startIndex
    }

    private fun swapToStandby(newIndex: Int) {
        val oldActive = activePlayer()
        oldActive.playWhenReady = false

        activeSlot = activeSlot.swap()
        activePlaylistIndex = newIndex
        standbyStartIndex = -1

        val newActive = activePlayer()
        newActive.seekToDefaultPosition(0)
        newActive.playWhenReady = true
        applyPlaybackSpeed(newActive)
        updateVisiblePlayerSurface(animated = true)
        maybeMarkCurrentAsWatched(force = false)
    }

    private fun updateVisiblePlayerSurface(animated: Boolean) {
        val activeView = if (activeSlot == PlayerSlot.A) binding.mobilePlayerViewA else binding.mobilePlayerViewB
        val inactiveView = if (activeSlot == PlayerSlot.A) binding.mobilePlayerViewB else binding.mobilePlayerViewA

        if (!animated) {
            activeView.visibility = View.VISIBLE
            activeView.alpha = 1f
            inactiveView.visibility = View.INVISIBLE
            inactiveView.alpha = 1f
            return
        }

        activeView.visibility = View.VISIBLE
        activeView.alpha = 0f
        activeView.animate().alpha(1f).setDuration(SWAP_ANIMATION_MS).start()
        inactiveView.animate()
            .alpha(0f)
            .setDuration(SWAP_ANIMATION_MS)
            .withEndAction {
                inactiveView.visibility = View.INVISIBLE
                inactiveView.alpha = 1f
            }
            .start()
    }

    private fun toMediaItem(video: VideoItem): MediaItem {
        return MediaItem.Builder()
            .setMediaId(video.id.toString())
            .setUri(video.streamUrl)
            .build()
    }

    private fun clearPlayers() {
        playerA.stop()
        playerA.clearMediaItems()
        playerB.stop()
        playerB.clearMediaItems()
        activePlaylistIndex = -1
        standbyStartIndex = -1
        currentVideos = emptyList()
        lastPlaylistIds = emptyList()
    }

    private fun renderBadges(category: String?, dateValue: String?) {
        val categoryText = category?.trim().orEmpty()
        val dateText = dateValue?.trim().orEmpty()

        if (categoryText.isNotBlank()) {
            binding.mobileCategoryBadge.text = categoryText
            binding.mobileCategoryBadge.visibility = View.VISIBLE
        } else {
            binding.mobileCategoryBadge.visibility = View.GONE
        }

        if (dateText.isNotBlank()) {
            binding.mobileDateBadge.text = dateText
            binding.mobileDateBadge.visibility = View.VISIBLE
        } else {
            binding.mobileDateBadge.visibility = View.GONE
        }
    }

    private fun maybeMarkCurrentAsWatched(force: Boolean) {
        val index = activePlaylistIndex
        if (index !in currentVideos.indices) return

        val currentVideo = currentVideos[index]
        if (currentVideo.isWatched || watchedReportedIds.contains(currentVideo.id)) {
            return
        }

        val ratio = if (force) {
            1f
        } else {
            val duration = activePlayer().duration
            val position = activePlayer().currentPosition
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

    private fun setHoldSpeedEnabled(enabled: Boolean) {
        if (enabled == holdSpeedEnabled) return
        holdSpeedEnabled = enabled
        applyPlaybackSpeed(activePlayer())
    }

    private fun applyPlaybackSpeed(player: ExoPlayer) {
        player.playbackParameters = PlaybackParameters(if (holdSpeedEnabled) 4f else 1f)
    }

    private fun activePlayer(): ExoPlayer = if (activeSlot == PlayerSlot.A) playerA else playerB

    private fun standbyPlayer(): ExoPlayer = if (activeSlot == PlayerSlot.A) playerB else playerA

    private enum class PlayerSlot {
        A,
        B,
        ;

        fun swap(): PlayerSlot = if (this == A) B else A
    }

    private inner class SlotPlayerListener(
        private val slot: PlayerSlot,
    ) : Player.Listener {
        override fun onPlaybackStateChanged(playbackState: Int) {
            if (slot != activeSlot) return
            if (playbackState == Player.STATE_ENDED) {
                maybeMarkCurrentAsWatched(force = true)
                viewModel.moveNext()
            }
        }

        override fun onPlayerError(error: PlaybackException) {
            if (slot != activeSlot) return
            viewModel.onPlaybackError("Erro de rede/reproducao: ${error.errorCodeName}")
            viewLifecycleOwner.lifecycleScope.launch {
                delay(800)
                val player = activePlayer()
                if (player.currentMediaItem != null) {
                    player.seekToDefaultPosition(0)
                    player.prepare()
                    player.playWhenReady = true
                    applyPlaybackSpeed(player)
                }
            }
        }
    }

    companion object {
        private const val WATCHED_THRESHOLD = 0.10f
        private const val WATCHED_PROBE_INTERVAL_MS = 750L
        private const val PRELOAD_NEXT_COUNT = 2
        private const val SWAP_ANIMATION_MS = 120L
    }
}
