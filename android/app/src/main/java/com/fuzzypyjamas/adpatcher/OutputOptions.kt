package com.fuzzypyjamas.adpatcher

import android.content.Context

/**
 * Android subset of the PC's output options (adpatch.DEFAULT_OPTIONS):
 * the AD track's AAC bitrate and whether the original audio is kept as a
 * second track. MediaMuxer writes MP4 only, so container / subtitle /
 * codec choices stay PC-only. Saved in prefs "output"; Patcher reads it.
 */
data class OutputOptions(
    val adBitrate: Int = DEFAULT_BITRATE,
    val keepOriginal: Boolean = true
) {
    fun save(context: Context) {
        prefs(context).edit()
            .putInt("ad_bitrate", adBitrate)
            .putBoolean("keep_original", keepOriginal)
            .apply()
    }

    override fun toString() = "AD ${adBitrate / 1000}k, " +
        if (keepOriginal) "original kept" else "AD only"

    companion object {
        const val DEFAULT_BITRATE = 192_000
        val BITRATES = intArrayOf(128_000, 192_000, 256_000)

        private fun prefs(c: Context) =
            c.getSharedPreferences("output", Context.MODE_PRIVATE)

        fun load(context: Context): OutputOptions {
            val p = prefs(context)
            val br = p.getInt("ad_bitrate", DEFAULT_BITRATE)
            return OutputOptions(
                if (br in BITRATES) br else DEFAULT_BITRATE,
                p.getBoolean("keep_original", true))
        }
    }
}
