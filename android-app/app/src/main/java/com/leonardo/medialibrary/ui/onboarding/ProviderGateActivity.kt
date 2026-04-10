package com.leonardo.medialibrary.ui.onboarding

import android.app.Activity
import android.os.Bundle
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.leonardo.medialibrary.R
import com.leonardo.medialibrary.databinding.ActivityProviderGateBinding
import com.leonardo.medialibrary.domain.model.FeedMode
import com.leonardo.medialibrary.domain.model.ProviderAccessRules
import com.leonardo.medialibrary.ui.common.ProviderAccessSession
import com.leonardo.medialibrary.ui.common.ServiceLocator
import kotlinx.coroutines.launch

class ProviderGateActivity : AppCompatActivity() {
    private lateinit var binding: ActivityProviderGateBinding

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityProviderGateBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.providerNameInput.setText("")

        binding.saveProviderButton.setOnClickListener {
            lifecycleScope.launch {
                handleSave()
            }
        }
    }

    private suspend fun handleSave() {
        val providerName = binding.providerNameInput.text?.toString()?.trim().orEmpty()
        if (providerName.isBlank()) {
            showStatus(getString(R.string.provider_name_required), isError = true)
            return
        }

        ServiceLocator.preferences.saveProviderName(providerName)

        val mode = ProviderAccessRules.resolveMode(providerName)
        if (mode != null) {
            if (mode == FeedMode.CATEGORIES) {
                ServiceLocator.preferences.clearSelectedCategory()
            }
            ProviderAccessSession.isUnlocked = true
            showStatus(getString(R.string.provider_access_granted), isError = false)
            setResult(Activity.RESULT_OK)
            finish()
        } else {
            ProviderAccessSession.isUnlocked = false
            showStatus(getString(R.string.provider_access_denied), isError = true)
        }
    }

    private fun showStatus(message: String, isError: Boolean) {
        binding.providerStatusText.text = message
        val color = if (isError) {
            ContextCompat.getColor(this, R.color.danger)
        } else {
            ContextCompat.getColor(this, R.color.text_secondary)
        }
        binding.providerStatusText.setTextColor(color)
    }
}
