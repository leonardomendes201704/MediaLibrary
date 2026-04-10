package com.leonardo.medialibrary

import android.content.Intent
import android.os.Bundle
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.leonardo.medialibrary.domain.model.FeedMode
import com.leonardo.medialibrary.ui.common.HostActions
import com.leonardo.medialibrary.ui.common.PlayerProvider
import com.leonardo.medialibrary.ui.common.ProviderAccessSession
import com.leonardo.medialibrary.ui.common.ServiceLocator
import com.leonardo.medialibrary.ui.config.ServerConfigActivity
import com.leonardo.medialibrary.ui.onboarding.CategoryPickerActivity
import com.leonardo.medialibrary.ui.onboarding.ProviderGateActivity
import com.leonardo.medialibrary.ui.mobile.MobileFeedFragment
import com.leonardo.medialibrary.ui.tv.TvPlayerFragment
import com.leonardo.medialibrary.util.DeviceClassifier
import kotlinx.coroutines.launch

class MainActivity : AppCompatActivity(), HostActions {
    private var isProviderGateOpen = false
    private var isCategoryPickerOpen = false
    private var isServerConfigOpen = false

    private val providerGateLauncher = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) {
        isProviderGateOpen = false
        lifecycleScope.launch {
            ensureMainContent(forceReload = true)
        }
    }

    private val categoryPickerLauncher = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        isCategoryPickerOpen = false
        lifecycleScope.launch {
            if (result.resultCode != RESULT_OK) {
                ProviderAccessSession.isUnlocked = false
            }
            ensureMainContent(forceReload = true)
        }
    }

    private val configLauncher = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) {
        isServerConfigOpen = false
        lifecycleScope.launch {
            ensureMainContent(forceReload = true)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        lifecycleScope.launch {
            ensureMainContent(forceReload = false)
        }
    }

    override fun onStart() {
        super.onStart()
        lifecycleScope.launch {
            ensureMainContent(forceReload = false)
        }
    }

    override fun onDestroy() {
        if (isFinishing) {
            PlayerProvider.release()
        }
        super.onDestroy()
    }

    private suspend fun ensureMainContent(forceReload: Boolean) {
        if (!ProviderAccessSession.isUnlocked) {
            openProviderGate()
            return
        }

        val repository = ServiceLocator.mediaRepository
        val backendUrl = repository.getBackendUrl()
        if (backendUrl.isBlank()) {
            openServerConfig()
            return
        }

        val feedMode = repository.getActiveFeedMode()
        if (feedMode == FeedMode.CATEGORIES) {
            val selectedCategory = repository.getSelectedCategory().trim()
            if (selectedCategory.isBlank()) {
                openCategoryPicker()
                return
            }
        }

        val current = supportFragmentManager.findFragmentById(R.id.main_fragment_container)
        if (current != null && !forceReload) {
            return
        }

        val profile = DeviceClassifier.detect(this)
        val fragment = if (profile.isTv) {
            TvPlayerFragment()
        } else {
            MobileFeedFragment()
        }

        supportFragmentManager
            .beginTransaction()
            .replace(R.id.main_fragment_container, fragment)
            .commitNowAllowingStateLoss()
    }

    private fun openProviderGate() {
        if (isProviderGateOpen) return
        isProviderGateOpen = true
        providerGateLauncher.launch(Intent(this, ProviderGateActivity::class.java))
    }

    private fun openCategoryPicker() {
        if (isCategoryPickerOpen) return
        isCategoryPickerOpen = true
        categoryPickerLauncher.launch(Intent(this, CategoryPickerActivity::class.java))
    }

    override fun openServerConfig() {
        if (isServerConfigOpen) return
        isServerConfigOpen = true
        configLauncher.launch(Intent(this, ServerConfigActivity::class.java))
    }
}
