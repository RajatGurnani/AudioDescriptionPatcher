package com.fuzzypyjamas.adpatcher.describe

import android.content.Context
import android.media.Image
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaExtractor
import android.media.MediaFormat
import android.net.Uri
import android.os.SystemClock
import com.fuzzypyjamas.adpatcher.AudioEngine
import org.json.JSONArray
import org.json.JSONObject
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min

/**
 * Shot-cut detection (docs/plan/10, A3): decodes the video track with
 * MediaCodec, reduces every frame to a 64x36 luma + 32x18 chroma grid of
 * block means, scores each frame by mean |difference| to the previous
 * one and calls a cut where the score is high both absolutely and
 * relative to its neighbours (adaptive, like PySceneDetect's
 * AdaptiveDetector). The timeline is split over [workers] parallel
 * decoders, like GapFinder / AudioEngine.onsetEnvelopeParallel.
 *
 * Features + rules mirror tools/shots_ref.py (`own`) - keep in sync.
 */
class ShotDetector(private val workers: Int = 2) {

    class Result(val cuts: DoubleArray, val durationS: Double, val frames: Int) {
        /** Shots as [start, end] seconds. */
        fun shots(): List<DoubleArray> {
            val b = doubleArrayOf(0.0) + cuts + doubleArrayOf(durationS)
            return (0 until b.size - 1).map { doubleArrayOf(b[it], b[it + 1]) }
        }

        fun toJson(): JSONObject = JSONObject().put("duration", durationS).put("frames", frames)
            .put("cuts", JSONArray().apply { cuts.forEach { put(Math.round(it * 1000) / 1000.0) } })
    }

    var wallMs = 0L; private set

    fun analyze(context: Context, uri: Uri, onProgress: (Float) -> Unit = {}): Result {
        val t0 = SystemClock.elapsedRealtime()
        val durUs = probeVideoDurationUs(context, uri)
        val nw = if (durUs < 60_000_000L) 1 else workers
        val parts = arrayOfNulls<FrameScores>(nw)
        val errors = arrayOfNulls<Throwable>(nw)
        val frac = FloatArray(nw)
        val threads = (0 until nw).map { w ->
            Thread {
                try {
                    val fromUs = durUs * w / nw
                    val toUs = if (w == nw - 1) Long.MAX_VALUE else durUs * (w + 1) / nw
                    parts[w] = decodeRange(context, uri, fromUs, toUs, durUs) { f ->
                        frac[w] = f; onProgress(frac.sum() / nw)
                    }
                } catch (t: Throwable) {
                    errors[w] = t
                }
            }.apply { start() }
        }
        threads.forEach { it.join() }
        errors.filterNotNull().firstOrNull()?.let { throw it }
        // concatenate ranges (each holds frames with pts in its own range)
        val n = parts.sumOf { it!!.n }
        val ts = DoubleArray(n)
        val sc = FloatArray(n)
        var o = 0
        for (p in parts) {
            System.arraycopy(p!!.t, 0, ts, o, p.n)
            System.arraycopy(p.s, 0, sc, o, p.n)
            o += p.n
        }
        if (n > 0) sc[0] = 0f
        wallMs = SystemClock.elapsedRealtime() - t0
        val dur = max(durUs / 1e6, if (n > 0) ts[n - 1] else 0.0)
        return Result(cutsFromScores(ts, sc), dur, n)
    }

    /** Per-frame pts (s) + score (vs previous frame) for one range. */
    internal class FrameScores {
        var t = DoubleArray(1 shl 14); var s = FloatArray(1 shl 14); var n = 0
        fun add(time: Double, score: Float) {
            if (n == t.size) { t = t.copyOf(n * 2); s = s.copyOf(n * 2) }
            t[n] = time; s[n] = score; n++
        }
    }

    private fun decodeRange(
        context: Context, uri: Uri, fromUs: Long, toUs: Long, durUs: Long,
        onProgress: (Float) -> Unit,
    ): FrameScores {
        val spanUs = max(1L, min(toUs, durUs) - fromUs)
        // feed input up to 1 s past the range (B-frame reorder); the last
        // range has toUs = Long.MAX_VALUE, so don't overflow
        val inputEndUs = if (toUs > Long.MAX_VALUE - 1_000_000L) Long.MAX_VALUE else toUs + 1_000_000L
        val out = FrameScores()
        val ex = MediaExtractor()
        context.contentResolver.openFileDescriptor(uri, "r")!!.use { pfd ->
            ex.setDataSource(pfd.fileDescriptor)
            val track = (0 until ex.trackCount).first {
                ex.getTrackFormat(it).getString(MediaFormat.KEY_MIME)!!.startsWith("video/")
            }
            ex.selectTrack(track)
            val fmt = ex.getTrackFormat(track)
            // seek to the sync frame before the range, so the frame just
            // before fromUs exists to diff against
            ex.seekTo(fromUs, MediaExtractor.SEEK_TO_PREVIOUS_SYNC)
            fmt.setInteger(MediaFormat.KEY_COLOR_FORMAT,
                MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420Flexible)
            val codec = MediaCodec.createDecoderByType(fmt.getString(MediaFormat.KEY_MIME)!!)
            codec.configure(fmt, null, null, 0)
            codec.start()
            val info = MediaCodec.BufferInfo()
            var prev: Grid? = null
            var spare: Grid? = Grid()
            var inDone = false
            try {
                while (true) {
                    if (AudioEngine.cancelRequested) throw RuntimeException("cancelled")
                    if (!inDone) {
                        val ii = codec.dequeueInputBuffer(5_000)
                        if (ii >= 0) {
                            val sz = ex.readSampleData(codec.getInputBuffer(ii)!!, 0)
                            if (sz < 0 || ex.sampleTime > inputEndUs) {
                                codec.queueInputBuffer(ii, 0, 0, 0, MediaCodec.BUFFER_FLAG_END_OF_STREAM)
                                inDone = true
                            } else {
                                codec.queueInputBuffer(ii, 0, sz, ex.sampleTime, 0)
                                ex.advance()
                            }
                        }
                    }
                    val oi = codec.dequeueOutputBuffer(info, 5_000)
                    if (oi < 0) continue
                    val eos = info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0
                    val pts = info.presentationTimeUs
                    if (info.size > 0 && pts < toUs) {
                        val img = codec.getOutputImage(oi)
                        if (img != null) {
                            val g = spare ?: Grid()
                            g.fill(img)
                            img.close()
                            if (pts >= fromUs) {
                                out.add(pts / 1e6, prev?.let { g.diff(it) } ?: 0f)
                            }
                            spare = prev
                            prev = g
                        }
                        onProgress(((pts - fromUs).toFloat() / spanUs).coerceIn(0f, 1f))
                    }
                    codec.releaseOutputBuffer(oi, false)
                    if (eos || pts >= toUs) break
                }
            } finally {
                try { codec.stop() } catch (_: Exception) {}
                codec.release()
                ex.release()
            }
        }
        return out
    }

    private fun probeVideoDurationUs(context: Context, uri: Uri): Long {
        val ex = MediaExtractor()
        try {
            context.contentResolver.openFileDescriptor(uri, "r")!!.use { pfd ->
                ex.setDataSource(pfd.fileDescriptor)
                for (i in 0 until ex.trackCount) {
                    val f = ex.getTrackFormat(i)
                    if (f.getString(MediaFormat.KEY_MIME)!!.startsWith("video/"))
                        return if (f.containsKey(MediaFormat.KEY_DURATION)) f.getLong(MediaFormat.KEY_DURATION) else 0L
                }
            }
        } finally {
            ex.release()
        }
        throw RuntimeException("no video track found")
    }

    /**
     * 64x36 Y + 32x18 U/V block means. Inside each block every 4th
     * pixel/row is read (1/16 of the pixels) - close to ffmpeg's area
     * downscale used by shots_ref.py, at a fraction of the cost.
     */
    internal class Grid {
        val y = FloatArray(GW * GH)
        val u = FloatArray(GW * GH / 4)
        val v = FloatArray(GW * GH / 4)

        fun fill(img: Image) {
            val w = img.width
            val h = img.height
            plane(img.planes[0], w, h, GW, GH, y)
            plane(img.planes[1], w / 2, h / 2, GW / 2, GH / 2, u)
            plane(img.planes[2], w / 2, h / 2, GW / 2, GH / 2, v)
        }

        private fun plane(p: Image.Plane, w: Int, h: Int, gw: Int, gh: Int, dst: FloatArray) {
            val buf = p.buffer
            val rs = p.rowStride
            val ps = p.pixelStride
            for (by in 0 until gh) {
                val y0 = by * h / gh
                val y1 = max(y0 + 1, (by + 1) * h / gh)
                for (bx in 0 until gw) {
                    val x0 = bx * w / gw
                    val x1 = max(x0 + 1, (bx + 1) * w / gw)
                    var sum = 0
                    var cnt = 0
                    var yy = y0
                    while (yy < y1) {
                        var xx = x0
                        val row = yy * rs
                        while (xx < x1) {
                            sum += buf.get(row + xx * ps).toInt() and 0xff
                            cnt++
                            xx += SUB
                        }
                        yy += SUB
                    }
                    dst[by * gw + bx] = sum.toFloat() / cnt
                }
            }
        }

        /** mean|dY| + mean|dU| + mean|dV| (shots_ref.scores). */
        fun diff(o: Grid): Float = meanAbs(y, o.y) + meanAbs(u, o.u) + meanAbs(v, o.v)

        private fun meanAbs(a: FloatArray, b: FloatArray): Float {
            var s = 0f
            for (i in a.indices) s += abs(a[i] - b[i])
            return s / a.size
        }
    }

    companion object {
        // keep identical to tools/shots_ref.py
        const val GW = 64
        const val GH = 36
        const val ADAPT_WIN = 2
        const val ADAPT_RATIO = 3.0
        const val MIN_SCORE = 12.0
        const val MIN_SHOT_S = 0.6
        private const val SUB = 4

        fun cutsFromScores(ts: DoubleArray, s: FloatArray): DoubleArray {
            val cuts = ArrayList<Double>()
            var last = -1e9
            val n = s.size
            for (i in 1 until n) {
                var sum = 0.0
                var k = 0
                for (j in i - ADAPT_WIN..i + ADAPT_WIN) {
                    if (j != i && j >= 1 && j < n) { sum += s[j]; k++ }
                }
                val ref = max(sum / max(k, 1), 1e-3)
                if (s[i] >= MIN_SCORE && s[i] / ref >= ADAPT_RATIO && ts[i] - last >= MIN_SHOT_S) {
                    cuts += ts[i]
                    last = ts[i]
                }
            }
            return cuts.toDoubleArray()
        }
    }
}
