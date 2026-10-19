package com.fuzzypyjamas.adpatcher

import android.content.Context
import android.media.MediaCodec
import android.media.MediaFormat
import android.media.MediaMuxer
import android.net.Uri
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.exp
import kotlin.math.ln
import kotlin.math.log10
import kotlin.math.max
import kotlin.math.min
import kotlin.math.pow
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * Voice boost (dialogue clarity), streaming DSP on interleaved stereo float
 * PCM. Zero latency (no look-ahead), so sync is untouched. Chain:
 *   low-shelf cut (rumble/score bass) -> mid/side, side scaled down
 *   (dialogue + narration sit in the centre) -> presence peak on mid ->
 *   mid split into speech band (300-3400 Hz) + rest; while the speech
 *   detector fires, the rest + side are ducked -> RMS compressor ->
 *   peak limiter at -1 dBFS.
 * Biquads: RBJ Audio-EQ Cookbook formulas. See docs/plan/11-voice-boost.md.
 *
 * Every gain is computed from the main signal; [process] can apply the
 * identical (time-varying linear) chain to "shadow" buffers - the tests use
 * that to split the output into its voice and background parts exactly.
 */
class VoiceBoost(private val rate: Int, val level: Level) {
    enum class Level { OFF, LOW, HIGH }

    /** User setting: [level] plus where it applies. */
    data class Options(
        val level: Level = Level.OFF,
        val extraTrack: Boolean = true,   // add "original + boost" track
        val boostAd: Boolean = false      // process the AD track itself
    ) {
        val addsTrack get() = level != Level.OFF && extraTrack
        val boostsAd get() = level != Level.OFF && boostAd

        fun save(context: Context) {
            prefs(context).edit()
                .putString("vb_level", level.name)
                .putBoolean("vb_track", extraTrack)
                .putBoolean("vb_ad", boostAd)
                .apply()
        }

        override fun toString() =
            if (level == Level.OFF) "off"
            else "${level.name.lowercase()} (" + listOfNotNull(
                "extra track".takeIf { extraTrack },
                "AD track".takeIf { boostAd }).joinToString(" + ") + ")"

        companion object {
            private fun prefs(c: Context) =
                c.getSharedPreferences("voice_boost", Context.MODE_PRIVATE)

            fun load(context: Context): Options {
                val p = prefs(context)
                return Options(
                    runCatching {
                        Level.valueOf(p.getString("vb_level", "OFF")!!)
                    }.getOrDefault(Level.OFF),
                    p.getBoolean("vb_track", true),
                    p.getBoolean("vb_ad", false))
            }
        }
    }

    private class Params(
        val sideGain: Float, val shelfDb: Double, val peakDb: Double,
        val duckDb: Double, val ratio: Double, val threshDb: Double,
        val makeupDb: Double)

    private val prm = when (level) {
        Level.HIGH -> Params(0.35f, -6.0, 7.0, 8.0, 3.0, -30.0, 8.5)
        else -> Params(0.6f, -3.0, 4.0, 4.0, 2.0, -28.0, 5.5)
    }

    /** RBJ biquad, transposed direct form II, one channel. */
    private class Biquad(
        val b0: Double, val b1: Double, val b2: Double,
        val a1: Double, val a2: Double
    ) {
        private var z1 = 0.0
        private var z2 = 0.0
        fun copy() = Biquad(b0, b1, b2, a1, a2)
        fun run(x: Double): Double {
            val y = b0 * x + z1
            z1 = b1 * x - a1 * y + z2
            z2 = b2 * x - a2 * y
            return y
        }

        companion object {
            fun make(b0: Double, b1: Double, b2: Double,
                     a0: Double, a1: Double, a2: Double) =
                Biquad(b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0)

            fun lowShelf(fs: Int, f: Double, db: Double): Biquad {
                val a = 10.0.pow(db / 40)
                val w = 2 * PI * f / fs
                val cw = cos(w)
                val al = sin(w) / 2 * sqrt(2.0)   // shelf slope S = 1
                val sa = 2 * sqrt(a) * al
                return make(
                    a * ((a + 1) - (a - 1) * cw + sa),
                    2 * a * ((a - 1) - (a + 1) * cw),
                    a * ((a + 1) - (a - 1) * cw - sa),
                    (a + 1) + (a - 1) * cw + sa,
                    -2 * ((a - 1) + (a + 1) * cw),
                    (a + 1) + (a - 1) * cw - sa)
            }

            fun peak(fs: Int, f: Double, q: Double, db: Double): Biquad {
                val a = 10.0.pow(db / 40)
                val w = 2 * PI * f / fs
                val al = sin(w) / (2 * q)
                return make(1 + al * a, -2 * cos(w), 1 - al * a,
                    1 + al / a, -2 * cos(w), 1 - al / a)
            }

            fun highPass(fs: Int, f: Double, q: Double = 0.7071): Biquad {
                val w = 2 * PI * f / fs
                val al = sin(w) / (2 * q)
                val c = cos(w)
                return make((1 + c) / 2, -(1 + c), (1 + c) / 2,
                    1 + al, -2 * c, 1 - al)
            }

            fun lowPass(fs: Int, f: Double, q: Double = 0.7071): Biquad {
                val w = 2 * PI * f / fs
                val al = sin(w) / (2 * q)
                val c = cos(w)
                return make((1 - c) / 2, 1 - c, (1 - c) / 2,
                    1 + al, -2 * c, 1 - al)
            }
        }
    }

    /** Linear filter state of one signal path (main or shadow). */
    private inner class Path {
        val shelfL = Biquad.lowShelf(rate, 150.0, prm.shelfDb)
        val shelfR = shelfL.copy()
        val presence = Biquad.peak(rate, 2500.0, 0.8, prm.peakDb)
        val bandHp = Biquad.highPass(rate, 300.0)
        val bandLp = Biquad.lowPass(rate, min(3400.0, rate * 0.45))
        // per-frame intermediates, read by the control logic (main path)
        var mb = 0.0
        var mr = 0.0
        var s = 0.0

        fun split(l: Double, r: Double) {
            val ls = shelfL.run(l)
            val rs = shelfR.run(r)
            val m = presence.run((ls + rs) * 0.5)
            s = (ls - rs) * 0.5 * prm.sideGain
            mb = bandLp.run(bandHp.run(m))
            mr = m - mb
        }
    }

    private val main = Path()
    private val shadows = ArrayList<Path>()

    private fun coef(ms: Double) = 1 - exp(-1.0 / (rate * ms / 1000))
    private val envC = coef(30.0)
    private val flDown = coef(50.0)
    private val flUp = coef(3000.0)
    private val pAtt = coef(20.0)
    private val pRel = coef(300.0)
    private val cAtt = coef(10.0)
    private val cRel = coef(200.0)
    private val limRel = coef(80.0)
    private val duckLin = 10.0.pow(-prm.duckDb / 20)
    private val makeup = 10.0.pow(prm.makeupDb / 20)
    private val ceiling = 10.0.pow(-1.0 / 20)
    private val floorPow = 10.0.pow(-55.0 / 10)

    private var eBand = 0.0
    private var floor = 1.0
    private var speech = 0.0
    private var rms = 0.0
    private var lim = 1.0

    /** Process [frames] interleaved stereo frames of [buf] in place; the
     *  same chain (same gains) is applied to each of [shadow]. */
    fun process(buf: FloatArray, frames: Int,
                shadow: Array<FloatArray> = emptyArray()) {
        if (level == Level.OFF) return
        while (shadows.size < shadow.size) shadows += Path()
        for (i in 0 until frames) {
            val k = 2 * i
            main.split(buf[k].toDouble(), buf[k + 1].toDouble())
            for (j in shadow.indices)
                shadows[j].split(shadow[j][k].toDouble(),
                    shadow[j][k + 1].toDouble())

            // speech detector: centre speech-band energy rising above its
            // own slowly tracked floor (music beds are steadier than
            // syllables); floor falls fast, rises over seconds
            eBand += (main.mb * main.mb - eBand) * envC
            floor = if (eBand < floor) floor + (eBand - floor) * flDown
            else floor + (eBand - floor) * flUp
            val snrDb = 10 * log10((eBand + 1e-12) / (floor + 1e-12))
            val target = if (eBand < floorPow) 0.0
            else ((snrDb - 3) / 6).coerceIn(0.0, 1.0)
            speech += (target - speech) * (if (target > speech) pAtt else pRel)
            val duck = 1 - speech * (1 - duckLin)

            var l = main.mb + main.mr * duck + main.s * duck
            var r = main.mb + main.mr * duck - main.s * duck

            // RMS compressor (stereo-linked) + makeup
            val p2 = (l * l + r * r) * 0.5
            rms += (p2 - rms) * (if (p2 > rms) cAtt else cRel)
            val lvlDb = 10 * ln(rms + 1e-12) / ln(10.0)
            val over = lvlDb - prm.threshDb
            var g = makeup
            if (over > 0) g *= 10.0.pow(-over * (1 - 1 / prm.ratio) / 20)

            // peak limiter: instant attack, smooth release
            val pk = max(abs(l), abs(r)) * g
            lim = if (pk * lim > ceiling) ceiling / pk
            else lim + (1 - lim) * limRel
            g *= lim

            buf[k] = (l * g).toFloat()
            buf[k + 1] = (r * g).toFloat()
            for (j in shadow.indices) {
                val sp = shadows[j]
                l = sp.mb + sp.mr * duck + sp.s * duck
                r = sp.mb + sp.mr * duck - sp.s * duck
                shadow[j][k] = (l * g).toFloat()
                shadow[j][k + 1] = (r * g).toFloat()
            }
        }
    }

    private var fbuf = FloatArray(0)

    /** 16-bit interleaved stereo variant (encoder input). */
    fun processShorts(buf: ShortArray, frames: Int) {
        if (level == Level.OFF) return
        if (fbuf.size < frames * 2) fbuf = FloatArray(frames * 2)
        for (i in 0 until frames * 2) fbuf[i] = buf[i] / 32768f
        process(fbuf, frames)
        for (i in 0 until frames * 2)
            buf[i] = (fbuf[i] * 32767f).coerceIn(-32768f, 32767f)
                .toInt().toShort()
    }

    companion object {
        private const val TAG = "ADPatcher"

        /**
         * Extra output track: the video's own audio, decoded, downmixed
         * to stereo (5.1 centre kept at full weight - that's the dialogue),
         * voice-boosted, AAC-encoded at [outRate]. Same priming
         * compensation as the AD track, so it lines up with the copied
         * original. Runs synchronously; call it on its own thread.
         */
        fun writeTrack(
            context: Context, videoUri: Uri, muxer: MediaMuxer,
            muxerLock: Any, track: Int, outRate: Int, level: Level
        ) {
            val delay = Patcher.encoderDelay(outRate)
            val encoder = MediaCodec.createEncoderByType(
                MediaFormat.MIMETYPE_AUDIO_AAC)
            encoder.configure(Patcher.encoderFormat(outRate), null, null,
                MediaCodec.CONFIGURE_FLAG_ENCODE)
            encoder.start()
            val sink = Patcher.EncoderSink(encoder, muxer, muxerLock, track,
                outRate, delay)
            val fx = VoiceBoost(outRate, level)

            var inRate = 0
            var step = 1.0
            var p = 0.0              // input position of next output frame
            var inBase = 0L          // absolute input index of cur[2]
            var cur = FloatArray(0)  // [prev frame] + this chunk, stereo
            var nextOut = -1L        // absolute output frame (at outRate)
            var out = FloatArray(4096)
            val shorts = ShortArray(4096)

            fun emit(n: Int) {
                fx.process(out, n)
                var first = nextOut
                var off = 0
                if (first < delay) {   // priming hides the first frames
                    off = min(n.toLong(), delay - first).toInt()
                    first += off
                }
                var o = off
                while (o < n) {
                    val m = min(n - o, shorts.size / 2)
                    for (i in 0 until m * 2)
                        shorts[i] = (out[(o * 2) + i] * 32767f)
                            .coerceIn(-32768f, 32767f).toInt().toShort()
                    sink.feed(shorts, m, first)
                    first += m; o += m
                }
                nextOut += n
            }

            AudioEngine.decodeAudio(context, videoUri) { chunk ->
                val ch = chunk.channels
                val frames = chunk.samples.size / ch
                if (frames == 0) return@decodeAudio
                if (inRate == 0) {
                    inRate = chunk.sampleRate
                    step = inRate.toDouble() / outRate
                    nextOut = Math.round(chunk.ptsUs * outRate / 1e6)
                        .coerceAtLeast(0)
                    cur = FloatArray(2)   // fake prev frame = silence
                    inBase = 0
                }
                // downmix this chunk to stereo after the carried frame
                val nb = FloatArray((frames + 1) * 2)
                nb[0] = cur[cur.size - 2]; nb[1] = cur[cur.size - 1]
                val sc = 1f / 32768f
                var s = 0
                for (i in 1..frames) {
                    val smp = chunk.samples
                    when {
                        ch == 1 -> {
                            val v = smp[s] * sc
                            nb[2 * i] = v; nb[2 * i + 1] = v
                        }
                        ch == 2 -> {
                            nb[2 * i] = smp[s] * sc
                            nb[2 * i + 1] = smp[s + 1] * sc
                        }
                        else -> {   // FL FR FC [LFE] BL BR ...
                            val c = smp[s + 2] * sc * 0.707f
                            var l = smp[s] * sc + c
                            var r = smp[s + 1] * sc + c
                            for (x in 4 until ch) {
                                if (x % 2 == 0) l += smp[s + x] * sc * 0.707f
                                else r += smp[s + x] * sc * 0.707f
                            }
                            nb[2 * i] = l * 0.5f; nb[2 * i + 1] = r * 0.5f
                        }
                    }
                    s += ch
                }
                cur = nb
                // nb[2*(k+1)] = absolute input frame inBase + k
                val lastAbs = inBase + frames - 1
                var n = 0
                if (inRate == outRate) {
                    if (out.size < frames * 2) out = FloatArray(frames * 2)
                    System.arraycopy(nb, 2, out, 0, frames * 2)
                    n = frames
                } else {
                    while (p <= lastAbs) {
                        val i = Math.floor(p).toLong()
                        val f = (p - i).toFloat()
                        val li = (i - inBase + 1).toInt()  // >= 0 (prev)
                        if (li + 1 > frames) break
                        if (2 * n + 2 > out.size) out = out.copyOf(out.size * 2)
                        for (c in 0..1) {
                            val a = nb[2 * li + c]
                            val b = nb[2 * (li + 1) + c]
                            out[2 * n + c] = a + (b - a) * f
                        }
                        n++
                        p += step
                    }
                }
                inBase += frames
                if (n > 0) emit(n)
            }
            sink.finish()
            android.util.Log.i(TAG, "voice-boost track done")
        }
    }
}
