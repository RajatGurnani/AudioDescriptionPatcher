package com.renderedideas.adpatcher

import android.content.Context
import android.net.Uri

/**
 * Tiny persistence for job state + conveniences, all in SharedPreferences.
 *
 * - "last job" manifest: input/output URIs, plus the alignment result once
 *   known. An incomplete manifest on launch => offer to resume (and if the
 *   alignment was saved, resuming skips straight to muxing).
 * - last-used inputs for auto-filling the pickers.
 * - the most recent AudioVault download, for auto-selecting as AD input.
 */
object JobStore {
    private const val PREFS = "jobstore"

    private fun prefs(c: Context) =
        c.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun saveJob(c: Context, video: Uri, ad: Uri, out: Uri,
                a: Double?, b: Double?) {
        prefs(c).edit()
            .putString("video", video.toString())
            .putString("ad", ad.toString())
            .putString("out", out.toString())
            .putBoolean("complete", false)
            .apply()
        if (a != null && b != null) saveAlignment(c, a, b)
        else prefs(c).edit().remove("a").remove("b").apply()
    }

    fun saveAlignment(c: Context, a: Double, b: Double) {
        prefs(c).edit()
            .putLong("a", java.lang.Double.doubleToRawLongBits(a))
            .putLong("b", java.lang.Double.doubleToRawLongBits(b))
            .apply()
    }

    fun markComplete(c: Context) {
        prefs(c).edit().putBoolean("complete", true).apply()
    }

    class SavedJob(
        val video: Uri, val ad: Uri, val out: Uri,
        val a: Double?, val b: Double?
    )

    /** The unfinished job to offer for resume, if any and still readable. */
    fun incompleteJob(c: Context): SavedJob? {
        val p = prefs(c)
        if (p.getBoolean("complete", true)) return null
        val video = p.getString("video", null) ?: return null
        val ad = p.getString("ad", null) ?: return null
        val out = p.getString("out", null) ?: return null
        val job = SavedJob(
            Uri.parse(video), Uri.parse(ad), Uri.parse(out),
            if (p.contains("a")) java.lang.Double.longBitsToDouble(
                p.getLong("a", 0)) else null,
            if (p.contains("b")) java.lang.Double.longBitsToDouble(
                p.getLong("b", 0)) else null)
        // stale grants make resume impossible - verify we can still read
        return try {
            c.contentResolver.openFileDescriptor(job.video, "r")?.close()
            c.contentResolver.openFileDescriptor(job.ad, "r")?.close()
            job
        } catch (e: Exception) {
            null
        }
    }

    fun saveLastInputs(c: Context, video: Uri?, ad: Uri?) {
        val e = prefs(c).edit()
        video?.let { e.putString("last_video", it.toString()) }
        ad?.let { e.putString("last_ad", it.toString()) }
        e.apply()
    }

    fun lastVideo(c: Context): Uri? =
        prefs(c).getString("last_video", null)?.let { Uri.parse(it) }

    fun lastAd(c: Context): Uri? =
        prefs(c).getString("last_ad", null)?.let { Uri.parse(it) }

    fun saveVaultDownload(c: Context, path: String) {
        prefs(c).edit().putString("vault_download", path).apply()
    }

    /** Consume (read-and-clear) the most recent AudioVault download. */
    fun takeVaultDownload(c: Context): String? {
        val p = prefs(c).getString("vault_download", null)
        if (p != null) prefs(c).edit().remove("vault_download").apply()
        return p
    }

    fun clearAll(c: Context) {
        prefs(c).edit().clear().apply()
    }
}
