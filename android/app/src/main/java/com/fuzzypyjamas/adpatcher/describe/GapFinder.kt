package com.fuzzypyjamas.adpatcher.describe

import ai.onnxruntime.OnnxTensor
import ai.onnxruntime.OrtEnvironment
import ai.onnxruntime.OrtSession
import android.content.Context
import android.net.Uri
import android.os.SystemClock
import com.fuzzypyjamas.adpatcher.AudioEngine
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.nio.FloatBuffer
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.exp
import kotlin.math.floor
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sin

/**
 * Finds narration gaps: stretches with no dialogue, long enough for a
 * description (docs/plan/10, A2). Speech model = pyannote
 * segmentation-3.0 (MIT, CNRS; ONNX export by sherpa-onnx), run with
 * ONNX Runtime on 10 s windows at 16 kHz. Silero VAD was tried first and
 * misses ~40-60 % of film dialogue under music.
 *
 * Streaming: the timeline is split into [workers] ranges of model
 * windows, each decoded by its own codec (AudioEngine.decodeAudio with a
 * range) into a rolling 16 kHz buffer, so memory stays ~1 MB per worker
 * whatever the film length.
 *
 * Rules mirror tools/gaps_ref.py (PC reference + scorer) - keep in sync.
 */
class GapFinder(
    context: Context,
    private val workers: Int = 4,
    threadsPerRun: Int = 2,
) : AutoCloseable {

    class Result(
        val speech: List<DoubleArray>,   // [start, end] seconds
        val gaps: List<DoubleArray>,
        val durationS: Double,
    ) {
        fun toJson(): JSONObject = JSONObject()
            .put("speech", ranges(speech)).put("gaps", ranges(gaps))
            .put("duration", durationS).put("engine", "pyannote")

        private fun ranges(r: List<DoubleArray>) = JSONArray().apply {
            r.forEach { put(JSONArray().put(round3(it[0])).put(round3(it[1]))) }
        }

        companion object {
            fun fromJson(o: JSONObject): Result {
                fun rd(a: JSONArray) = (0 until a.length()).map {
                    val p = a.getJSONArray(it); doubleArrayOf(p.getDouble(0), p.getDouble(1))
                }
                return Result(rd(o.getJSONArray("speech")), rd(o.getJSONArray("gaps")),
                    o.getDouble("duration"))
            }
        }
    }

    private val env = OrtEnvironment.getEnvironment()
    // OrtSession.run is thread-safe: one session shared by all workers
    private val session: OrtSession = env.createSession(
        context.assets.open(MODEL_ASSET).use { it.readBytes() },
        OrtSession.SessionOptions().apply { setIntraOpNumThreads(threadsPerRun) })

    /** Wall ms of the last analyze() and ms summed inside the model. */
    var wallMs = 0L; private set
    @Volatile var modelMs = 0L; private set

    /** P(nobody speaks) -> P(speech) per output frame for one 10 s window. */
    private fun runModel(win: FloatArray): FloatArray {
        val t = SystemClock.elapsedRealtime()
        val p = OnnxTensor.createTensor(env, FloatBuffer.wrap(win), longArrayOf(1, 1, WIN.toLong()))
            .use { x ->
                session.run(mapOf("x" to x)).use { out ->
                    @Suppress("UNCHECKED_CAST")
                    val y = (out[0].value as Array<Array<FloatArray>>)[0]
                    FloatArray(y.size) { 1f - exp(y[it][0]) }   // powerset class 0 = silence
                }
            }
        synchronized(this) { modelMs += SystemClock.elapsedRealtime() - t }
        return p
    }

    /** Decodes the first audio track of [uri] and returns speech + gaps. */
    fun analyze(context: Context, uri: Uri, onProgress: (Float) -> Unit = {}): Result {
        val t0 = SystemClock.elapsedRealtime()
        modelMs = 0
        val (durUs, _) = AudioEngine.probeAudio(context, uri)
        val totalOut = durUs * SR / 1_000_000L
        val plan = WindowPlan(totalOut)
        val nw = if (durUs < 60_000_000L) 1 else workers
        val errors = arrayOfNulls<Throwable>(nw)
        val frac = FloatArray(nw)
        val threads = (0 until nw).map { w ->
            Thread {
                try {
                    val k0 = plan.windows * w / nw
                    val k1 = plan.windows * (w + 1) / nw
                    val last = w == nw - 1
                    val runner = RangeRunner(plan, k0, k1, ::runModel)
                    // decode from a bit before the first window (resampler
                    // warm-up) to the end of the last window
                    val fromUs = max(0L, k0 * STEP * 1_000_000L / SR - 500_000L)
                    val toUs = if (last) Long.MAX_VALUE - 1
                    else ((k1 - 1) * STEP + WIN) * 1_000_000L / SR + 100_000L
                    AudioEngine.decodeAudio(context, uri, rangeUs = fromUs..toUs) { c ->
                        if (runner.feed(c)) throw AudioEngine.StopDecode()
                        frac[w] = runner.progress()
                        onProgress(frac.sum() / nw)
                    }
                    runner.finish()
                    frac[w] = 1f
                    onProgress(frac.sum() / nw)
                } catch (t: Throwable) {
                    errors[w] = t
                }
            }.apply { start() }
        }
        threads.forEach { it.join() }
        errors.filterNotNull().firstOrNull()?.let { throw it }
        wallMs = SystemClock.elapsedRealtime() - t0
        return plan.result()
    }

    override fun close() {
        session.close()
    }

    /** All window probabilities of one film, averaged where windows overlap. */
    internal class WindowPlan(val totalOut: Long) {
        // same window count as gaps_ref.pyannote_probs: starts 0, STEP, ...
        // up to max(total - WIN, 0) inclusive
        val windows = (max(totalOut - WIN, 0L) / STEP + 1).toInt()
        private val nfr = (totalOut / HOP + 1).toInt()
        private val acc = FloatArray(nfr)
        private val cnt = IntArray(nfr)

        @Synchronized
        fun add(k: Int, p: FloatArray) {
            val f0 = (k.toLong() * STEP / HOP).toInt()
            for (i in 0 until min(p.size, nfr - f0)) { acc[f0 + i] += p[i]; cnt[f0 + i]++ }
        }

        fun probs() = FloatArray(nfr) { if (cnt[it] > 0) acc[it] / cnt[it] else 0f }

        fun result(): Result {
            val dur = totalOut / SR.toDouble()
            val speech = probsToSpeech(probs(), HOP / SR.toDouble())
            return Result(speech, gapsFromSpeech(speech, dur), dur)
        }
    }

    /** Runs windows [k0, k1) of [plan] as their audio arrives. */
    internal class RangeRunner(
        private val plan: WindowPlan, k0: Int, private val k1: Int,
        private val model: (FloatArray) -> FloatArray,
    ) {
        private var k = k0
        private val k0 = k0
        private val mono = Mono16k()
        private val win = FloatArray(WIN)

        /** Feed one decoded chunk; true once every window is done. */
        fun feed(c: AudioEngine.PcmChunk): Boolean {
            if (AudioEngine.cancelRequested) throw RuntimeException("cancelled")
            mono.feed(c)
            while (k < k1 && k.toLong() * STEP + WIN <= mono.endAbs) runWindow()
            return k >= k1
        }

        /** End of stream: remaining windows get zero padding. */
        fun finish() {
            mono.flush()
            while (k < k1) runWindow()
        }

        fun progress() = (k - k0).toFloat() / max(1, k1 - k0)

        private fun runWindow() {
            val s = k.toLong() * STEP
            mono.copy(s, win)
            plan.add(k, model(win))
            k++
            mono.discardBefore(k.toLong() * STEP)
        }
    }

    /**
     * Downmix + bandlimited resample to 16 kHz (windowed-sinc, 32 taps,
     * 256 phases, cutoff 0.45 x 16 kHz) into a rolling buffer addressed by
     * absolute output sample index. The first chunk's pts anchors the
     * timeline, so ranged decodes land on the global grid.
     */
    internal class Mono16k {
        private var buf = FloatArray(1 shl 18)
        private var bufBase = 0L      // absolute index of buf[0]
        private var bufLen = 0
        /** Absolute index one past the last output sample written. */
        val endAbs get() = bufBase + bufLen

        private var inRate = 0
        private var ratio = 1.0
        private var hist = FloatArray(1 shl 16)  // recent mono input
        private var histStart = 0L               // absolute input index of hist[0]
        private var histLen = 0
        private var nextOut = 0L                 // next absolute output index
        private var table = FloatArray(0)

        fun feed(c: AudioEngine.PcmChunk) {
            val ch = c.channels
            val frames = c.samples.size / ch
            if (frames == 0) return
            if (inRate == 0) start(c)
            if (histLen + frames > hist.size) compactHist(frames)
            val skipLfe = ch >= 6     // 5.1/7.1: channel 3 is LFE
            val scale = 1f / ((if (skipLfe) ch - 1 else ch) * 32768f)
            val smp = c.samples
            var o = histLen
            var s = 0
            for (f in 0 until frames) {
                var v = 0f
                for (j in 0 until ch) {
                    if (!(skipLfe && j == 3)) v += smp[s]
                    s++
                }
                hist[o++] = v * scale
            }
            histLen = o
            emit()
        }

        /** End of stream: emit what the remaining input supports, zero-padded. */
        fun flush() {
            if (inRate == 0) return
            if (histLen + TAPS > hist.size) compactHist(TAPS)
            for (i in 0 until TAPS) hist[histLen++] = 0f
            emit()
        }

        private fun start(c: AudioEngine.PcmChunk) {
            inRate = c.sampleRate
            ratio = inRate / SR.toDouble()
            buildTable()
            // absolute input index of this chunk's first sample; outputs are
            // a pure function of absolute input indices, so ranged decodes
            // agree with a full decode once past the zero-history warm-up
            val in0 = Math.round(max(0L, c.ptsUs) * inRate / 1e6)
            val lead = kotlin.math.ceil(in0 / ratio).toLong()
            nextOut = lead
            bufBase = lead
            histStart = in0 - TAPS
            histLen = TAPS                              // zeros
        }

        private fun compactHist(extra: Int) {
            // drop input older than the next output's first tap
            val need = floor(nextOut * ratio).toLong() - TAPS
            val drop = (need - histStart).toInt().coerceIn(0, histLen)
            System.arraycopy(hist, drop, hist, 0, histLen - drop)
            histLen -= drop; histStart += drop
            if (histLen + extra > hist.size) hist = hist.copyOf(histLen + extra + 4096)
        }

        private fun emit() {
            val half = TAPS / 2
            val h = hist
            val tab = table
            val inEnd = histStart + histLen
            while (true) {
                val t = nextOut * ratio
                val base = floor(t).toLong()
                if (base + half >= inEnd) break
                val ph = ((t - base) * PHASES).toInt().coerceAtMost(PHASES - 1)
                val i0 = (base - half + 1 - histStart).toInt()
                val row = ph * TAPS
                var acc = 0f
                for (k in 0 until TAPS) acc += h[i0 + k] * tab[row + k]
                if (bufLen == buf.size) buf = buf.copyOf(buf.size * 2)
                buf[bufLen++] = acc
                nextOut++
            }
        }

        /** Copy [dst.size] samples from absolute [from]; zeros where unavailable. */
        fun copy(from: Long, dst: FloatArray) {
            dst.fill(0f)
            val a = max(from, bufBase)
            val b = min(from + dst.size, endAbs)
            if (b > a) System.arraycopy(buf, (a - bufBase).toInt(), dst, (a - from).toInt(), (b - a).toInt())
        }

        fun discardBefore(abs: Long) {
            val d = (abs - bufBase).toInt().coerceIn(0, bufLen)
            if (d == 0) return
            System.arraycopy(buf, d, buf, 0, bufLen - d)
            bufLen -= d; bufBase += d
        }

        /** Everything still buffered (tests / short clips). */
        fun snapshot(): Pair<Long, FloatArray> = bufBase to buf.copyOf(bufLen)

        private fun buildTable() {
            // lowpass at 0.45*16k relative to the input rate (capped at the
            // input Nyquist for inputs already <= 16 kHz)
            val fc = min(0.45 * SR, 0.5 * inRate) / inRate
            table = FloatArray(PHASES * TAPS)
            val half = TAPS / 2
            for (p in 0 until PHASES) {
                val frac = p / PHASES.toDouble()
                var sum = 0.0
                val row = DoubleArray(TAPS)
                for (k in 0 until TAPS) {
                    val d = (k - half + 1) - frac           // tap distance in input samples
                    val sinc = if (d == 0.0) 2 * fc else sin(2 * PI * fc * d) / (PI * d)
                    val w = 0.5 + 0.5 * cos(PI * d / half)  // Hann over +-half
                    row[k] = if (abs(d) < half) sinc * w else 0.0
                    sum += row[k]
                }
                for (k in 0 until TAPS) table[p * TAPS + k] = (row[k] / sum).toFloat()
            }
        }
    }

    companion object {
        const val MODEL_ASSET = "pyannote-segmentation-3.0.int8.onnx"
        const val SR = 16000
        const val WIN = 160000      // 10 s
        const val STEP = 80000      // 50 % overlap
        const val HOP = 270         // samples per model output frame
        private const val TAPS = 32
        private const val PHASES = 256

        // gap rules - keep identical to tools/gaps_ref.py
        const val MIN_GAP_S = 1.5
        const val GAP_GUARD_S = 0.2
        const val THRESHOLD = 0.4f
        const val NEG_THRESHOLD = 0.28f
        const val MIN_SPEECH_S = 0.25
        const val MIN_SILENCE_S = 0.10

        private fun round3(v: Double) = Math.round(v * 1000) / 1000.0

        fun probsToSpeech(probs: FloatArray, hopS: Double): List<DoubleArray> {
            val speech = ArrayList<DoubleArray>()
            var start = -1.0
            var below = -1.0
            for (i in probs.indices) {
                val p = probs[i]
                val t = i * hopS
                if (start < 0) {
                    if (p >= THRESHOLD) { start = t; below = -1.0 }
                } else if (p < NEG_THRESHOLD) {
                    if (below < 0) below = t
                    if (t + hopS - below >= MIN_SILENCE_S) {
                        if (below - start >= MIN_SPEECH_S) speech += doubleArrayOf(start, below)
                        start = -1.0; below = -1.0
                    }
                } else {
                    below = -1.0
                }
            }
            val end = probs.size * hopS
            if (start >= 0 && end - start >= MIN_SPEECH_S) speech += doubleArrayOf(start, end)
            return speech
        }

        fun gapsFromSpeech(speech: List<DoubleArray>, duration: Double): List<DoubleArray> {
            val gaps = ArrayList<DoubleArray>()
            var t = 0.0
            for (s in speech + listOf(doubleArrayOf(duration + GAP_GUARD_S, duration))) {
                val g0 = if (t > 0) t + GAP_GUARD_S else 0.0
                val g1 = min(s[0] - GAP_GUARD_S, duration)
                if (g1 - g0 >= MIN_GAP_S) gaps += doubleArrayOf(g0, g1)
                t = max(t, s[1])
            }
            return gaps
        }

        fun load(f: File): Result = Result.fromJson(JSONObject(f.readText()))
    }
}
