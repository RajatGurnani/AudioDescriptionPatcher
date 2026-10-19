package com.fuzzypyjamas.adpatcher

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
    private const val TAG = "ADPatcher"
    const val MIN_SEGMENT_SCORE = 0.15f
    const val WEAK_SCORE = 0.12f

    data class CheckPoint(
        val adSec: Double, val videoSec: Double,
        val score: Float, val residualMs: Double
    )

    /** Evidence behind the confidence score - same fields and meaning
     *  as adpatch.py's fit_alignment() `signals`. */
    data class Signals(
        val peak: Double, val peakRatio: Double, val coverage: Double,
        val residP95Ms: Double, val durRatio: Double
    )

    data class Result(
        val a: Double, val bSec: Double,
        val report: List<CheckPoint>, val suspect: Boolean,
        val confidence: Double, val band: String,
        val warnings: List<String>, val signals: Signals,
        val pieces: List<Piece> = emptyList()
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
        scores: MutableList<Pair<Double, Float>>? = null,
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
                    if (AudioEngine.cancelRequested) break
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
        if (AudioEngine.cancelRequested)
            throw RuntimeException("cancelled")
        results.filterNotNull().forEach { scores?.add(it.factor to it.score) }
        return results.filterNotNull().maxByOrNull { it.score }!!
    }

    /** Uniqueness of the global peak at [factor]: best peak / best peak
     *  more than [exclude] frames away (adpatch.py `_peak_ratio`). */
    private fun peakRatio(ad: FloatArray, vid: FloatArray, factor: Double,
                          exclude: Int,
                          lags: List<Double> = emptyList()): Double {
        val stretched = stretch(ad, factor)
        val n = nextPow2(vid.size + stretched.size)
        val cc = crossCorrelate(null, stretched, n, fftOf(vid, n))
        var k = 0
        for (j in cc.indices) if (cc[j] > cc[k]) k = j
        var second = Float.NEGATIVE_INFINITY
        val centers = listOf(k) + lags.map {
            ((Math.round(it).toInt() % n) + n) % n
        }
        for (j in cc.indices) {
            val far = centers.all { cIdx ->
                minOf(abs(j - cIdx), n - abs(j - cIdx)) > exclude  // circular
            }
            if (far && cc[j] > second) second = cc[j]
        }
        if (second == Float.NEGATIVE_INFINITY) return 10.0
        return if (second > 0) minOf(10.0, (cc[k] / second).toDouble())
        else 10.0
    }

    private fun clamp01(x: Double) = x.coerceIn(0.0, 1.0)

    /** Overall confidence + band + warnings - verbatim port of
     *  adpatch.py confidence(); keep the two in sync. */
    fun confidence(s: Signals): Triple<Double, String, List<String>> {
        val cPeak = clamp01((s.peak - 0.03) / 0.15)
        val cUnique = clamp01((s.peakRatio - 1.05) / 0.6)
        val cCover = s.coverage
        val cResid = clamp01(1 - (s.residP95Ms - 40) / 160)
        val cDur = clamp01(1 - (abs(s.durRatio - 1) - 0.03) / 0.12)
        val score = Math.pow(cPeak, 0.25) * Math.pow(cUnique, 0.15) *
            Math.pow(cCover, 0.3) * Math.pow(cResid, 0.2) *
            Math.pow(cDur, 0.1)
        val band = when {
            score >= 0.75 -> "good"
            score >= 0.45 -> "check"
            else -> "likely wrong"
        }
        val w = ArrayList<String>()
        if (s.peak < 0.04 || s.coverage < 0.15)
            w.add("These files don't seem to share a soundtrack - is " +
                "this the AD file for this video?")
        else if (s.coverage < 0.3 && s.durRatio < 0.95)
            w.add("The AD file is much longer than the video and matches " +
                "poorly: probably extended audio description (pauses " +
                "the video), which a time shift can't fix.")
        else if (abs(s.durRatio - 1) > 0.03 && s.coverage < 0.7)
            w.add("The AD file looks like a different edit of the movie " +
                "(lengths differ and parts don't match). Some scenes may " +
                "be out of sync.")
        if (s.peakRatio < 1.2 && s.coverage >= 0.3)
            w.add("Repetitive audio: the offset could be wrong by a whole " +
                "repeat - spot-check the start.")
        if (s.residP95Ms > 120 && s.coverage >= 0.3)
            w.add("Sync wobbles by up to %.0f ms in places."
                .format(s.residP95Ms))
        return Triple(Math.round(score * 1000) / 1000.0, band, w)
    }


    // ------------------------------------------------- piecewise (edits)
    // Port of adpatch.py's _fit_piecewise() and helpers - keep in sync.
    // video_time = a * ad_time + b_k within piece k (different edits).

    private const val PW_WIN_S = 20.0
    private const val PW_HOP_S = 10.0
    private const val PW_MIN_RUN = 3
    private const val PW_MAX_PIECES = 64

    data class Piece(val adStart: Double, val adEnd: Double, val b: Double,
                     val windows: Int)

    /** One placed stretch of AD: AD seconds [adFrom, adTo) starts at
     *  video second [at]. */
    data class Placed(val adFrom: Double, val adTo: Double, val at: Double)

    /** Render plan: where each piece's AD goes on the video timeline; a
     *  piece running into the next one's start is cut there (the later
     *  piece wins - an insertion gets dropped). Mirrors plan_pieces(). */
    fun planPieces(a: Double, pieces: List<Piece>): List<Placed> {
        val placed = ArrayList<DoubleArray>()   // s0, s1, t0, t1
        for ((i, q) in pieces.withIndex()) {
            var t0 = a * q.adStart + q.b
            var t1 = a * q.adEnd + q.b
            if (i + 1 < pieces.size) {
                val n = pieces[i + 1]
                t1 = min(t1, a * n.adStart + n.b)
            }
            if (placed.isNotEmpty()) t0 = max(t0, placed.last()[3])
            if (t1 - t0 < 0.05) continue
            placed.add(doubleArrayOf((t0 - q.b) / a, (t1 - q.b) / a, t0, t1))
        }
        return placed.mapNotNull { (s0in, s1, t0, _) ->
            var s0 = s0in
            var d0 = t0
            if (d0 < 0) { s0 += -d0 / a; d0 = 0.0 }
            if (s1 - s0 > 0.01) Placed(s0, s1, d0) else null
        }
    }

    private class Cand(val off: Double, val score: Float)

    /** Top-3 video offsets per AD window by normalized correlation at
     *  20 Hz against the whole video (parallel over windows). */
    private fun windowCandidates(st: FloatArray, vid: FloatArray,
                                 starts: IntArray, poolF: Int,
                                 win: Int): List<List<Cand>> {
        val st20 = pool(st, poolF)
        val v20 = pool(vid, poolF)
        val l = win / poolF
        val valid = v20.size - l + 1
        if (valid <= 0) return starts.map { emptyList() }
        val n = nextPow2(v20.size + l)
        val vFft = fftOf(v20, n)
        val csum = DoubleArray(v20.size + 1)
        for (i in v20.indices) csum[i + 1] = csum[i] +
            v20[i].toDouble() * v20[i]
        val vnorm = FloatArray(valid) {
            sqrt(max(0.0, csum[it + l] - csum[it])).toFloat()
        }
        val nms = (1 * AudioEngine.FPS / poolF).toInt()
        val out = arrayOfNulls<List<Cand>>(starts.size)
        val next = java.util.concurrent.atomic.AtomicInteger(0)
        val workers = minOf(Runtime.getRuntime().availableProcessors(), 4)
        (0 until workers).map {
            Thread {
                while (true) {
                    if (AudioEngine.cancelRequested) break
                    val wi = next.getAndIncrement()
                    if (wi >= starts.size) break
                    val u = starts[wi] / poolF
                    if (u + l > st20.size) { out[wi] = emptyList(); continue }
                    val seg = st20.copyOfRange(u, u + l)
                    val sn = norm(seg)
                    if (sn == 0f) { out[wi] = emptyList(); continue }
                    val cc = crossCorrelate(null, seg, n, vFft)
                    val sc = FloatArray(valid) {
                        cc[it] / (sn * vnorm[it] + 1e-9f)
                    }
                    val cands = ArrayList<Cand>()
                    repeat(3) {
                        var j = 0
                        for (k in sc.indices) if (sc[k] > sc[j]) j = k
                        if (sc[j] <= 0f) return@repeat
                        cands.add(Cand((j * poolF - starts[wi]).toDouble(),
                            sc[j]))
                        for (k in max(0, j - nms)..min(valid - 1, j + nms))
                            sc[k] = -1f
                    }
                    out[wi] = cands
                }
            }.apply { start() }
        }.forEach { it.join() }
        if (AudioEngine.cancelRequested) throw RuntimeException("cancelled")
        return out.map { it ?: emptyList() }
    }

    private fun offsetFamilies(cands: List<List<Cand>>, baseOff: Double,
                               tol: Double): List<Double> {
        val best = cands.filter { it.isNotEmpty() }.map { it[0].score }
        if (best.isEmpty()) return listOf(baseOff)
        val thr = max(0.12, 0.5 * best.sorted()[best.size / 2])
        val pts = cands.flatMapIndexed { w, cs ->
            cs.filter { it.score >= thr }.map { it.off to w }
        }.sortedBy { it.first }
        val groups = ArrayList<MutableList<Pair<Double, Int>>>()
        for (pt in pts) {
            if (groups.isEmpty() || pt.first - groups.last().last().first > tol)
                groups.add(ArrayList())
            groups.last().add(pt)
        }
        val fams = groups.filter { g ->
            g.map { it.second }.toSet().size >= PW_MIN_RUN
        }.map { g -> g.map { it.first }.sorted().let { it[it.size / 2] } }
            .toMutableList()
        if (fams.all { abs(it - baseOff) > tol }) fams.add(baseOff)
        return fams.sorted()
    }

    private fun viterbi(scores: Array<DoubleArray>, jump: Double): IntArray {
        val nW = scores.size
        val nF = scores[0].size
        var dp = scores[0].copyOf()
        val back = Array(nW) { IntArray(nF) }
        for (i in 1 until nW) {
            var g = 0
            for (f in 0 until nF) if (dp[f] > dp[g]) g = f
            val sw = dp[g] - jump
            val nd = DoubleArray(nF)
            for (f in 0 until nF) {
                if (sw > dp[f]) { back[i][f] = g; nd[f] = sw }
                else { back[i][f] = f; nd[f] = dp[f] }
                nd[f] += scores[i][f]
            }
            dp = nd
        }
        val path = IntArray(nW)
        var f = 0
        for (k in 0 until nF) if (dp[k] > dp[f]) f = k
        path[nW - 1] = f
        for (i in nW - 1 downTo 1) { f = back[i][f]; path[i - 1] = f }
        return path
    }

    /** (family, firstWindow, lastWindow) runs. */
    private fun runs(path: IntArray): List<IntArray> {
        val r = ArrayList<IntArray>()
        var start = 0
        for (i in 1..path.size) {
            if (i == path.size || path[i] != path[start]) {
                r.add(intArrayOf(path[start], start, i - 1)); start = i
            }
        }
        return r
    }

    private fun vetRuns(pathIn: IntArray, scores: Array<DoubleArray>): IntArray {
        val path = pathIn.copyOf()
        while (true) {
            val rs = runs(path)
            val short = rs.filter { it[2] - it[1] + 1 < PW_MIN_RUN }
            if (rs.size == 1 || short.isEmpty()) return path
            val r = short.minByOrNull { it[2] - it[1] }!!
            val idx = rs.indexOf(r)
            val options = listOf(idx - 1, idx + 1)
                .filter { it in rs.indices }.map { rs[it][0] }
            val best = options.maxByOrNull { f ->
                (r[1]..r[2]).sumOf { scores[it][f] }
            }!!
            for (i in r[1]..r[2]) path[i] = best
        }
    }

    private fun sim(st: FloatArray, vid: FloatArray, p: Int, len: Int,
                    off: Double): Double {
        val v0 = p + Math.round(off).toInt()
        if (p < 0 || p + len > st.size || v0 < 0 || v0 + len > vid.size)
            return 0.0
        var dot = 0.0; var na = 0.0; var nv = 0.0
        for (i in 0 until len) {
            val x = st[p + i].toDouble(); val y = vid[v0 + i].toDouble()
            dot += x * y; na += x * x; nv += y * y
        }
        return dot / (sqrt(na) * sqrt(nv) + 1e-9)
    }

    /** Hand-over frame from piece A to B in [lo, hi); ties -> latest.
     *  Returns (frame, strength threshold) - see _split_point(). */
    private fun splitPoint(st: FloatArray, vid: FloatArray, lo: Int, hi: Int,
                           offA: Double, offB: Double, probe: Int, step: Int,
                           thrIn: Double?): Pair<Int, Double> {
        val ps = ArrayList<Int>()
        var p = lo
        while (p < max(lo + 1, hi - probe)) { ps.add(p); p += step }
        val sa = ps.map { sim(st, vid, it, probe, offA) }
        val sb = ps.map { sim(st, vid, it, probe, offB) }
        val mx = sa.indices.map { max(sa[it], sb[it]) }
        val thr = thrIn ?: (0.4 * percentile(mx.sorted(), 90.0))
        val c = DoubleArray(ps.size + 1)
        for (i in ps.indices)
            c[i + 1] = c[i] + if (mx[i] >= thr) sa[i] - sb[i] else 0.0
        val total = c[ps.size]
        var best = Double.NEGATIVE_INFINITY
        for (v in c) best = max(best, 2 * v - total)
        var t = 0
        for (i in c.indices) if (2 * c[i] - total >= best - 1e-9) t = i
        val frame = when {
            t >= ps.size -> ps.last() + probe
            t > 0 -> ps[t] + probe / 2
            else -> ps[0]
        }
        return frame to thr
    }

    /** numpy-style percentile (linear interpolation) of sorted values. */
    private fun percentile(sorted: List<Double>, q: Double): Double {
        if (sorted.isEmpty()) return 0.0
        val pos = q / 100 * (sorted.size - 1)
        val lo = floor(pos).toInt()
        val hi = min(lo + 1, sorted.size - 1)
        return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo)
    }

    /** Solve the small dense system m * x = r (Gaussian elimination). */
    private fun solve(m: Array<DoubleArray>, r: DoubleArray): DoubleArray? {
        val n = r.size
        for (col in 0 until n) {
            var piv = col
            for (i in col + 1 until n)
                if (abs(m[i][col]) > abs(m[piv][col])) piv = i
            if (abs(m[piv][col]) < 1e-12) return null
            val tm = m[col]; m[col] = m[piv]; m[piv] = tm
            val tr = r[col]; r[col] = r[piv]; r[piv] = tr
            for (i in col + 1 until n) {
                val f = m[i][col] / m[col][col]
                for (j in col until n) m[i][j] -= f * m[col][j]
                r[i] -= f * r[col]
            }
        }
        val x = DoubleArray(n)
        for (i in n - 1 downTo 0) {
            var s = r[i]
            for (j in i + 1 until n) s -= m[i][j] * x[j]
            x[i] = s / m[i][i]
        }
        return x
    }

    private class PwResult(val a: Double, val bFrames: Double,
                           val report: List<CheckPoint>, val used: List<Boolean>,
                           val coverage: Double, val residP95: Double,
                           val fams: List<Double>, val pieces: List<Piece>)

    private fun fitPiecewise(st: FloatArray, vid: FloatArray, a0: Double,
                             poolF: Int, linCoverage: Double,
                             log: (String) -> Unit): PwResult? {
        val fps = AudioEngine.FPS
        val win = (PW_WIN_S * fps).toInt()
        val hop = (PW_HOP_S * fps).toInt()
        val usable = st.size - win
        if (usable < hop * PW_MIN_RUN) return null
        val starts = IntArray(usable / hop + 1) { it * hop }
        val cands = windowCandidates(st, vid, starts, poolF, win)
        val base = cands.filter { it.isNotEmpty() }
            .maxByOrNull { it[0].score }?.get(0)?.off ?: 0.0
        val fams = offsetFamilies(cands, base, 0.3 * fps)
        if (fams.size < 2) return null

        val nW = starts.size
        val nF = fams.size
        val scores = Array(nW) { DoubleArray(nF) }
        val pos = Array(nW) { DoubleArray(nF) { Double.NaN } }
        val search = (0.5 * fps).toInt()
        for (i in 0 until nW) for (f in 0 until nF) {
            segmentMatch(st, vid, starts[i], win, starts[i] + fams[f],
                search)?.let {
                pos[i][f] = it.vpos; scores[i][f] = max(0.0, it.score.toDouble())
            }
        }
        val bestSingle = (0 until nF).maxOf { f ->
            (0 until nW).count { scores[it][f] > MIN_SEGMENT_SCORE }
        }.toDouble() / nW
        val rowMax = (0 until nW).map { i -> scores[i].maxOrNull()!! }.sorted()
        val jump = max(0.2, 1.5 * rowMax[rowMax.size / 2])
        val path = vetRuns(viterbi(scores, jump), scores)
        val rs = runs(path)
        val covered = (0 until nW).count {
            scores[it][path[it]] > MIN_SEGMENT_SCORE
        }.toDouble() / nW
        if (rs.size < 2 || rs.size > PW_MAX_PIECES ||
            covered < max(bestSingle, linCoverage) + 0.1) return null

        val bounds = arrayListOf(0)
        for (k in 0 until rs.size - 1) {
            val fa = rs[k][0]; val fb = rs[k + 1][0]
            val lo = max(bounds.last(), starts[rs[k][2]] - hop)
            val hi = min(st.size, starts[rs[k + 1][1]] + win + hop)
            val (x1, thr) = splitPoint(st, vid, lo, hi, fams[fa], fams[fb],
                (4 * fps).toInt(), (0.25 * fps).toInt(), null)
            val (x2, _) = splitPoint(st, vid, max(0, x1 - (2.5 * fps).toInt()),
                x1 + (2.5 * fps).toInt(), fams[fa], fams[fb],
                (1 * fps).toInt(), (0.05 * fps).toInt(), thr)
            bounds.add(max(bounds.last() + 1, x2))
        }
        bounds.add(st.size)

        // joint weighted fit: shared slope a2, one intercept per piece
        class Pt(val u: Double, val y: Double, val w: Double, val k: Int)
        val pts = ArrayList<Pt>()
        for ((k, r) in rs.withIndex()) for (i in r[1]..r[2]) {
            val u = starts[i]
            if (scores[i][r[0]] > MIN_SEGMENT_SCORE && bounds[k] <= u &&
                u + win <= bounds[k + 1])
                pts.add(Pt(u.toDouble(), pos[i][r[0]], scores[i][r[0]], k))
        }
        val nP = rs.size
        var a2 = 1.0
        val c = DoubleArray(nP) { fams[rs[it][0]] }
        var used: List<Pt> = pts
        repeat(2) {
            if (used.size < 2) return@repeat
            val present = (0 until nP).filter { k -> used.any { it.k == k } }
            val col = present.withIndex().associate { it.value to it.index + 1 }
            val dim = present.size + 1
            val m = Array(dim) { DoubleArray(dim) }
            val rhs = DoubleArray(dim)
            for (q in used) {
                val row = DoubleArray(dim)
                row[0] = q.u; row[col[q.k]!!] = 1.0
                for (i in 0 until dim) {
                    rhs[i] += q.w * row[i] * q.y
                    for (j in 0 until dim) m[i][j] += q.w * row[i] * row[j]
                }
            }
            val sol = solve(m, rhs) ?: return@repeat
            a2 = sol[0]
            for (k in present) c[k] = sol[col[k]!!]
            val resid = used.map { abs(it.y - (a2 * it.u + c[it.k])) }
            val med = resid.sorted()[resid.size / 2]
            val cut = max(3 * med + 1e-9, 0.05 * fps)
            val kept = used.filterIndexed { i, _ -> resid[i] < cut }
            if (kept.size == used.size || kept.size < 2) return@repeat
            used = kept
        }

        val a = a0 * a2
        val pieces = (0 until nP).map { k ->
            Piece(bounds[k] / a0 / fps, bounds[k + 1] / a0 / fps, c[k] / fps,
                pts.count { it.k == k })
        }
        val longest = (0 until nP).maxByOrNull { bounds[it + 1] - bounds[it] }!!
        val usedSet = used.map { it.u.toInt() to it.k }.toSet()
        val report = ArrayList<CheckPoint>()
        val usedFlags = ArrayList<Boolean>()
        for ((k, r) in rs.withIndex()) for (i in r[1]..r[2]) {
            val y = pos[i][r[0]]
            if (y.isNaN()) continue
            val centerAd = (starts[i] + win / 2) / a0
            report.add(CheckPoint(centerAd / fps, (a * centerAd + c[k]) / fps,
                scores[i][r[0]].toFloat(),
                (y - (a2 * starts[i] + c[k])) / fps * 1000))
            usedFlags.add((starts[i] to k) in usedSet)
        }
        val residUsed = report.indices.filter { usedFlags[it] }
            .map { abs(report[it].residualMs) }.sorted()
        log("different edit: $nP pieces " + pieces.joinToString(", ") {
            "%.1f-%.1fm (%+.2fs)".format(it.adStart / 60, it.adEnd / 60, it.b)
        })
        return PwResult(a, c[longest], report, usedFlags, covered,
            if (residUsed.isEmpty()) 1000.0 else percentile(residUsed, 95.0),
            fams, pieces)
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
        val t0 = android.os.SystemClock.elapsedRealtime()
        fun elapsed() =
            "${(android.os.SystemClock.elapsedRealtime() - t0) / 1000}s"

        // Stage 1: coarse (0.2%) then fine (0.02%) speed grid at 20 Hz.
        val poolF = 5
        val ad20 = pool(adEnv, poolF)
        val vid20 = pool(vidEnv, poolF)
        val coarse = DoubleArray(51) { 0.95 + it * 0.002 }
        val coarseScores = ArrayList<Pair<Double, Float>>()
        var a = speedScan(ad20, vid20, coarse, coarseScores) {
            onProgress(it * 0.6f)
        }.factor
        val fine = DoubleArray(21) { a - 0.002 + it * 0.0002 }
        var scan = speedScan(ad20, vid20, fine, null) {
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
                val s = speedScan(ad20, vid20, doubleArrayOf(special),
                    null) {}
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
        android.util.Log.d(TAG, "speed scan done at ${elapsed()}: " +
            "a=$a lag20=${scan.lag} peak=${scan.score}")

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

        android.util.Log.d(TAG, "segment fit done at ${elapsed()}: " +
            "${matches.size} matches, ${confident.size} confident, " +
            "a2=$a2 b2=$b2")
        val finalA = a0 * a2
        val finalB = b2
        val used = confident.map { it.u }.toSet()
        var weak = 0
        val report = matches.map { m ->
            val residMs = (m.vpos - (a2 * m.u + b2)) / fps * 1000
            val centerAd = (m.u + segLen / 2) / a0
            if (m.score <= WEAK_SCORE) weak++
            CheckPoint(centerAd / fps,
                (finalA * centerAd + finalB) / fps, m.score, residMs)
        }
        val nConfident = matches.count { it.score > MIN_SEGMENT_SCORE }
        val residUsed = matches.filter { it.u in used }
            .map { abs((it.vpos - (a2 * it.u + b2)) / fps * 1000) }.sorted()
        val signals = Signals(
            peak = scan.score.toDouble(),
            peakRatio = peakRatio(ad20, vid20, a0, (2 * fps / poolF).toInt()),
            coverage = nConfident.toDouble() / max(1, matches.size),
            // numpy percentile(95) uses linear interpolation; match it
            residP95Ms = if (residUsed.isEmpty()) 1000.0 else {
                val pos = 0.95 * (residUsed.size - 1)
                val lo = floor(pos).toInt()
                val hi = min(lo + 1, residUsed.size - 1)
                residUsed[lo] + (residUsed[hi] - residUsed[lo]) * (pos - lo)
            },
            durRatio = vidEnv.size / max(1.0, finalA * adEnv.size))
        // different edit? same gate as adpatch.py fit_alignment()
        if (signals.coverage < 0.9 && signals.peak >= 0.04) {
            val pw = fitPiecewise(stretched, vidEnv, a0, poolF,
                signals.coverage, log)
            if (pw != null) {
                val pwSignals = signals.copy(
                    peakRatio = peakRatio(ad20, vid20, a0,
                        (2 * fps / poolF).toInt(), pw.fams.map { it / poolF }),
                    coverage = pw.coverage, residP95Ms = pw.residP95,
                    durRatio = 1.0)
                val (conf, band, warnings) = confidence(pwSignals)
                log("confidence %.2f (%s)".format(conf, band))
                warnings.forEach { log("⚠ $it") }
                val pwWeak = pw.report.count { it.score <= WEAK_SCORE }
                return Result(pw.a, pw.bFrames / fps, pw.report,
                    suspect = pwWeak > pw.report.size / 2 ||
                        band == "likely wrong",
                    confidence = conf, band = band, warnings = warnings,
                    signals = pwSignals, pieces = pw.pieces)
            }
        }
        val (conf, band, warnings) = confidence(signals)
        log("confidence %.2f (%s)".format(conf, band))
        warnings.forEach { log("⚠ $it") }
        return Result(finalA, finalB / fps, report,
            suspect = weak > matches.size / 2 || band == "likely wrong",
            confidence = conf, band = band, warnings = warnings,
            signals = signals)
    }
}
