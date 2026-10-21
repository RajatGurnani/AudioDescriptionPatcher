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
        val y = m.finish()
        val sr = GapFinder.SR
        assertEquals("length (0.5 s lead + 3 s)", 3.5 * sr, y.size.toDouble(), 40.0)
        assertTrue("lead is silent", y.take(sr / 2 - 10).all { abs(it) < 1e-6 })
        val a440 = tone(440.0, y, sr, sr, 3 * sr)
        val a10k = tone(10000.0 * sr / sr, y, sr, sr, 3 * sr)  // aliases to 6 kHz if not filtered
        val alias = tone(6000.0, y, sr, sr, 3 * sr)
        assertEquals("440 Hz kept", 0.25, a440, 0.01)
        assertTrue("10 kHz alias suppressed: $alias", alias < 0.005)
        assertTrue(a10k < 0.01)
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
