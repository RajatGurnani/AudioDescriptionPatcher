package com.fuzzypyjamas.adpatcher.describe

import android.graphics.Bitmap
import android.media.MediaMetadataRetriever
import org.json.JSONArray
import org.json.JSONObject
import kotlin.math.max
import kotlin.math.min

/**
 * Turns narration gaps + shots into cues: where a description may be
 * spoken and which frames it should describe (docs/plan/10, A3).
 * Rules mirror adgen.py `plan_cues` - keep in sync.
 */
object CuePlanner {

    class Cue(
        val start: Double, val end: Double,
        val frameTimes: DoubleArray,     // seconds, oldest first
        val shotIds: IntArray,
        val fresh: Boolean,              // shows a shot no earlier cue showed
    ) {
        fun toJson(): JSONObject = JSONObject().put("start", r3(start)).put("end", r3(end))
            .put("frames", JSONArray().apply { frameTimes.forEach { put(r3(it)) } })
            .put("shots", JSONArray().apply { shotIds.forEach { put(it) } })
            .put("fresh", fresh)
    }

    const val LOOKBACK_S = 4.0     // shots this far before a gap still count
    const val MAX_FRAMES = 3       // newest shots per cue
    const val MAX_CUE_S = 10.0     // longer gaps are split into cues
    const val MIN_STALE_S = 4.0    // a cue with nothing new needs this much room

    fun plan(gaps: List<DoubleArray>, shots: List<DoubleArray>): List<Cue> {
        val cues = ArrayList<Cue>()
        var lastShown = -1
        for (g in gaps) {
            // split long gaps into near-equal pieces of <= MAX_CUE_S
            val pieces = max(1, Math.ceil((g[1] - g[0]) / MAX_CUE_S).toInt())
            val len = (g[1] - g[0]) / pieces
            for (p in 0 until pieces) {
                val c0 = g[0] + p * len
                val c1 = c0 + len
                val from = if (p == 0) c0 - LOOKBACK_S else c0
                val ids = shots.indices.filter { shots[it][1] > from && shots[it][0] < c1 }
                    .takeLast(MAX_FRAMES)
                if (ids.isEmpty()) continue
                val times = ids.map { i ->
                    val a = max(shots[i][0], from)
                    val b = min(shots[i][1], c1)
                    (a + b) / 2
                }
                val fresh = ids.last() > lastShown
                if (!fresh && c1 - c0 < MIN_STALE_S) continue
                cues += Cue(c0, c1, times.toDoubleArray(), ids.toIntArray(), fresh)
                lastShown = max(lastShown, ids.last())
            }
        }
        return cues
    }

    /** Frames for the VLM: [width] px wide, aspect kept. */
    fun grab(r: MediaMetadataRetriever, tS: Double, width: Int = 512): Bitmap? {
        val us = (tS * 1_000_000).toLong()
        val src = r.getFrameAtTime(us, MediaMetadataRetriever.OPTION_CLOSEST) ?: return null
        if (src.width <= width) return src
        val h = Math.round(src.height * width / src.width.toFloat())
        return Bitmap.createScaledBitmap(src, width, h, true).also { if (it !== src) src.recycle() }
    }

    private fun r3(v: Double) = Math.round(v * 1000) / 1000.0
}
