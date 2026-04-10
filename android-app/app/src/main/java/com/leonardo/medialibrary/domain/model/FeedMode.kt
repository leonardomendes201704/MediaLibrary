package com.leonardo.medialibrary.domain.model

enum class FeedMode {
    NORMAL,
    FAVORITES_ONLY,
    NEWEST,
    CATEGORIES,
}

object ProviderAccessRules {
    const val CODE_NORMAL = "secret"
    const val CODE_FAVORITES = "secretfav"
    const val CODE_NEWEST = "secretnew"
    const val CODE_CATEGORIES = "categories"

    fun resolveMode(providerName: String): FeedMode? {
        return when (providerName.trim().lowercase()) {
            CODE_NORMAL -> FeedMode.NORMAL
            CODE_FAVORITES -> FeedMode.FAVORITES_ONLY
            CODE_NEWEST -> FeedMode.NEWEST
            CODE_CATEGORIES -> FeedMode.CATEGORIES
            else -> null
        }
    }
}
