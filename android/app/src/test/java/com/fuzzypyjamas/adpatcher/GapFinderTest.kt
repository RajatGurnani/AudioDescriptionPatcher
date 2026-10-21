package com.fuzzypyjamas.adpatcher

import com.fuzzypyjamas.adpatcher.describe.GapFinder
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * GapFinder (AI audio description, docs/plan/10 A2):
 * - resampler: 48 kHz stereo -> 16 kHz mono keeps speech-band tones,
 *   removes content above 7.2 kHz, preserves timing;
 * - rule parity with tools/gaps_ref.py on identical pyannote probs
 *   (skipped unless -Dparity.dir has NAME.probs.f32 + NAME.gaps.json).
 */
class GapFinderTest {

    private fun tone(f: Double, x: FloatArray, sr: Int, from: Int, to: Int): Double {
        // amplitude of frequency f in x[from, to) by single-bin DFT
        var re = 0.0; var im = 0.0
        for (i in from until to) {
            val w = 2 * PI * f * i / sr
            re += x[i] * kotlin.math.cos(w); im += x[i] * sin(w)
        }
        return 2 * sqrt(re * re + im * im) / (to - from)
    }

    @Test
    fun resamplerKeepsSpeechBandAndTiming() {
        val inRate = 48000
        val secs = 3
        val n = inRate * secs
        val pcm = ShortArray(n * 2)
        for (i in 0 until n) {
            val v = 0.25 * sin(2 * PI * 440.0 * i / inRate) + 0.25 * sin(2 * PI * 10000.0 * i / inRate)
            pcm[2 * i] = (v * 32767).toInt().toShort()
            pcm[2 * i + 1] = pcm[2 * i]
        }
        val m = GapFinder.Mono16k()
        // feed in uneven chunks, first one at pts 0.5 s
        var off = 0
        var ptsUs = 500_000L
        for (len in generateSequence(1000) { if (it > 7000) 1000 else it + 1700 }) {
            if (off >= n) break
            val k = minOf(len, n - off)
            m.feed(AudioEngine.PcmChunk(pcm.copyOfRange(2 * off, 2 * (off + k)), 2, inRate, ptsUs))
            ptsUs += k * 1_000_000L / inRate; off += k
        }
        m.flush()
        val (base, y) = m.snapshot()
        assertEquals("buffer starts at the pts lead", 8000L, base)
        val sr = GapFinder.SR
        assertEquals("length 3 s after the lead", 3.0 * sr, y.size.toDouble(), 40.0)
        val a440 = tone(440.0, y, sr, sr / 2, 5 * sr / 2)
        val a10k = tone(10000.0, y, sr, sr / 2, 5 * sr / 2)
        val alias = tone(6000.0, y, sr, sr / 2, 5 * sr / 2)  // 10 kHz folds to 6 kHz unfiltered
        assertEquals("440 Hz kept", 0.25, a440, 0.01)
        assertTrue("10 kHz alias suppressed: $alias", alias < 0.005)
        assertTrue(a10k < 0.01)
    }

    /** Synthetic 48 kHz stereo "film": tones + noise bursts, as PCM chunks. */
    private fun film(secs: Int): ShortArray {
        val r = java.util.Random(7)
        val n = 48000 * secs
        return ShortArray(n * 2) { i ->
            val t = i / 2 / 48000.0
            val v = 0.2 * sin(2 * PI * (200 + 50 * (t % 7)) * t) + if ((t % 3) < 1) 0.1 * r.nextGaussian() else 0.0
            (v.coerceIn(-1.0, 1.0) * 32767).toInt().toShort()
        }
    }

    /** Feed samples [fromIn, end) in 1024-frame chunks with pts, like a ranged decode. */
    private fun feedRange(pcm: ShortArray, fromIn: Int, sink: (AudioEngine.PcmChunk) -> Boolean) {
        var off = fromIn
        val n = pcm.size / 2
        while (off < n) {
            val k = minOf(1024, n - off)
            val c = AudioEngine.PcmChunk(pcm.copyOfRange(2 * off, 2 * (off + k)), 2, 48000, off * 1_000_000L / 48000)
            if (sink(c)) return
            off += k
        }
    }

    @Test
    fun parallelRangesEqualSingleRun() {
        val secs = 47
        val pcm = film(secs)
        // fake model: per-frame mean |x| of the window, so any sample
        // misplacement or seam glitch changes the result
        val model = { w: FloatArray ->
            FloatArray(589) { f ->
                var a = 0f
                val s0 = f * GapFinder.HOP
                for (i in s0 until minOf(s0 + 991, w.size)) a += abs(w[i])
                a / 991
            }
        }
        val total = secs * 16000L
        fun run(workers: Int): FloatArray {
            val plan = GapFinder.WindowPlan(total)
            for (w in 0 until workers) {
                val k0 = plan.windows * w / workers
                val k1 = plan.windows * (w + 1) / workers
                val rr = GapFinder.RangeRunner(plan, k0, k1, model)
                // like analyze(): start 0.5 s early, chunk-aligned (seek lands on a sync frame)
                val fromIn = (maxOf(0L, k0 * GapFinder.STEP * 3L - 24000L) / 1024 * 1024).toInt()
                feedRange(pcm, fromIn) { rr.feed(it) }
                rr.finish()
            }
            return plan.probs()
        }
        val one = run(1)
        val four = run(4)
        assertEquals(one.size, four.size)
        var maxd = 0f
        for (i in one.indices) maxd = maxOf(maxd, abs(one[i] - four[i]))
        assertTrue("4 ranges vs 1 run differ by $maxd", maxd < 1e-5f)
        assertTrue("windows produced output", one.any { it > 0.01f })
    }

    private fun readF32(f: File): FloatArray {
        val bb = ByteBuffer.wrap(f.readBytes()).order(ByteOrder.LITTLE_ENDIAN)
        return FloatArray(bb.remaining() / 4) { bb.float }
    }

    @Test
    fun rulesMatchPython() {
        val dir = File(System.getProperty("parity.dir") ?: "")
        val cases = dir.listFiles { f -> f.name.endsWith(".probs.f32") }.orEmpty()
        assumeTrue("no gap parity export (see tools/gaps_ref.py --probs-out)", cases.isNotEmpty())
        for (pf in cases) {
            val name = pf.name.removeSuffix(".probs.f32")
            val exp = GapFinder.Result.fromJson(JSONObject(File(dir, "$name.gaps.json").readText()))
            val speech = GapFinder.probsToSpeech(readF32(pf), GapFinder.HOP / GapFinder.SR.toDouble())
            val gaps = GapFinder.gapsFromSpeech(speech, exp.durationS)
            assertEquals("$name speech count", exp.speech.size, speech.size)
            assertEquals("$name gap count", exp.gaps.size, gaps.size)
            for ((a, b) in exp.gaps.zip(gaps)) {
                assertEquals("$name gap start", a[0], b[0], 0.002)
                assertEquals("$name gap end", a[1], b[1], 0.002)
            }
            println("$name: ${gaps.size} gaps match")
        }
    }
}
