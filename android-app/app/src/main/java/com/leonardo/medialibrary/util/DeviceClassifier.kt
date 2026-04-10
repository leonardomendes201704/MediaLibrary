package com.leonardo.medialibrary.util

import android.app.UiModeManager
import android.content.Context
import android.content.pm.PackageManager
import android.content.res.Configuration
import android.os.Build

enum class DeviceType {
    FIRE_TV,
    ANDROID_TV,
    TABLET,
    PHONE,
}

data class DeviceProfile(
    val type: DeviceType,
    val isTv: Boolean,
    val isTablet: Boolean,
)

object DeviceClassifier {
    private const val FEATURE_FIRE_TV = "amazon.hardware.fire_tv"

    fun detect(context: Context): DeviceProfile {
        val pm = context.packageManager
        val uiModeManager = context.getSystemService(Context.UI_MODE_SERVICE) as UiModeManager
        val modeType = uiModeManager.currentModeType

        val isTvMode = modeType == Configuration.UI_MODE_TYPE_TELEVISION
        val hasLeanback = pm.hasSystemFeature(PackageManager.FEATURE_LEANBACK)
        val isFireManufacturer = Build.MANUFACTURER.equals("Amazon", ignoreCase = true)
        val hasFireTvFeature = pm.hasSystemFeature(FEATURE_FIRE_TV)
        val isFireTvModel = Build.MODEL.startsWith("AFT")

        if (isFireManufacturer && (hasFireTvFeature || isFireTvModel || isTvMode)) {
            return DeviceProfile(type = DeviceType.FIRE_TV, isTv = true, isTablet = false)
        }
        if (isTvMode || hasLeanback) {
            return DeviceProfile(type = DeviceType.ANDROID_TV, isTv = true, isTablet = false)
        }

        val smallest = context.resources.configuration.smallestScreenWidthDp
        if (smallest >= 600) {
            return DeviceProfile(type = DeviceType.TABLET, isTv = false, isTablet = true)
        }
        return DeviceProfile(type = DeviceType.PHONE, isTv = false, isTablet = false)
    }
}

