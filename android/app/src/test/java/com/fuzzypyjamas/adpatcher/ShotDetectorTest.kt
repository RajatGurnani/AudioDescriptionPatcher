package com.fuzzypyjamas.adpatcher

import com.fuzzypyjamas.adpatcher.describe.CuePlanner
import com.fuzzypyjamas.adpatcher.describe.ShotDetector
import org.json.JSONObject
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * ShotDetector + CuePlanner (AI audio description, docs/plan/10 A3):
 * - cut rules == tools/shots_ref.py on identical per-frame scores
 *   (skipped unless -Dparity.dir has NAME.shotscores.f32 + .t + NAME.shots.json);
 * - cue planning on a hand-made timeline.
 */
class ShotDetectorTest {

    private fun bytes(f: File) = ByteBuffer.wrap(f.readBytes()).order(ByteOrder.LITTLE_ENDIAN)

    @Test
    fun cutRulesMatchPython() {
        val dir = File(System.getProperty("parity.dir") ?: "")
        val cases = dir.listFiles { f -> f.name.endsWith(".shotscores.f32") }.orEmpty()
        assumeTrue("no shot parity export (see tools/shots_ref.py --dump-scores)", cases.isNotEmpty())
        for (sf in cases) {
            val name = sf.name.removeSuffix(".shotscores.f32")
            val sb = bytes(sf); val s = FloatArray(sb.remaining() / 4) { sb.float }
            val tb = bytes(File(dir, "$name.shotscores.f32.t")); val t = DoubleArray(tb.remaining() / 8) { tb.double }
            val exp = JSONObject(File(dir, "$name.shots.json").readText()).getJSONArray("cuts")
            val cuts = ShotDetector.cutsFromScores(t, s)
            assertArrayEquals("$name cuts", DoubleArray(exp.length()) { exp.getDouble(it) },
                cuts.map { Math.round(it * 1000) / 1000.0 }.toDoubleArray(), 1e-6)
            println("$name: ${cuts.size} cuts match")
        }
    }

    @Test
    fun cuePlanning() {
        val shots = listOf(doubleArrayOf(0.0, 10.0), doubleArrayOf(10.0, 14.0),
            doubleArrayOf(14.0, 40.0), doubleArrayOf(40.0, 60.0))
        val gaps = listOf(
            doubleArrayOf(12.0, 15.0),   // sees shots 0 (via lookback), 1, 2
            doubleArrayOf(16.0, 18.0),   // only shot 2 again, short -> skipped
            doubleArrayOf(20.0, 32.0),   // long, stale, split in 2 x 6 s
            doubleArrayOf(41.0, 43.0))   // new shot 3
        val cues = CuePlanner.plan(gaps, shots)
        assertEquals(4, cues.size)
        assertArrayEquals(intArrayOf(0, 1, 2), cues[0].shotIds)
        assertTrue(cues[0].fresh)
        assertEquals(9.0, cues[0].frameTimes[0], 1e-9)      // mid of [8, 10]
        assertEquals(12.0, cues[0].frameTimes[1], 1e-9)     // mid of [10, 14]
        assertEquals(14.5, cues[0].frameTimes[2], 1e-9)     // mid of [14, 15]
        assertEquals(20.0, cues[1].start, 1e-9); assertEquals(26.0, cues[1].end, 1e-9)
        assertFalse(cues[1].fresh)
        assertEquals(26.0, cues[2].start, 1e-9)
        assertArrayEquals(intArrayOf(2, 3), cues[3].shotIds)   // lookback keeps the previous shot as context
        assertTrue(cues[3].fresh)
    }
}
