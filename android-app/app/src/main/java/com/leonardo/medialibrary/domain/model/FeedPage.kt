package com.leonardo.medialibrary.domain.model

data class FeedPage(
    val items: List<VideoItem>,
    val nextCursor: String?,
)

