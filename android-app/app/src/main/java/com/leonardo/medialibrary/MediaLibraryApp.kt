package com.leonardo.medialibrary

import android.app.Activity
import android.app.Application
import android.os.Build
import android.os.Bundle
import androidx.lifecycle.DefaultLifecycleObserver
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.ProcessLifecycleOwner
import com.leonardo.medialibrary.ui.common.ProviderAccessSession
import com.leonardo.medialibrary.ui.common.ServiceLocator

class MediaLibraryApp : Application() {
    @Volatile
    private var topActivity: Activity? = null

    override fun onCreate() {
        super.onCreate()
        ServiceLocator.init(this)

        registerActivityLifecycleCallbacks(object : ActivityLifecycleCallbacks {
            override fun onActivityCreated(activity: Activity, savedInstanceState: Bundle?) = Unit

            override fun onActivityStarted(activity: Activity) {
                topActivity = activity
            }

            override fun onActivityResumed(activity: Activity) {
                topActivity = activity
            }

            override fun onActivityPaused(activity: Activity) = Unit

            override fun onActivityStopped(activity: Activity) = Unit

            override fun onActivitySaveInstanceState(activity: Activity, outState: Bundle) = Unit

            override fun onActivityDestroyed(activity: Activity) {
                if (topActivity === activity) {
                    topActivity = null
                }
            }
        })

        ProcessLifecycleOwner.get().lifecycle.addObserver(object : DefaultLifecycleObserver {
            override fun onStop(owner: LifecycleOwner) {
                // When app goes background/minimized, require new gate validation and close task.
                ProviderAccessSession.isUnlocked = false
                val activity = topActivity ?: return
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
                    activity.finishAndRemoveTask()
                } else {
                    activity.finishAffinity()
                }
            }
        })
    }
}
