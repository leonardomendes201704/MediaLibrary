package com.leonardo.medialibrary.domain.model

data class PlaybackResumeState(
    val videoId: Int,
    val positionMs: Long,
)

