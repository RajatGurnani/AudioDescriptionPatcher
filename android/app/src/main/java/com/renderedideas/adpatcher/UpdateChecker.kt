package com.renderedideas.adpatcher

import android.app.Activity
import android.app.DownloadManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.net.Uri
import android.os.Environment
import android.util.Log
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.core.content.FileProvider
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import kotlin.concurrent.thread

/**
 * In-app updates from GitHub Releases: compare the latest release tag to
 * BuildConfig.VERSION_NAME; on user confirmation download the APK asset
 * and hand it to the system installer.
 *
 * NOTE: works once the repository is public - the unauthenticated
 * Releases API returns 404 for private repos. Until then a manual check
 * explains that instead of failing silently.
 */
object UpdateChecker {
    private const val TAG = "ADPatcher"
    private const val API = "https://api.github.com/repos/" +
            "RajatGurnani/AudioDescriptionPatcher/releases/latest"
    private const val CHECK_INTERVAL_MS = 24 * 60 * 60 * 1000L

    /** Silent daily check on launch; [manual]=true always checks + talks. */
    fun check(activity: Activity, manual: Boolean) {
        val prefs = activity.getSharedPreferences("updates",
            Context.MODE_PRIVATE)
        if (!manual) {
            val last = prefs.getLong("last_check", 0)
            if (System.currentTimeMillis() - last < CHECK_INTERVAL_MS) return
        }
        thread {
            try {
                prefs.edit().putLong("last_check",
                    System.currentTimeMillis()).apply()
                val conn = URL(API).openConnection() as HttpURLConnection
                conn.setRequestProperty("User-Agent", "ADPatcher-updater")
                conn.setRequestProperty("Accept",
                    "application/vnd.github+json")
                conn.connectTimeout = 10_000
                conn.readTimeout = 10_000
                val code = conn.responseCode
                if (code == 404) {
                    if (manual) toast(activity, "Update check needs the " +
                            "GitHub repo to be public (it is private).")
                    return@thread
                }
                if (code != 200) throw RuntimeException("HTTP $code")
                val body = conn.inputStream.bufferedReader().readText()

                val tag = Regex("\"tag_name\"\\s*:\\s*\"([^\"]+)\"")
                    .find(body)?.groupValues?.get(1)
                    ?: throw RuntimeException("no tag_name")
                val latest = tag.trimStart('v', 'V')
                val apkUrl = Regex(
                    "\"browser_download_url\"\\s*:\\s*\"([^\"]+\\.apk)\"")
                    .find(body)?.groupValues?.get(1)

                if (!isNewer(latest, BuildConfig.VERSION_NAME)) {
                    if (manual) toast(activity,
                        "Up to date (v${BuildConfig.VERSION_NAME})")
                    return@thread
                }
                if (apkUrl == null) {
                    if (manual) toast(activity,
                        "v$latest is out but has no APK attached")
                    return@thread
                }
                activity.runOnUiThread {
                    if (activity.isFinishing) return@runOnUiThread
                    AlertDialog.Builder(activity)
                        .setTitle("Update available")
                        .setMessage("v$latest is available " +
                            "(you have v${BuildConfig.VERSION_NAME}). " +
                            "Download and install?")
                        .setPositiveButton("Update") { _, _ ->
                            download(activity, apkUrl, latest)
                        }
                        .setNegativeButton("Later", null)
                        .show()
                }
            } catch (t: Throwable) {
                Log.w(TAG, "update check failed", t)
                if (manual) toast(activity,
                    "Update check failed: ${t.message}")
            }
        }
    }

    /** Semantic-ish version compare: "0.10.1" > "0.9.9". */
    private fun isNewer(a: String, b: String): Boolean {
        val pa = a.split('.').map { it.filter(Char::isDigit)
            .ifEmpty { "0" }.toInt() }
        val pb = b.split('.').map { it.filter(Char::isDigit)
            .ifEmpty { "0" }.toInt() }
        for (i in 0 until maxOf(pa.size, pb.size)) {
            val x = pa.getOrElse(i) { 0 }
            val y = pb.getOrElse(i) { 0 }
            if (x != y) return x > y
        }
        return false
    }

    private fun download(activity: Activity, url: String, version: String) {
        val name = "ADPatcher-v$version.apk"
        val dest = File(activity.getExternalFilesDir(
            Environment.DIRECTORY_DOWNLOADS), name)
        dest.delete()
        val request = DownloadManager.Request(Uri.parse(url))
            .setTitle(name)
            .setMimeType("application/vnd.android.package-archive")
            .setNotificationVisibility(DownloadManager.Request
                .VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
            .setDestinationInExternalFilesDir(activity,
                Environment.DIRECTORY_DOWNLOADS, name)
        val dm = activity.getSystemService(DownloadManager::class.java)
        val id = dm.enqueue(request)
        toast(activity, "Downloading update…")

        val receiver = object : BroadcastReceiver() {
            override fun onReceive(context: Context, intent: Intent) {
                if (intent.getLongExtra(
                        DownloadManager.EXTRA_DOWNLOAD_ID, -1) != id) return
                context.unregisterReceiver(this)
                if (!dest.exists()) {
                    toast(activity, "Update download failed")
                    return
                }
                val apkUri = FileProvider.getUriForFile(context,
                    "${context.packageName}.fileprovider", dest)
                context.startActivity(
                    Intent(Intent.ACTION_VIEW).apply {
                        setDataAndType(apkUri,
                            "application/vnd.android.package-archive")
                        addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or
                                Intent.FLAG_GRANT_READ_URI_PERMISSION)
                    })
            }
        }
        activity.applicationContext.registerReceiver(receiver,
            IntentFilter(DownloadManager.ACTION_DOWNLOAD_COMPLETE),
            Context.RECEIVER_EXPORTED)
    }

    private fun toast(activity: Activity, msg: String) =
        activity.runOnUiThread {
            Toast.makeText(activity, msg, Toast.LENGTH_LONG).show()
        }
}
