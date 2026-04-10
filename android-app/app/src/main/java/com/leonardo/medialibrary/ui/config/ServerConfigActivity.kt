package com.leonardo.medialibrary.ui.config

import android.app.Activity
import android.os.Bundle
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.leonardo.medialibrary.R
import com.leonardo.medialibrary.databinding.ActivityServerConfigBinding
import com.leonardo.medialibrary.ui.common.ServiceLocator
import kotlinx.coroutines.launch

class ServerConfigActivity : AppCompatActivity() {
    private lateinit var binding: ActivityServerConfigBinding

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityServerConfigBinding.inflate(layoutInflater)
        setContentView(binding.root)

        lifecycleScope.launch {
            val current = ServiceLocator.mediaRepository.getBackendUrl()
            if (current.isNotBlank()) {
                binding.serverInput.setText(current)
            }
        }

        binding.testButton.setOnClickListener {
            lifecycleScope.launch {
                runConnectionTest()
            }
        }

        binding.saveButton.setOnClickListener {
            lifecycleScope.launch {
                saveAndClose()
            }
        }
    }

    private suspend fun runConnectionTest(): Boolean {
        val candidate = binding.serverInput.text?.toString().orEmpty()
        if (candidate.isBlank()) {
            showStatus("Informe um endereço válido.", isError = true)
            return false
        }
        showStatus("Testando conexão...", isError = false)
        val result = ServiceLocator.mediaRepository.testConnection(candidate)
        return result.fold(
            onSuccess = { normalized ->
                showStatus("Conexão OK: $normalized", isError = false)
                true
            },
            onFailure = { err ->
                showStatus("Falha: ${err.message}", isError = true)
                false
            },
        )
    }

    private suspend fun saveAndClose() {
        val candidate = binding.serverInput.text?.toString().orEmpty()
        if (candidate.isBlank()) {
            showStatus("Informe o endpoint antes de salvar.", isError = true)
            return
        }
        val ok = runConnectionTest()
        if (!ok) return

        ServiceLocator.mediaRepository.saveBackendUrl(candidate)
        setResult(Activity.RESULT_OK)
        finish()
    }

    private fun showStatus(message: String, isError: Boolean) {
        binding.serverStatusText.text = message
        val color = if (isError) {
            ContextCompat.getColor(this, R.color.danger)
        } else {
            ContextCompat.getColor(this, R.color.text_secondary)
        }
        binding.serverStatusText.setTextColor(color)
    }
}

