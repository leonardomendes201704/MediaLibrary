package com.leonardo.medialibrary.data.remote

import com.squareup.moshi.Json
import retrofit2.http.DELETE
import retrofit2.http.Body
import retrofit2.http.GET
import retrofit2.http.PUT
import retrofit2.http.Path
import retrofit2.http.Query

interface BackendApi {
    @GET("health")
    suspend fun health(): HealthDto

    @GET("feed")
    suspend fun feed(
        @Query("cursor") cursor: String?,
        @Query("page_size") pageSize: Int,
        @Query("random_order") randomOrder: Boolean,
        @Query("exclude_favorites") excludeFavorites: Boolean,
        @Query("only_favorites") onlyFavorites: Boolean,
        @Query("exclude_watched") excludeWatched: Boolean,
        @Query("only_watched") onlyWatched: Boolean,
        @Query("order_by") orderBy: String?,
        @Query("category") category: String?,
    ): FeedResponseDto

    @GET("feed/categories")
    suspend fun feedCategories(): List<FeedCategoryDto>

    @PUT("videos/{videoId}/favorite")
    suspend fun setFavorite(
        @Path("videoId") videoId: Int,
        @Body payload: FavoriteUpdateRequestDto,
    ): FavoriteUpdateResponseDto

    @PUT("videos/{videoId}/watched")
    suspend fun setWatched(
        @Path("videoId") videoId: Int,
        @Body payload: WatchedUpdateRequestDto,
    ): WatchedUpdateResponseDto

    @DELETE("videos/{videoId}")
    suspend fun deleteVideo(
        @Path("videoId") videoId: Int,
        @Query("permanent") permanent: Boolean,
    ): DeleteVideoResponseDto
}

data class HealthDto(
    @Json(name = "status")
    val status: String,
)

data class FeedResponseDto(
    @Json(name = "items")
    val items: List<VideoDto>,
    @Json(name = "next_cursor")
    val nextCursor: String?,
)

data class FeedCategoryDto(
    @Json(name = "category")
    val category: String,
    @Json(name = "count")
    val count: Int,
)

data class VideoDto(
    @Json(name = "id")
    val id: Int,
    @Json(name = "status")
    val status: String,
    @Json(name = "stream_url")
    val streamUrl: String?,
    @Json(name = "thumbnail_url")
    val thumbnailUrl: String?,
    @Json(name = "duration_seconds")
    val durationSeconds: Double?,
    @Json(name = "width")
    val width: Int?,
    @Json(name = "height")
    val height: Int?,
    @Json(name = "is_favorite")
    val isFavorite: Boolean = false,
    @Json(name = "is_watched")
    val isWatched: Boolean = false,
    @Json(name = "path_category")
    val pathCategory: String? = null,
    @Json(name = "path_date")
    val pathDate: String? = null,
)

data class FavoriteUpdateRequestDto(
    @Json(name = "is_favorite")
    val isFavorite: Boolean,
)

data class FavoriteUpdateResponseDto(
    @Json(name = "success")
    val success: Boolean,
    @Json(name = "video_id")
    val videoId: Int,
    @Json(name = "is_favorite")
    val isFavorite: Boolean,
)

data class WatchedUpdateRequestDto(
    @Json(name = "is_watched")
    val isWatched: Boolean,
    @Json(name = "watched_ratio")
    val watchedRatio: Float? = null,
)

data class WatchedUpdateResponseDto(
    @Json(name = "success")
    val success: Boolean,
    @Json(name = "video_id")
    val videoId: Int,
    @Json(name = "is_watched")
    val isWatched: Boolean,
    @Json(name = "watched_ratio")
    val watchedRatio: Float?,
)

data class DeleteVideoResponseDto(
    @Json(name = "success")
    val success: Boolean,
)
