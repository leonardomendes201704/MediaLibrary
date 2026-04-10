package com.leonardo.medialibrary.ui.common

import android.content.Context
import androidx.media3.common.C
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.cache.CacheDataSource
import androidx.media3.datasource.cache.LeastRecentlyUsedCacheEvictor
import androidx.media3.datasource.cache.SimpleCache
import androidx.media3.database.StandaloneDatabaseProvider
import androidx.media3.datasource.okhttp.OkHttpDataSource
import androidx.media3.exoplayer.DefaultLoadControl
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import okhttp3.OkHttpClient
import java.io.File

@UnstableApi
object PlayerProvider {
    private const val CACHE_SIZE_BYTES = 512L * 1024L * 1024L
    private const val MIN_BUFFER_MS = 2_000
    private const val MAX_BUFFER_MS = 10_000
    private const val BUFFER_FOR_PLAYBACK_MS = 500
    private const val BUFFER_FOR_PLAYBACK_AFTER_REBUFFER_MS = 1_000

    private var primaryPlayer: ExoPlayer? = null
    private val managedPlayers = linkedSetOf<ExoPlayer>()
    private var mediaCache: SimpleCache? = null
    private var httpClient: OkHttpClient? = null

    @Synchronized
    fun get(context: Context): ExoPlayer {
        primaryPlayer?.let { return it }
        val newPlayer = create(context)
        primaryPlayer = newPlayer
        return newPlayer
    }

    @Synchronized
    fun create(context: Context): ExoPlayer {
        val appContext = context.applicationContext
        ensureSharedDependencies(appContext)
        val mediaSourceFactory = DefaultMediaSourceFactory(cacheDataSourceFactory())
        val loadControl = DefaultLoadControl.Builder()
            .setBufferDurationsMs(
                MIN_BUFFER_MS,
                MAX_BUFFER_MS,
                BUFFER_FOR_PLAYBACK_MS,
                BUFFER_FOR_PLAYBACK_AFTER_REBUFFER_MS,
            )
            .build()

        return ExoPlayer.Builder(appContext)
            .setLoadControl(loadControl)
            .setMediaSourceFactory(mediaSourceFactory)
            .build()
            .apply {
                playWhenReady = true
                repeatMode = ExoPlayer.REPEAT_MODE_OFF
                setVideoScalingMode(C.VIDEO_SCALING_MODE_SCALE_TO_FIT_WITH_CROPPING)
                managedPlayers.add(this)
            }
    }

    @Synchronized
    fun release(player: ExoPlayer) {
        managedPlayers.remove(player)
        if (primaryPlayer === player) {
            primaryPlayer = null
        }
        player.release()
    }

    @Synchronized
    fun release() {
        managedPlayers.toList().forEach { it.release() }
        managedPlayers.clear()
        primaryPlayer = null
        mediaCache?.release()
        mediaCache = null
        httpClient?.dispatcher?.executorService?.shutdown()
        httpClient?.connectionPool?.evictAll()
        httpClient = null
    }

    private fun ensureSharedDependencies(appContext: Context) {
        if (mediaCache != null && httpClient != null) {
            return
        }
        val cacheDirectory = File(appContext.cacheDir, "video_cache")
        val evictor = LeastRecentlyUsedCacheEvictor(CACHE_SIZE_BYTES)
        val databaseProvider = StandaloneDatabaseProvider(appContext)
        mediaCache = SimpleCache(cacheDirectory, evictor, databaseProvider)

        httpClient = OkHttpClient.Builder()
            .retryOnConnectionFailure(true)
            .build()
    }

    private fun cacheDataSourceFactory(): CacheDataSource.Factory {
        val upstream = OkHttpDataSource.Factory(httpClient!!)
        return CacheDataSource.Factory()
            .setCache(mediaCache!!)
            .setUpstreamDataSourceFactory(upstream)
            .setFlags(CacheDataSource.FLAG_IGNORE_CACHE_ON_ERROR)
    }
}
