package com.fuzzypyjamas.adpatcher.describe

import ai.onnxruntime.OnnxTensor
import ai.onnxruntime.OrtEnvironment
import ai.onnxruntime.OrtSession
import android.content.Context
import android.net.Uri
import com.fuzzypyjamas.adpatcher.AudioEngine
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.nio.FloatBuffer
import kotlin.math.PI
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
 * Rules mirror tools/gaps_ref.py (PC reference + scorer) - keep in sync.
 */
class GapFinder(context: Context) : AutoCloseable {

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
    private val session: OrtSession = env.createSession(
        context.assets.open(MODEL_ASSET).use { it.readBytes() },
        OrtSession.SessionOptions().apply { setIntraOpNumThreads(4) })

    /** Decodes the first audio track of [uri] and returns speech + gaps. */
    fun analyze(context: Context, uri: Uri, onProgress: (Float) -> Unit = {}): Result {
        val mono = Mono16k()
        AudioEngine.decodeAudio(context, uri, onProgress = onProgress) { mono.feed(it) }
        val x = mono.finish()
        return fromSamples(x)
    }

    /** Same, from 16 kHz mono samples (tests/probes). */
    fun fromSamples(x: FloatArray): Result {
        val probs = speechProbs(x)
        val dur = x.size / SR.toDouble()
        val speech = probsToSpeech(probs, HOP / SR.toDouble())
        return Result(speech, gapsFromSpeech(speech, dur), dur)
    }

    /** P(anyone speaking) per 270-sample frame; 10 s windows, 50 % overlap, mean. */
    private fun speechProbs(x: FloatArray): FloatArray {
        val nfr = x.size / HOP + 1
        val acc = FloatArray(nfr)
        val cnt = IntArray(nfr)
        val win = FloatArray(WIN)
        val shape = longArrayOf(1, 1, WIN.toLong())
        var s = 0
        while (true) {
            if (AudioEngine.cancelRequested) throw RuntimeException("cancelled")
            win.fill(0f)
            System.arraycopy(x, s, win, 0, min(WIN, x.size - s))
            OnnxTensor.createTensor(env, FloatBuffer.wrap(win), shape).use { t ->
                session.run(mapOf("x" to t)).use { out ->
                    @Suppress("UNCHECKED_CAST")
                    val y = (out[0].value as Array<Array<FloatArray>>)[0]
                    val f0 = s / HOP
                    for (i in 0 until min(y.size, nfr - f0)) {
                        // powerset class 0 = nobody speaks
                        acc[f0 + i] += 1f - exp(y[i][0])
                        cnt[f0 + i]++
                    }
                }
            }
            if (s + WIN >= x.size) break
            s += STEP
        }
        for (i in 0 until nfr) if (cnt[i] > 0) acc[i] /= cnt[i]
        return acc
    }

    override fun close() {
        session.close()
    }

    /**
     * Downmix + bandlimited resample to 16 kHz (windowed-sinc, 32 taps,
     * 256 phases, cutoff 0.45 x output rate). Places samples by chunk pts
     * so leading silence/priming offsets land on the right timeline.
     */
    internal class Mono16k {
        private var out = FloatArray(1 shl 20)
        private var n = 0
        private var inRate = 0
        private var hist = FloatArray(0)      // recent mono input samples
        private var histStart = 0L           // absolute input index of hist[0]
        private var histLen = 0
        private var inPos = 0L               // absolute input samples received
        private var nextOut = 0L             // next output sample index
        private var table = FloatArray(0)
        private var ratio = 1.0

        fun feed(c: AudioEngine.PcmChunk) {
            if (inRate == 0) {
                inRate = c.sampleRate
                ratio = inRate / SR.toDouble()
                buildTable()
                hist = FloatArray(1 shl 16)
                // pts of the first chunk = where the track starts
                val lead = (c.ptsUs * SR / 1_000_000L).toInt().coerceAtLeast(0)
                ensure(lead); n = lead; nextOut = lead.toLong()
                inPos = (nextOut * ratio).toLong(); histStart = inPos
            }
            val ch = c.channels
            val frames = c.samples.size / ch
            if (histLen + frames > hist.size) {
                // keep only the taps' worth of history
                val keep = TAPS + 2
                val drop = max(0, histLen - keep)
                System.arraycopy(hist, drop, hist, 0, histLen - drop)
                histLen -= drop; histStart += drop
                if (histLen + frames > hist.size) hist = hist.copyOf(histLen + frames + 4096)
            }
            for (f in 0 until frames) {
                var v = 0f
                var k = 0
                for (j in 0 until ch) {
                    if (ch >= 6 && j == 3) continue   // skip LFE in 5.1/7.1
                    v += c.samples[f * ch + j]; k++
                }
                hist[histLen++] = v / (k * 32768f)
            }
            inPos += frames
            emit()
        }

        private fun emit() {
            val half = TAPS / 2
            while (true) {
                val t = nextOut * ratio                 // input position
                val base = floor(t).toLong()
                if (base + half >= inPos) break
                val frac = t - base
                val ph = (frac * PHASES).toInt().coerceAtMost(PHASES - 1)
                var acc = 0f
                val row = ph * TAPS
                for (k in 0 until TAPS) {
                    val idx = (base - half + 1 + k - histStart).toInt()
                    if (idx in 0 until histLen) acc += hist[idx] * table[row + k]
                }
                ensure(n + 1); out[n++] = acc
                nextOut++
            }
        }

        private fun buildTable() {
            // lowpass at 0.45*16k relative to the input rate (no-op guard
            // for inputs already <= 16 kHz: cutoff capped at input Nyquist)
            val fc = min(0.45 * SR, 0.5 * inRate) / inRate
            table = FloatArray(PHASES * TAPS)
            val half = TAPS / 2
            for (p in 0 until PHASES) {
                val frac = p / PHASES.toDouble()
                var sum = 0.0
                for (k in 0 until TAPS) {
                    val d = (k - half + 1) - frac           // tap distance in input samples
                    val sinc = if (d == 0.0) 2 * fc else sin(2 * PI * fc * d) / (PI * d)
                    val w = 0.5 + 0.5 * cos(PI * d / half)  // Hann over +-half
                    val v = if (kotlin.math.abs(d) < half) sinc * w else 0.0
                    table[p * TAPS + k] = v.toFloat(); sum += v
                }
                for (k in 0 until TAPS) table[p * TAPS + k] = (table[p * TAPS + k] / sum).toFloat()
            }
        }

        private fun ensure(size: Int) {
            if (size > out.size) out = out.copyOf(max(size, out.size * 2))
        }

        fun finish(): FloatArray = out.copyOf(n)
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
