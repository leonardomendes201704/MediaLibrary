package com.leonardo.medialibrary.domain.model

data class VideoItem(
    val id: Int,
    val streamUrl: String,
    val thumbnailUrl: String?,
    val durationSeconds: Double?,
    val width: Int?,
    val height: Int?,
    val status: String,
    val isFavorite: Boolean,
    val isWatched: Boolean,
    val pathCategory: String?,
    val pathDate: String?,
)
