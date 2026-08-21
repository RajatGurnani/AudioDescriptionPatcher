package com.renderedideas.adpatcher

import android.app.DownloadManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.net.Uri
import android.os.Bundle
import android.os.Environment
import android.webkit.CookieManager
import android.webkit.DownloadListener
import android.webkit.URLUtil
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import java.io.File

/**
 * In-app AudioVault browser. The user logs in with their own account
 * (cookies persist across visits); tapping a Download link routes the
 * file through DownloadManager into this app's files, and the result is
 * auto-selected as the AD input back in MainActivity.
 *
 * Deliberately a plain WebView + user session rather than a scraping
 * API: no stored credentials, and it keeps working when the site's HTML
 * changes.
 */
class VaultActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private var downloadId = -1L

    private val onDownloadComplete = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            val id = intent.getLongExtra(
                DownloadManager.EXTRA_DOWNLOAD_ID, -1)
            if (id != downloadId) return
            val dm = getSystemService(DownloadManager::class.java)
            dm.query(DownloadManager.Query().setFilterById(id))?.use { c ->
                if (c.moveToFirst()) {
                    val status = c.getInt(c.getColumnIndexOrThrow(
                        DownloadManager.COLUMN_STATUS))
                    if (status == DownloadManager.STATUS_SUCCESSFUL) {
                        val uri = c.getString(c.getColumnIndexOrThrow(
                            DownloadManager.COLUMN_LOCAL_URI))
                        val path = Uri.parse(uri).path
                        if (path != null) {
                            JobStore.saveVaultDownload(
                                this@VaultActivity, path)
                            Toast.makeText(this@VaultActivity,
                                "Downloaded ✓ - selected as AD file",
                                Toast.LENGTH_LONG).show()
                            finish()
                        }
                    } else {
                        Toast.makeText(this@VaultActivity,
                            "Download failed", Toast.LENGTH_LONG).show()
                    }
                }
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        com.google.android.material.color.DynamicColors
            .applyToActivityIfAvailable(this)
        webView = WebView(this)
        setContentView(webView)
        title = "AudioVault"

        webView.settings.javaScriptEnabled = true
        webView.settings.domStorageEnabled = true
        webView.webViewClient = WebViewClient()
        CookieManager.getInstance().setAcceptCookie(true)

        webView.setDownloadListener(DownloadListener {
                url, userAgent, contentDisposition, mimetype, _ ->
            val name = URLUtil.guessFileName(
                url, contentDisposition, mimetype)
            val request = DownloadManager.Request(Uri.parse(url))
                .setTitle(name)
                .setMimeType(mimetype)
                .addRequestHeader("User-Agent", userAgent)
                .addRequestHeader("Cookie",
                    CookieManager.getInstance().getCookie(url) ?: "")
                .setNotificationVisibility(DownloadManager.Request
                    .VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                .setDestinationInExternalFilesDir(this,
                    Environment.DIRECTORY_DOWNLOADS, name)
            // clear any stale file with the same name
            File(getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS),
                name).delete()
            downloadId = getSystemService(DownloadManager::class.java)
                .enqueue(request)
            Toast.makeText(this, "Downloading $name…",
                Toast.LENGTH_SHORT).show()
        })

        registerReceiver(onDownloadComplete,
            IntentFilter(DownloadManager.ACTION_DOWNLOAD_COMPLETE),
            RECEIVER_EXPORTED)

        webView.loadUrl("https://audiovault.net")
    }

    override fun onDestroy() {
        unregisterReceiver(onDownloadComplete)
        super.onDestroy()
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (webView.canGoBack()) webView.goBack()
        else @Suppress("DEPRECATION") super.onBackPressed()
    }
}
