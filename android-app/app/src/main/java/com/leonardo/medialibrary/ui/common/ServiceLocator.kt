package com.leonardo.medialibrary.ui.common

import android.content.Context
import com.leonardo.medialibrary.data.local.AppPreferences
import com.leonardo.medialibrary.data.remote.BackendApiFactory
import com.leonardo.medialibrary.data.repository.MediaRepositoryImpl
import com.leonardo.medialibrary.domain.repository.MediaRepository

object ServiceLocator {
    private lateinit var appContext: Context

    fun init(context: Context) {
        if (!::appContext.isInitialized) {
            appContext = context.applicationContext
        }
    }

    val preferences: AppPreferences by lazy {
        AppPreferences(appContext)
    }

    val mediaRepository: MediaRepository by lazy {
        MediaRepositoryImpl(
            preferences = preferences,
            apiFactory = BackendApiFactory(),
        )
    }
}

