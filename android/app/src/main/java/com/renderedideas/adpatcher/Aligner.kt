package com.renderedideas.adpatcher

import org.jtransforms.fft.FloatFFT_1D
import kotlin.math.abs
import kotlin.math.ceil
import kotlin.math.floor
import kotlin.math.ln
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sqrt

/**
 * Alignment engine - a direct port of adpatch.py's fit_alignment().
 *
 * Recovers `video_time = a * ad_time + b` from the two onset envelopes:
 *
 *  Stage 1 (speed scan): grid-search stretch factors 0.95..1.05 on 20 Hz
 *  pooled envelopes. Each candidate stretches the AD envelope and
 *  cross-correlates it against the video envelope via FFT; the true factor
 *  lines onsets up across the whole runtime and wins by peak sharpness.
 *  This must happen before any local matching - uncorrected drift smears
 *  local correlations into noise.
 *
 *  Stage 2 (refinement): stretch the AD envelope by the winning factor,
 *  match 30 s segments spread over the runtime, and fit a weighted line
 *  through the (position, matched-position) pairs with outlier rejection.
 */
object Aligner {
    const val MIN_SEGMENT_SCORE = 0.15f
    const val WEAK_SCORE = 0.12f

    data class CheckPoint(
        val adSec: Double, val videoSec: Double,
        val score: Float, val residualMs: Double
    )

    data class Result(
        val a: Double, val bSec: Double,
        val report: List<CheckPoint>, val suspect: Boolean
    )

    // ---------------------------------------------------------------- FFT

    private fun nextPow2(n: Int): Int {
        var p = 1
        while (p < n) p = p shl 1
        return p
    }

    /** Pack a real signal into an interleaved complex array of size 2n. */
    private fun pack(x: FloatArray, n: Int): FloatArray {
        val out = FloatArray(2 * n)
        for (i in x.indices) out[2 * i] = x[i]
        return out
    }

    /**
     * Circular cross-correlation c[k] = sum_t x[t+k] * y[t] via FFT.
     * Returns the real part per lag (length n). [xFft] may pass a
     * precomputed FFT of x to amortize across calls.
     */
    private fun crossCorrelate(
        x: FloatArray?, y: FloatArray, n: Int, xFft: FloatArray? = null
    ): FloatArray {
        val fft = FloatFFT_1D(n.toLong())
        val fx = xFft ?: pack(x!!, n).also { fft.complexForward(it) }
        val fy = pack(y, n).also { fft.complexForward(it) }
        val r = FloatArray(2 * n)
        for (i in 0 until n) {
            val a = fx[2 * i]; val b = fx[2 * i + 1]
            val c = fy[2 * i]; val d = fy[2 * i + 1]
            r[2 * i] = a * c + b * d        // x * conj(y), real
            r[2 * i + 1] = b * c - a * d    // x * conj(y), imag
        }
        fft.complexInverse(r, true)
        return FloatArray(n) { r[2 * it] }
    }

    fun fftOf(x: FloatArray, n: Int): FloatArray =
        pack(x, n).also { FloatFFT_1D(n.toLong()).complexForward(it) }

    // ---------------------------------------------------------- envelope ops

    fun pool(env: FloatArray, factor: Int): FloatArray {
        val n = env.size / factor
        return FloatArray(n) { i ->
            var s = 0f
            for (j in 0 until factor) s += env[i * factor + j]
            s / factor
        }
    }

    /** Time-stretch: content at frame i lands at frame factor*i. */
    fun stretch(env: FloatArray, factor: Double): FloatArray {
        val m = (env.size * factor).toInt()
        return FloatArray(m) { j ->
            val p = j / factor
            val i = floor(p).toInt()
            if (i >= env.size - 1) env[env.size - 1]
            else {
                val f = (p - i).toFloat()
                env[i] + (env[i + 1] - env[i]) * f
            }
        }
    }

    private fun norm(x: FloatArray): Float {
        var s = 0.0
        for (v in x) s += v * v
        return sqrt(s).toFloat()
    }

    // ------------------------------------------------------------ stage 1

    private class ScanResult(val factor: Double, val lag: Int,
                             val score: Float)

    /**
     * Try each stretch factor; return the one whose global correlation
     * peak is sharpest, with its lag. FFT of the video envelope is
     * computed once and reused for every candidate.
     */
    private fun speedScan(
        ad: FloatArray, vid: FloatArray, factors: DoubleArray,
        onProgress: (Float) -> Unit
    ): ScanResult {
        val maxLen = ceil(ad.size * factors.max()).toInt()
        val n = nextPow2(vid.size + maxLen)
        val vidFft = fftOf(vid, n)
        val vidNorm = norm(vid)

        // The grid is embarrassingly parallel: fan candidates out over the
        // cores (capped to bound the ~3 float arrays of size 2n each
        // worker holds live).
        val workers = minOf(Runtime.getRuntime().availableProcessors(),
            factors.size, 4)
        val results = arrayOfNulls<ScanResult>(factors.size)
        val next = java.util.concurrent.atomic.AtomicInteger(0)
        val done = java.util.concurrent.atomic.AtomicInteger(0)
        val threads = (0 until workers).map {
            Thread {
                while (true) {
                    val i = next.getAndIncrement()
                    if (i >= factors.size) break
                    val f = factors[i]
                    val stretched = stretch(ad, f)
                    val cc = crossCorrelate(null, stretched, n, vidFft)
                    var k = 0
                    for (j in cc.indices) if (cc[j] > cc[k]) k = j
                    val score = cc[k] / (vidNorm * norm(stretched) + 1e-9f)
                    results[i] =
                        ScanResult(f, if (k > n / 2) k - n else k, score)
                    onProgress(done.incrementAndGet().toFloat() /
                        factors.size)
                }
            }.apply { start() }
        }
        threads.forEach { it.join() }
        return results.filterNotNull().maxByOrNull { it.score }!!
    }

    // ------------------------------------------------------------ stage 2

    private class Match(val u: Int, val vpos: Double, val score: Float)

    /**
     * Locate one segment of the stretched AD envelope inside the video
     * envelope, searching +-[search] frames around [expected]. Returns the
     * matched video position (sub-frame, via parabolic interpolation) and
     * the cosine-similarity score at the peak.
     */
    private fun segmentMatch(
        stretched: FloatArray, vid: FloatArray,
        segStart: Int, segLen: Int, expected: Double, search: Int
    ): Match? {
        val seg = stretched.copyOfRange(segStart, segStart + segLen)
        val w0 = max(0, (expected - search).toInt())
        val w1 = min(vid.size, (expected + segLen + search).toInt())
        if (w1 - w0 < segLen + 10) return null
        val win = vid.copyOfRange(w0, w1)

        val n = nextPow2(win.size + segLen)
        val cc = crossCorrelate(win, seg, n)
        val valid = win.size - segLen
        // sliding L2 norm of the window for proper normalization
        val csum = DoubleArray(win.size + 1)
        for (i in win.indices) csum[i + 1] = csum[i] +
            win[i].toDouble() * win[i]
        val segNorm = norm(seg)
        var bestK = 0
        var bestScore = -1f
        for (k in 0 until valid) {
            val wNorm = sqrt(csum[k + segLen] - csum[k]).toFloat()
            val score = cc[k] / (segNorm * wNorm + 1e-9f)
            if (score > bestScore) { bestScore = score; bestK = k }
        }
        // parabolic sub-frame refinement around the peak
        var kf = bestK.toDouble()
        if (bestK in 1 until valid - 1) {
            val y0 = cc[bestK - 1]; val y1 = cc[bestK]; val y2 = cc[bestK + 1]
            val denom = y0 - 2 * y1 + y2
            if (denom != 0f) kf = bestK + 0.5 * (y0 - y2) / denom
        }
        return Match(segStart, w0 + kf, bestScore)
    }

    /** Weighted least-squares line fit y = a*x + b. */
    private fun linearFit(
        xs: DoubleArray, ys: DoubleArray, ws: DoubleArray
    ): Pair<Double, Double> {
        var sw = 0.0; var sx = 0.0; var sy = 0.0; var sxx = 0.0; var sxy = 0.0
        for (i in xs.indices) {
            val w = ws[i]
            sw += w; sx += w * xs[i]; sy += w * ys[i]
            sxx += w * xs[i] * xs[i]; sxy += w * xs[i] * ys[i]
        }
        val denom = sw * sxx - sx * sx
        if (abs(denom) < 1e-12) return Pair(1.0, ys[0] - xs[0])
        val a = (sw * sxy - sx * sy) / denom
        val b = (sy * sxx - sx * sxy) / denom
        return Pair(a, b)
    }

    // ------------------------------------------------------------- driver

    fun fitAlignment(
        adEnv: FloatArray, vidEnv: FloatArray,
        log: (String) -> Unit, onProgress: (Float) -> Unit
    ): Result {
        val fps = AudioEngine.FPS

        // Stage 1: coarse (0.2%) then fine (0.02%) speed grid at 20 Hz.
        val poolF = 5
        val ad20 = pool(adEnv, poolF)
        val vid20 = pool(vidEnv, poolF)
        val coarse = DoubleArray(51) { 0.95 + it * 0.002 }
        var a = speedScan(ad20, vid20, coarse) { onProgress(it * 0.6f) }
            .factor
        val fine = DoubleArray(21) { a - 0.002 + it * 0.0002 }
        var scan = speedScan(ad20, vid20, fine) {
            onProgress(0.6f + it * 0.3f)
        }
        a = scan.factor

        // Snap to a "special" ratio (exact 1.0, PAL<->film) when one sits
        // within a coarse grid step of the winner and correlates almost as
        // well - weak-content scans otherwise pick a spuriously drifted
        // factor that turns into an audible sync slope.
        val specials = doubleArrayOf(1.0, 25 / 23.976, 23.976 / 25,
            25.0 / 24, 24.0 / 25, 23.976 / 24, 24 / 23.976)
        for (special in specials) {
            if (abs(special - a) < 0.003 && abs(special - a) > 1e-9) {
                val s = speedScan(ad20, vid20, doubleArrayOf(special)) {}
                if (s.score >= 0.98f * scan.score) {
                    log("snapped speed %.6f -> %.6f".format(a, special))
                    scan = s; a = s.factor
                }
                break
            }
        }
        var b = scan.lag.toDouble() * poolF   // back to 100 Hz frames
        log("coarse: speed %.4f, offset %+.2fs (peak %.2f)"
            .format(a, b / fps, scan.score))

        // Stage 2: segment refinement on the stretched envelope.
        val a0 = a
        val stretched = stretch(adEnv, a0)
        var segLen = (30 * fps).toInt()
        var usable = stretched.size - segLen
        if (usable <= 0) {
            segLen = max(stretched.size / 2, (5 * fps).toInt())
            usable = stretched.size - segLen
        }
        val nSeg = max(3, min(16, usable / (2 * segLen) + 1))
        val matches = ArrayList<Match>()
        for (i in 0 until nSeg) {
            val u = (usable.toLong() * i / max(1, nSeg - 1)).toInt()
            segmentMatch(stretched, vidEnv, u, segLen,
                u + b, (5 * fps).toInt())?.let { matches.add(it) }
        }
        onProgress(1f)

        var a2 = 1.0
        var b2 = b
        var confident = matches.filter { it.score > MIN_SEGMENT_SCORE }
        if (confident.size >= 2) {
            repeat(2) {   // fit, drop outliers, refit once
                val xs = confident.map { it.u.toDouble() }.toDoubleArray()
                val ys = confident.map { it.vpos }.toDoubleArray()
                val ws = confident.map { it.score.toDouble() }.toDoubleArray()
                val fit = linearFit(xs, ys, ws)
                a2 = fit.first; b2 = fit.second
                val resid = confident.map { abs(it.vpos - (a2 * it.u + b2)) }
                val median = resid.sorted()[resid.size / 2]
                val cutoff = max(3 * median + 1e-9, 0.05 * fps)
                val kept = confident.filterIndexed { i, _ -> resid[i] < cutoff }
                if (kept.size == confident.size || kept.size < 2) return@repeat
                confident = kept
            }
        } else if (confident.size == 1) {
            // one trustworthy anchor: refine the offset only
            b2 = confident[0].vpos - confident[0].u
            log("one confident segment match; refined offset only")
        } else {
            log("warning: too few confident matches; keeping coarse alignment")
        }

        val finalA = a0 * a2
        val finalB = b2
        var weak = 0
        val report = matches.map { m ->
            val residMs = (m.vpos - (a2 * m.u + b2)) / fps * 1000
            val centerAd = (m.u + segLen / 2) / a0
            if (m.score <= WEAK_SCORE) weak++
            CheckPoint(centerAd / fps,
                (finalA * centerAd + finalB) / fps, m.score, residMs)
        }
        return Result(finalA, finalB / fps, report,
            suspect = weak > matches.size / 2)
    }
}
