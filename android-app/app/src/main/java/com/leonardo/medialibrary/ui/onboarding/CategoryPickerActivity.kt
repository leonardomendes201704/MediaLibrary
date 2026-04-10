package com.leonardo.medialibrary.ui.onboarding

import android.app.Activity
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.AdapterView
import android.widget.BaseAdapter
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.leonardo.medialibrary.R
import com.leonardo.medialibrary.databinding.ActivityCategoryPickerBinding
import com.leonardo.medialibrary.domain.model.FeedCategory
import com.leonardo.medialibrary.ui.common.ServiceLocator
import kotlinx.coroutines.launch

class CategoryPickerActivity : AppCompatActivity() {
    private lateinit var binding: ActivityCategoryPickerBinding
    private val categories = mutableListOf<FeedCategory>()

    private val categoryAdapter: BaseAdapter = object : BaseAdapter() {
        override fun getCount(): Int = categories.size

        override fun getItem(position: Int): Any = categories[position]

        override fun getItemId(position: Int): Long = position.toLong()

        override fun getView(position: Int, convertView: View?, parent: ViewGroup): View {
            val view = convertView ?: LayoutInflater.from(parent.context)
                .inflate(R.layout.item_category_option, parent, false)
            val item = categories[position]
            view.findViewById<android.widget.TextView>(R.id.category_name_text).text = item.name
            view.findViewById<android.widget.TextView>(R.id.category_count_text).text =
                getString(R.string.category_picker_count_format, item.count)
            return view
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityCategoryPickerBinding.inflate(layoutInflater)
        setContentView(binding.root)

        setResult(Activity.RESULT_CANCELED)

        binding.categoryList.adapter = categoryAdapter
        binding.categoryList.onItemClickListener = AdapterView.OnItemClickListener { _, _, position, _ ->
            val selected = categories.getOrNull(position) ?: return@OnItemClickListener
            saveSelectionAndFinish(selected)
        }
        binding.categoryRetryButton.setOnClickListener {
            loadCategories()
        }

        loadCategories()
    }

    private fun loadCategories() {
        lifecycleScope.launch {
            setLoading(isLoading = true)
            runCatching {
                ServiceLocator.mediaRepository.loadFeedCategories()
            }.onSuccess { loaded ->
                categories.clear()
                categories.addAll(loaded)
                categoryAdapter.notifyDataSetChanged()
                val hasItems = categories.isNotEmpty()
                binding.categoryList.visibility = if (hasItems) View.VISIBLE else View.INVISIBLE
                if (hasItems) {
                    showStatus(getString(R.string.category_picker_choose), isError = false)
                } else {
                    showStatus(getString(R.string.category_picker_empty), isError = true)
                }
            }.onFailure { error ->
                categories.clear()
                categoryAdapter.notifyDataSetChanged()
                binding.categoryList.visibility = View.INVISIBLE
                showStatus(
                    message = error.message ?: getString(R.string.category_picker_load_failed),
                    isError = true,
                )
            }
            setLoading(isLoading = false)
        }
    }

    private fun saveSelectionAndFinish(category: FeedCategory) {
        lifecycleScope.launch {
            setLoading(isLoading = true)
            runCatching {
                ServiceLocator.mediaRepository.saveSelectedCategory(category.name)
            }.onSuccess {
                setResult(Activity.RESULT_OK)
                finish()
            }.onFailure { error ->
                setLoading(isLoading = false)
                showStatus(
                    message = error.message ?: getString(R.string.category_picker_save_failed),
                    isError = true,
                )
            }
        }
    }

    private fun setLoading(isLoading: Boolean) {
        binding.categoryLoading.visibility = if (isLoading) View.VISIBLE else View.GONE
        binding.categoryList.isEnabled = !isLoading
        binding.categoryRetryButton.isEnabled = !isLoading
    }

    private fun showStatus(message: String, isError: Boolean) {
        binding.categoryStatusText.text = message
        binding.categoryStatusText.setTextColor(
            if (isError) ContextCompat.getColor(this, R.color.danger)
            else ContextCompat.getColor(this, R.color.text_secondary),
        )
    }
}
