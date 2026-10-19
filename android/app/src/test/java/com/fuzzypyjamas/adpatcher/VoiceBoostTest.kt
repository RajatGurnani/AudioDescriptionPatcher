package com.fuzzypyjamas.adpatcher

import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.io.RandomAccessFile
import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.exp
import kotlin.math.log10
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * Voice boost measurement on a synthetic clip with exact stems: a centred
 * speech-like voice (glottal pulses -> vowel formants, 4 Hz syllables,
 * phrases + pauses, fricative bursts) over a background (music: bass,
 * chords, kick, hats + pink noise). The mix is processed and the identical
 * gain trajectory is applied to each stem (VoiceBoost shadow buffers), so
 * voice-to-background ratio (VBR) before/after is exact.
 *
 * Cases: wide-stereo background (typical film mix) and mono background
 * (worst case: no centre/side difference to exploit).
 * -Dvb.out=<dir> also writes WAVs (mix / low / high) for listening.
 */
class VoiceBoostTest {
    private val fs = 48000
    private val secs = 30

    private class Bq(val b0: Double, val b1: Double, val b2: Double,
                     val a1: Double, val a2: Double) {
        var z1 = 0.0; var z2 = 0.0
        fun run(x: Double): Double {
            val y = b0 * x + z1
            z1 = b1 * x - a1 * y + z2; z2 = b2 * x - a2 * y
            return y
        }
    }

    private fun bandPass(f: Double, q: Double): Bq {
        val w = 2 * PI * f / fs
        val al = sin(w) / (2 * q)
        val a0 = 1 + al
        return Bq(al / a0, 0.0, -al / a0, -2 * cos(w) / a0, (1 - al) / a0)
    }

    private fun hp(f: Double) = pass(f, true)
    private fun lp(f: Double) = pass(f, false)
    private fun pass(f: Double, high: Boolean): Bq {
        val w = 2 * PI * f / fs
        val al = sin(w) / (2 * 0.7071)
        val c = cos(w)
        val a0 = 1 + al
        return if (high) Bq((1 + c) / 2 / a0, -(1 + c) / a0, (1 + c) / 2 / a0,
            -2 * c / a0, (1 - al) / a0)
        else Bq((1 - c) / 2 / a0, (1 - c) / a0, (1 - c) / 2 / a0,
            -2 * c / a0, (1 - al) / a0)
    }

    private var seed = 1234567L
    private fun rnd(): Double {
        seed = (seed * 6364136223846793005L + 1442695040888963407L)
        return ((seed ushr 11).toDouble() / (1L shl 53)) * 2 - 1
    }

    /** Mono speech-like voice. */
    private fun voice(n: Int): DoubleArray {
        val vowels = arrayOf(
            doubleArrayOf(730.0, 1090.0, 2440.0),
            doubleArrayOf(270.0, 2290.0, 3010.0),
            doubleArrayOf(300.0, 870.0, 2240.0),
            doubleArrayOf(530.0, 1840.0, 2480.0))
        val out = DoubleArray(n)
        var phase = 0.0
        var vi = 0
        var f = Array(3) { bandPass(vowels[0][it], 8.0) }
        val fric = hp(4000.0)
        for (i in 0 until n) {
            val t = i.toDouble() / fs
            val phrase = (t % 3.0) < 2.0            // 2 s talk, 1 s pause
            val syl = t * 4.0
            if (syl.toInt() % vowels.size != vi) {
                vi = syl.toInt() % vowels.size
                f = Array(3) { bandPass(vowels[vi][it], 8.0) }
            }
            val env = if (phrase) sin(PI * (syl % 1.0)).let { it * it } else 0.0
            val f0 = 120 + 15 * sin(2 * PI * 0.7 * t)
            phase += f0 / fs
            val pulse = if (phase >= 1) { phase -= 1; 1.0 } else 0.0
            var v = 0.0
            for ((k, bq) in f.withIndex()) v += bq.run(pulse) * (1.0 / (k + 1))
            // fricative at syllable starts
            val fr = if (phrase && (syl % 1.0) < 0.12)
                fric.run(rnd()) * 0.15 else fric.run(0.0)
            out[i] = (v * 6 + fr) * env
        }
        return out
    }

    /** Stereo background (interleaved); mono = same on both channels. */
    private fun background(n: Int, wide: Boolean): DoubleArray {
        val out = DoubleArray(n * 2)
        val notesL = doubleArrayOf(130.81, 164.81, 196.0, 261.63)
        val notesR = doubleArrayOf(146.83, 174.61, 220.0, 293.66)
        var pinkL = DoubleArray(3); var pinkR = DoubleArray(3)
        val hat = hp(7000.0)
        for (i in 0 until n) {
            val t = i.toDouble() / fs
            val chord = ((t / 2).toInt()) % 2
            fun pad(notes: DoubleArray): Double {
                var s = 0.0
                for (k in notes.indices) {
                    val fr = notes[k] * (if (chord == 1) 1.122 else 1.0)
                    for (h in 1..8)
                        s += sin(2 * PI * fr * h * t + k) / h
                }
                return s * 0.05
            }
            val bass = 0.25 * sin(2 * PI * 55 * t)
            val beat = t % 0.5
            val kick = 0.5 * exp(-beat * 30) * sin(2 * PI * 60 * beat)
            val hh = hat.run(rnd()) * exp(-((t + 0.25) % 0.5) * 60) * 0.3
            fun pink(st: DoubleArray): Double {   // cheap 3-pole pink-ish
                val w = rnd()
                st[0] = 0.99765 * st[0] + w * 0.0990460
                st[1] = 0.96300 * st[1] + w * 0.2965164
                st[2] = 0.57000 * st[2] + w * 1.0526913
                return (st[0] + st[1] + st[2] + w * 0.1848) * 0.03
            }
            val pl = pink(pinkL)
            val l = pad(notesL) + bass + kick + hh + pl
            val r = if (wide) pad(notesR) + bass + kick - hh + pink(pinkR)
            else l
            out[2 * i] = l; out[2 * i + 1] = r
        }
        return out
    }

    /** Speech-band (300-3400 Hz) and full-band energy of interleaved stereo. */
    private fun energy(x: FloatArray): Pair<Double, Double> {
        val f = arrayOf(hp(300.0), lp(3400.0), hp(300.0), lp(3400.0))
        var band = 0.0
        var full = 0.0
        for (i in 0 until x.size / 2) {
            for (c in 0..1) {
                val v = x[2 * i + c].toDouble()
                full += v * v
                val b = f[2 * c + 1].run(f[2 * c].run(v))
                band += b * b
            }
        }
        return band to full
    }

    private fun db(r: Double) = 10 * log10(r)

    private fun run(wide: Boolean, out: File?): String {
        val n = fs * secs
        seed = 1234567L
        val v = voice(n)
        val bg = background(n, wide)
        // set voice 10 dB under the background (hard to follow)
        var ev = 0.0; var eb = 0.0
        for (i in 0 until n) { ev += 2 * v[i] * v[i] }
        for (x in bg) eb += x * x
        val gv = sqrt(eb / ev * 10.0.pow(-10.0 / 10))
        val vs = FloatArray(n * 2) { (v[it / 2] * gv).toFloat() }
        val bs = FloatArray(n * 2) { bg[it].toFloat() }
        val peak = (0 until n * 2).maxOf { kotlin.math.abs(vs[it] + bs[it]) }
        val norm = 0.8f / peak
        for (i in vs.indices) { vs[i] *= norm; bs[i] *= norm }
        val mix = FloatArray(n * 2) { vs[it] + bs[it] }

        val (vb0, vf0) = energy(vs)
        val (bb0, bf0) = energy(bs)
        val sb = StringBuilder()
        sb.append("%s background: before  band VBR %+.1f dB, full %+.1f dB\n"
            .format(if (wide) "wide-stereo" else "mono", db(vb0 / bb0),
                db(vf0 / bf0)))
        out?.let { writeWav(File(it, "vb_${tag(wide)}_off.wav"), mix) }
        for (level in listOf(VoiceBoost.Level.LOW, VoiceBoost.Level.HIGH)) {
            val m = mix.copyOf(); val vv = vs.copyOf(); val bb = bs.copyOf()
            val fx = VoiceBoost(fs, level)
            val block = 1024
            var off = 0
            while (off < n) {
                val k = minOf(block, n - off)
                // process in encoder-sized blocks like the app does
                val mB = m.copyOfRange(2 * off, 2 * (off + k))
                val vB = vv.copyOfRange(2 * off, 2 * (off + k))
                val bB = bb.copyOfRange(2 * off, 2 * (off + k))
                fx.process(mB, k, arrayOf(vB, bB))
                System.arraycopy(mB, 0, m, 2 * off, 2 * k)
                System.arraycopy(vB, 0, vv, 2 * off, 2 * k)
                System.arraycopy(bB, 0, bb, 2 * off, 2 * k)
                off += k
            }
            // stems must add back up to the processed mix (exactness check)
            var err = 0.0
            for (i in m.indices) err = maxOf(err,
                kotlin.math.abs((m[i] - vv[i] - bb[i]).toDouble()))
            val (vb, vf) = energy(vv)
            val (bb1, bf) = energy(bb)
            val dBand = db(vb / bb1) - db(vb0 / bb0)
            val dFull = db(vf / bf) - db(vf0 / bf0)
            val pk = m.maxOf { kotlin.math.abs(it) }
            sb.append(("  %-4s  band VBR %+.1f dB (%+.1f), full %+.1f dB " +
                "(%+.1f), voice level %+.1f dB, peak %.3f, " +
                "stem err %.1e\n").format(
                    level.name.lowercase(), db(vb / bb1), dBand,
                    db(vf / bf), dFull, db(vf / vf0), pk, err))
            assertTrue("stems must sum to the mix", err < 1e-4)
            assertTrue("no clipping", pk <= 1.0f)
            assertTrue("$level must improve speech-band VBR", dBand > 0.5)
            out?.let {
                writeWav(File(it, "vb_${tag(wide)}_${level.name
                    .lowercase()}.wav"), m)
            }
        }
        return sb.toString()
    }

    private fun tag(wide: Boolean) = if (wide) "wide" else "mono"
    private fun Double.pow(e: Double) = Math.pow(this, e)

    private fun writeWav(f: File, x: FloatArray) {
        RandomAccessFile(f, "rw").use { r ->
            r.setLength(0)
            val bb = java.nio.ByteBuffer.allocate(44 + x.size * 2)
                .order(java.nio.ByteOrder.LITTLE_ENDIAN)
            bb.put("RIFF".toByteArray()).putInt(36 + x.size * 2)
            bb.put("WAVEfmt ".toByteArray()).putInt(16).putShort(1)
                .putShort(2).putInt(fs).putInt(fs * 4).putShort(4)
                .putShort(16)
            bb.put("data".toByteArray()).putInt(x.size * 2)
            for (v in x) bb.putShort((v * 32767).toInt()
                .coerceIn(-32768, 32767).toShort())
            r.write(bb.array())
        }
    }

    @Test
    fun voiceBoostImprovesSpeechToBackground() {
        val out = System.getProperty("vb.out")?.let { File(it).apply {
            mkdirs() } }
        val report = run(true, out) + run(false, out)
        println(report)
        out?.let { File(it, "vb_report.txt").writeText(report) }
    }
}
