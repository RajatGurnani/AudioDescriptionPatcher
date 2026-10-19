package com.fuzzypyjamas.adpatcher

import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.math.abs

/**
 * Python <-> Kotlin alignment parity on the synthetic harness media.
 *
 * Inputs come from tools/parity_export.py (envelopes computed by
 * adpatch.py, plus its own results), so this checks Aligner.kt's math
 * against fit_alignment() on identical envelopes. Skipped unless the
 * export exists:
 *
 *   gradle testReleaseUnitTest -Dparity.dir=%TEMP%\adpatch-parity
 */
class AlignerParityTest {

    private fun readF32(f: File): FloatArray {
        val bb = ByteBuffer.wrap(f.readBytes()).order(ByteOrder.LITTLE_ENDIAN)
        return FloatArray(bb.remaining() / 4) { bb.float }
    }

    @Test
    fun matchesPython() {
        val dir = File(System.getProperty("parity.dir") ?: "")
        assumeTrue("no parity export (see tools/parity_export.py)",
            File(dir, "expected.tsv").exists())
        val vid = readF32(File(dir, "video.f32"))
        val failures = ArrayList<String>()
        for (line in File(dir, "expected.tsv").readLines()) {
            val f = line.trim().split("\t")
            if (f.size < 4) continue
            val (name, aS, bS, band) = f
            val pieces = f.getOrNull(4)?.takeIf { it.isNotEmpty() }
                ?.split(",")?.map { p ->
                    p.split(":").let { it[0].toDouble() to it[1].toDouble() }
                } ?: emptyList()
            val ad = readF32(File(dir, "ad_$name.f32"))
            val r = Aligner.fitAlignment(ad, vid, {}, {})
            val da = (r.a - aS.toDouble()) * 1e6
            val db = (r.bSec - bS.toDouble()) * 1000
            val msg = StringBuilder("%-10s da %+6.1f ppm  db %+7.1f ms  %s/%s  pieces %d/%d"
                .format(name, da, db, r.band, band, r.pieces.size, pieces.size))
            var ok = r.band == band && r.pieces.size == pieces.size
            if (band != "likely wrong") ok = ok && abs(da) < 5 && abs(db) < 5
            for ((i, p) in pieces.withIndex()) {
                val q = r.pieces.getOrNull(i) ?: break
                val dStart = q.adStart - p.first
                val dB = (q.b - p.second) * 1000
                msg.append("  [%+.2fs %+.1fms]".format(dStart, dB))
                ok = ok && abs(dStart) < 0.5 && abs(dB) < 5
            }
            println((if (ok) "ok   " else "FAIL ") + msg)
            if (!ok) failures.add(name)
        }
        assert(failures.isEmpty()) { "parity failures: $failures" }
    }
}
