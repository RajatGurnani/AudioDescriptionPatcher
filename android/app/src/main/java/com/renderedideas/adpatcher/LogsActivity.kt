package com.renderedideas.adpatcher

import android.content.Intent
import android.os.Bundle
import android.view.Gravity
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.FileProvider
import java.io.File

/**
 * ReVanced-style in-app log viewer: shows the current/most recent session
 * log with share/copy, and a picker for older session files.
 */
class LogsActivity : AppCompatActivity() {

    private lateinit var content: TextView
    private lateinit var titleView: TextView
    private var shownFile: File? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        com.google.android.material.color.DynamicColors
            .applyToActivityIfAvailable(this)
        title = "Logs"

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            val pad = (12 * resources.displayMetrics.density).toInt()
            setPadding(pad, pad, pad, pad)
        }
        titleView = TextView(this).apply { textSize = 16f }
        val buttons = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.START
        }
        val share = Button(this).apply { text = "Share" }
        val copy = Button(this).apply { text = "Copy" }
        val older = Button(this).apply { text = "Older…" }
        buttons.addView(share); buttons.addView(copy); buttons.addView(older)

        content = TextView(this).apply {
            typeface = android.graphics.Typeface.MONOSPACE
            textSize = 11f
            setTextIsSelectable(true)
        }
        val scroll = ScrollView(this).apply { addView(content) }
        root.addView(titleView)
        root.addView(buttons)
        root.addView(scroll, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f))
        setContentView(root)

        share.setOnClickListener { shareCurrent() }
        copy.setOnClickListener {
            val cm = getSystemService(android.content.ClipboardManager::class.java)
            cm.setPrimaryClip(android.content.ClipData.newPlainText(
                "adpatcher log", content.text))
            Toast.makeText(this, "Copied", Toast.LENGTH_SHORT).show()
        }
        older.setOnClickListener { pickOlder() }

        showLive()
    }

    private fun logFiles(): List<File> =
        JobRunner.logsDir(this).listFiles { f -> f.extension == "log" }
            ?.sortedByDescending { it.lastModified() } ?: emptyList()

    /** Live session if one exists, else the newest file on disk. */
    private fun showLive() {
        val live = JobRunner.logSnapshot()
        if (live.isNotBlank()) {
            titleView.text = "Current session"
            shownFile = null
            content.text = live
        } else {
            val newest = logFiles().firstOrNull()
            if (newest != null) showFile(newest)
            else {
                titleView.text = "No logs yet"
                content.text = "Run a patch job first."
            }
        }
    }

    private fun showFile(f: File) {
        titleView.text = f.name
        shownFile = f
        content.text = try { f.readText() }
        catch (e: Exception) { "could not read: ${e.message}" }
    }

    private fun pickOlder() {
        val files = logFiles()
        if (files.isEmpty()) {
            Toast.makeText(this, "No saved logs", Toast.LENGTH_SHORT).show()
            return
        }
        val names = files.map {
            "${it.name}  (${it.length() / 1024} KB)"
        }.toTypedArray()
        AlertDialog.Builder(this)
            .setTitle("Session logs")
            .setItems(names) { _, which -> showFile(files[which]) }
            .show()
    }

    private fun shareCurrent() {
        val f = shownFile ?: run {
            // live session: write a temp file so it can be shared
            val dir = JobRunner.logsDir(this).apply { mkdirs() }
            File(dir, "current-session.log")
                .apply { writeText(content.text.toString()) }
        }
        val uri = FileProvider.getUriForFile(this,
            "$packageName.fileprovider", f)
        startActivity(Intent.createChooser(
            Intent(Intent.ACTION_SEND).apply {
                type = "text/plain"
                putExtra(Intent.EXTRA_STREAM, uri)
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            }, "Share log"))
    }
}
