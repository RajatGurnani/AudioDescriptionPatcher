package com.fuzzypyjamas.adpatcher

import android.content.Context
import android.media.MediaCodec
import android.media.MediaExtractor
import android.media.MediaFormat
import android.net.Uri
import kotlin.math.ceil
import kotlin.math.log10
import kotlin.math.max
import kotlin.math.roundToLong
import kotlin.math.sqrt

/**
 * Streaming audio decode + onset-envelope extraction.
 *
 * Mirrors adpatch.py's onset_envelope(): audio is decoded, downmixed to
 * mono, resampled to [SR] Hz, and reduced to one loudness-change value per
 * [HOP] samples (a 100 Hz "onset envelope"). All alignment happens on these
 * envelopes - a 3-hour film becomes ~1M floats.
 *
 * For long files, [onsetEnvelopeParallel] splits the timeline into chunks
 * decoded concurrently (own extractor + codec each), which is the main
 * analysis-phase speedup on big.LITTLE phones.
 */
object AudioEngine {
    private const val TAG = "ADPatcher"
    const val SR = 8000
    const val HOP = 80
    const val FPS = SR.toDouble() / HOP   // envelope frames per second

    /** Set from the UI Cancel button; checked in every long-running loop. */
    @Volatile
    var cancelRequested = false

    /** Thrown by an onChunk callback to end a decode early (not an error). */
    class StopDecode : RuntimeException()

    /** Decoded PCM chunk: interleaved 16-bit samples + stream properties. */
    class PcmChunk(
        val samples: ShortArray, val channels: Int,
        val sampleRate: Int, val ptsUs: Long
    )

    /** Duration of the first audio track in microseconds (0 if unknown). */
    fun probeAudio(context: Context, uri: Uri): Pair<Long, Int> {
        val extractor = MediaExtractor()
        context.contentResolver.openFileDescriptor(uri, "r")!!.use {
            extractor.setDataSource(it.fileDescriptor)
            for (i in 0 until extractor.trackCount) {
                val f = extractor.getTrackFormat(i)
                if (f.getString(MediaFormat.KEY_MIME)!!
                        .startsWith("audio/")) {
                    val dur = if (f.containsKey(MediaFormat.KEY_DURATION))
                        f.getLong(MediaFormat.KEY_DURATION) else 0L
                    val rate =
                        if (f.containsKey(MediaFormat.KEY_SAMPLE_RATE))
                            f.getInteger(MediaFormat.KEY_SAMPLE_RATE) else 0
                    extractor.release()
                    return Pair(dur, rate)
                }
            }
        }
        extractor.release()
        throw RuntimeException("no audio track found")
    }

    /**
     * Decode the first audio track of [uri], invoking [onChunk] for every
     * PCM buffer. With [rangeUs] set, decoding seeks near the range start
     * and stops once chunks pass its end; chunks may still start earlier
     * than the range (seeks land on sync points) - callers trim by pts.
     *
     * Throughput-tuned: input is polled non-blocking, ALL ready output
     * buffers are drained per pass, and the only blocking wait happens
     * when neither side made progress. Returns the track duration in us.
     */
    fun decodeAudio(
        context: Context, uri: Uri,
        rangeUs: LongRange? = null,
        onProgress: (Float) -> Unit = {},
        onChunk: (PcmChunk) -> Unit
    ): Long {
        val extractor = MediaExtractor()
        val pfd = context.contentResolver.openFileDescriptor(uri, "r")
            ?: throw RuntimeException("cannot open $uri")
        pfd.use {
            extractor.setDataSource(it.fileDescriptor)
            var trackIndex = -1
            var format: MediaFormat? = null
            for (i in 0 until extractor.trackCount) {
                val f = extractor.getTrackFormat(i)
                if (f.getString(MediaFormat.KEY_MIME)!!.startsWith("audio/")) {
                    trackIndex = i; format = f; break
                }
            }
            if (trackIndex < 0) throw RuntimeException("no audio track found")
            extractor.selectTrack(trackIndex)
            // no seek for a range from 0: even seekTo(0) makes the AAC
            // decoder drop its first frame
            if (rangeUs != null && rangeUs.first > 0)
                extractor.seekTo(rangeUs.first,
                    MediaExtractor.SEEK_TO_PREVIOUS_SYNC)
            val durationUs =
                if (format!!.containsKey(MediaFormat.KEY_DURATION))
                    format.getLong(MediaFormat.KEY_DURATION) else 0L

            val mime = format.getString(MediaFormat.KEY_MIME)!!
            val codec = try {
                MediaCodec.createDecoderByType(mime).apply {
                    configure(format, null, null, 0)
                    start()
                }
            } catch (e: Exception) {
                extractor.release()
                throw RuntimeException(
                    "this phone has no decoder for audio codec '$mime' " +
                    "(common for AC3/DTS in MKVs). Re-encode the audio to " +
                    "AAC on a PC, or use the web app instead.")
            }

            val info = MediaCodec.BufferInfo()
            var inputDone = false
            var outputDone = false
            var channels = 0
            var sampleRate = 0
            try {
                while (!outputDone) {
                    if (cancelRequested)
                        throw RuntimeException("cancelled")
                    var progressed = false
                    if (!inputDone) {
                        val inIdx = codec.dequeueInputBuffer(0)
                        if (inIdx >= 0) {
                            progressed = true
                            val buf = codec.getInputBuffer(inIdx)!!
                            val size = extractor.readSampleData(buf, 0)
                            if (size < 0) {
                                codec.queueInputBuffer(inIdx, 0, 0, 0,
                                    MediaCodec.BUFFER_FLAG_END_OF_STREAM)
                                inputDone = true
                            } else {
                                codec.queueInputBuffer(inIdx, 0, size,
                                    extractor.sampleTime, 0)
                                extractor.advance()
                            }
                        }
                    }
                    while (true) {
                        val outIdx = codec.dequeueOutputBuffer(info,
                            if (progressed) 0 else 10_000)
                        if (outIdx ==
                            MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                            val of = codec.outputFormat
                            channels = of.getInteger(
                                MediaFormat.KEY_CHANNEL_COUNT)
                            sampleRate = of.getInteger(
                                MediaFormat.KEY_SAMPLE_RATE)
                            continue
                        }
                        if (outIdx < 0) break
                        progressed = true
                        if (info.size > 0) {
                            if (channels == 0) {
                                val of = codec.outputFormat
                                channels = of.getInteger(
                                    MediaFormat.KEY_CHANNEL_COUNT)
                                sampleRate = of.getInteger(
                                    MediaFormat.KEY_SAMPLE_RATE)
                            }
                            val outBuf = codec.getOutputBuffer(outIdx)!!
                            val shorts = ShortArray(info.size / 2)
                            outBuf.position(info.offset)
                            outBuf.asShortBuffer().get(shorts)
                            onChunk(PcmChunk(shorts, channels, sampleRate,
                                info.presentationTimeUs))
                            if (durationUs > 0) onProgress(
                                (info.presentationTimeUs.toFloat() /
                                    durationUs).coerceIn(0f, 1f))
                        }
                        codec.releaseOutputBuffer(outIdx, false)
                        if (rangeUs != null &&
                            info.presentationTimeUs >= rangeUs.last) {
                            outputDone = true
                            break
                        }
                        if (info.flags and
                            MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0) {
                            outputDone = true
                            break
                        }
                    }
                }
            } catch (e: StopDecode) {
                // callback decided it has everything it needs
            } finally {
                try { codec.stop() } catch (_: Exception) {}
                codec.release()
                extractor.release()
            }
            return durationUs
        }
    }

    /**
     * Incremental mono-downmix + linear-resample + energy accumulation.
     * One instance per decode stream; feed() consumes PCM chunks and
     * appends 100 Hz energy values to an internal growth array whose
     * index 0 is global grid frame [firstOutFrame].
     *
     * Decoded samples are placed by COUNT from an anchor, not by each
     * buffer's pts. Measured on a CMF Phone 1 (Android 16) against ffmpeg:
     * AAC-in-MP4 pts are exact, but the MP3 decoder's first buffer is
     * short yet stamped as a whole frame, so its pts run 75 ms ahead of
     * the content (57 ms after a seek), while sample counts match ffmpeg
     * to the sample. Following pts put ~20 ms of phantom silence into the
     * AD envelope that Patcher (which counts samples) never reproduced.
     * The anchor is 0 for a decode from the file start - ffmpeg's timeline
     * and what Patcher reads - or the first buffer's pts after a seek,
     * which [onsetEnvelopeParallel] then corrects by matching PCM probes
     * (kept in [head] / [tail]) and stores as [shift].
     */
    private class EnvelopeAccumulator(
        val firstOutFrame: Long,          // grid frame of energies[0]
        val slotFirst: Long,              // first grid frame this one owns
        private val anchorOnPts: Boolean,
        private val headUs: LongRange? = null,  // by count from 1st sample
        private val tailUs: LongRange? = null   // on the provisional timeline
    ) {
        var energies = FloatArray(1 shl 16)
        var count = 0
        var sampleRate = 0
        var head: FloatArray? = null; var headAt = 0L   // input index of [0]
        var tail: FloatArray? = null; var tailAt = 0L
        var shift = 0.0                   // grid frames, set after matching
        private var acc = 0f
        private var accCount = 0
        private var step = 0.0        // input samples per output sample
        private var nextPos = -1.0    // global input-sample position
        private var anchor = 0L       // global input index of first sample
        private var consumed = 0L
        private var prevLast = 0f

        val inPerFrame get() = step * HOP

        private fun record(dst: FloatArray?, at: Long, mono: FloatArray,
                           start: Long) {
            if (dst == null) return
            val from = max(at, start)
            val to = minOf(at + dst.size, start + mono.size)
            for (g in from until to)
                dst[(g - at).toInt()] = mono[(g - start).toInt()]
        }

        fun feed(chunk: PcmChunk) {
            val n = chunk.samples.size / chunk.channels
            if (n == 0) return
            if (nextPos < 0) {   // first chunk: fix the timeline
                sampleRate = chunk.sampleRate
                step = chunk.sampleRate.toDouble() / SR
                val r = sampleRate / 1e6
                anchor = if (anchorOnPts)
                    (chunk.ptsUs * r).roundToLong() else 0L
                nextPos = firstOutFrame * HOP * step
                headUs?.let {
                    headAt = anchor + (it.first * r).roundToLong()
                    head = FloatArray(((it.last - it.first) * r).toInt())
                }
                tailUs?.let {
                    tailAt = (it.first * r).roundToLong()
                    tail = FloatArray(((it.last - it.first) * r).toInt())
                }
            }
            val globalIn = anchor + consumed
            val ch = chunk.channels
            val scale = 1.0f / (ch * 32768.0f)
            val mono = FloatArray(n)
            var s = 0
            for (i in 0 until n) {
                var sum = 0f
                for (c in 0 until ch) sum += chunk.samples[s++]
                mono[i] = sum * scale
            }
            record(head, headAt, mono, globalIn)
            record(tail, tailAt, mono, globalIn)
            var pos = nextPos
            val limit = globalIn + n - 1
            while (pos < limit) {
                val i = pos.toLong()
                val local = (i - globalIn).toInt()
                // local < -1 only before the first sample (the grid starts
                // ahead of where a seek landed): silence
                val v = if (local < -1) 0f else {
                    val s0 = if (local < 0) prevLast else mono[local]
                    val s1 = mono[local + 1]
                    s0 + (s1 - s0) * (pos - i).toFloat()
                }
                acc += v * v
                if (++accCount == HOP) {
                    if (count == energies.size)
                        energies = energies.copyOf(energies.size * 2)
                    energies[count++] = acc
                    acc = 0f; accCount = 0
                }
                pos += step
            }
            nextPos = pos
            prevLast = mono[n - 1]
            consumed += n
        }

        /** Energy at global grid frame [g] after [shift] (linear interp). */
        fun value(g: Long): Float {
            val x = (g - firstOutFrame) - shift
            val i = Math.floor(x).toLong()
            val f = (x - i).toFloat()
            fun e(k: Long) = energies[k.coerceIn(0L, count - 1L).toInt()]
            return e(i) + (e(i + 1) - e(i)) * f
        }

        /** End (exclusive) of the grid frames this stream can supply. */
        val end get() = firstOutFrame + count + Math.floor(shift).toLong()
    }

    /** Serial envelope of the full stream (fallback / small files). */
    fun onsetEnvelope(
        context: Context, uri: Uri, onProgress: (Float) -> Unit
    ): FloatArray {
        val t0 = android.os.SystemClock.elapsedRealtime()
        val acc = EnvelopeAccumulator(0, 0, anchorOnPts = false)
        decodeAudio(context, uri, onProgress = onProgress) { acc.feed(it) }
        val env = finishEnvelope(arrayOf(acc))
        android.util.Log.d(TAG, "envelope done: ${env.size} frames " +
            "(${"%.1f".format(env.size / FPS / 60)} min) in " +
            "${(android.os.SystemClock.elapsedRealtime() - t0) / 1000}s")
        return env
    }

    // seam matching for ranged decodes (see onsetEnvelopeParallel)
    private const val PRE_US = 2_000_000L        // decode this much early
    private val HEAD_US = 250_000L..750_000L     // probe, after warm-up
    private const val SEARCH_US = 500_000L       // +- seek error tolerated
    private const val MARGIN = 50                // grid frames kept for shift
    private const val MIN_PROBE_SCORE = 0.9

    /**
     * Where does [b]'s head probe sit in [a]'s tail? Returns [b]'s
     * timeline correction in samples (true minus provisional), given [a]'s
     * own correction [aDelta], or null if the probe doesn't match (e.g.
     * digital silence at the seam). Decoders are deterministic after a few
     * frames, so a real match scores ~1.0 and the lag is sample-exact.
     */
    private fun seamDelta(a: EnvelopeAccumulator, b: EnvelopeAccumulator,
                          aDelta: Long): Long? {
        val tail = a.tail ?: return null
        val head = b.head ?: return null
        if (head.size < 16 || tail.size <= head.size) return null
        var hh = 0.0
        for (v in head) hh += v.toDouble() * v
        if (hh < 1e-8 * head.size) return null
        val n = Aligner.nextPow2(tail.size + head.size)
        val cc = Aligner.crossCorrelate(tail, head, n)
        val pre = DoubleArray(tail.size + 1)
        for (i in tail.indices)
            pre[i + 1] = pre[i] + tail[i].toDouble() * tail[i]
        var best = -1
        var bestScore = -1.0
        for (k in 0..tail.size - head.size) {
            val tt = pre[k + head.size] - pre[k]
            val sc = cc[k] / (sqrt(hh * tt) + 1e-12)
            if (sc > bestScore) { bestScore = sc; best = k }
        }
        android.util.Log.d(TAG, "seam probe score %.3f".format(bestScore))
        if (bestScore < MIN_PROBE_SCORE) return null
        return a.tailAt + best + aDelta - b.headAt
    }

    /**
     * Chunk-parallel envelope: the timeline is split into [workers]
     * ranges, each decoded with its own extractor + codec, and the energy
     * segments are concatenated on the global 100 Hz grid.
     *
     * A seek can't be trusted to the sample (MP3 pts after a seek were
     * 57 ms off on the CMF Phone 1), so every range after the first is
     * decoded from [PRE_US] early, a short PCM probe from its start is
     * located in the previous range's decoded audio, and the range's
     * energies are shifted by the difference. The first range never
     * seeks, so the whole envelope sits on the serial-decode timeline.
     */
    fun onsetEnvelopeParallel(
        context: Context, uri: Uri, workers: Int,
        onProgress: (Float) -> Unit
    ): FloatArray {
        val (durationUs, _) = probeAudio(context, uri)
        if (workers <= 1 || durationUs < 60_000_000L)
            return onsetEnvelope(context, uri, onProgress)

        val t0 = android.os.SystemClock.elapsedRealtime()
        val accs = arrayOfNulls<EnvelopeAccumulator>(workers)
        val errors = arrayOfNulls<Throwable>(workers)
        val fractions = FloatArray(workers)
        val threads = (0 until workers).map { w ->
            Thread {
                try {
                    val startUs = durationUs * w / workers
                    val endUs = if (w == workers - 1) Long.MAX_VALUE
                    else durationUs * (w + 1) / workers
                    val slotFirst = ceil(startUs * FPS / 1e6).toLong()
                    val slotLast = if (endUs == Long.MAX_VALUE)
                        Long.MAX_VALUE
                    else ceil(endUs * FPS / 1e6).toLong()
                    val fromUs = if (w == 0) 0L else max(0L, startUs - PRE_US)
                    // the next range's head probe, on this range's timeline
                    val nextFrom = endUs - PRE_US
                    val acc = EnvelopeAccumulator(
                        firstOutFrame = if (w == 0) 0L
                        else max(0L, slotFirst - MARGIN),
                        slotFirst = slotFirst,
                        anchorOnPts = w > 0,
                        headUs = if (w > 0) HEAD_US else null,
                        tailUs = if (w == workers - 1) null
                        else (nextFrom + HEAD_US.first - SEARCH_US)..
                            (nextFrom + HEAD_US.last + SEARCH_US))
                    accs[w] = acc
                    val stopAt = if (slotLast == Long.MAX_VALUE)
                        Long.MAX_VALUE
                    else slotLast - acc.firstOutFrame + MARGIN
                    // open-ended range: stop by sample count (StopDecode
                    // below), never by pts - MP3 pts run ahead of the
                    // content, which ended this range 40 ms short and
                    // slid every later range early by as much
                    decodeAudio(context, uri,
                        rangeUs = fromUs..(Long.MAX_VALUE - 1)) { chunk ->
                        acc.feed(chunk)
                        fractions[w] = if (endUs == Long.MAX_VALUE) 0f
                        else ((chunk.ptsUs - fromUs).toFloat() /
                                (endUs - fromUs)).coerceIn(0f, 1f)
                        onProgress(fractions.sum() / workers)
                        if (acc.count >= stopAt) throw StopDecode()
                    }
                    fractions[w] = 1f
                    onProgress(fractions.sum() / workers)
                } catch (t: Throwable) {
                    errors[w] = t
                }
            }.apply { start() }
        }
        threads.forEach { it.join() }
        errors.filterNotNull().firstOrNull()?.let { throw it }

        val segs = accs.filterNotNull().toTypedArray()
        var delta = 0L
        for (w in 1 until segs.size) {
            delta = seamDelta(segs[w - 1], segs[w], delta) ?: run {
                android.util.Log.i(TAG, "seam $w: probe didn't match - " +
                    "trusting the seek's timestamps")
                0L
            }
            segs[w].shift = delta / segs[w].inPerFrame
            android.util.Log.i(TAG, "seam $w: %+.2f ms".format(
                delta * 1000.0 / segs[w].sampleRate))
        }
        val env = finishEnvelope(segs)
        android.util.Log.d(TAG,
            "parallel envelope ($workers workers): ${env.size} frames in " +
            "${(android.os.SystemClock.elapsedRealtime() - t0) / 1000}s")
        return env
    }

    /** Concatenate accumulator segments, then log-diff + z-normalize. */
    private fun finishEnvelope(accs: Array<EnvelopeAccumulator>): FloatArray {
        // every range fills its slot exactly (value() holds the edge if a
        // decode came up short), so later ranges keep their grid place
        val ends = LongArray(accs.size) { i ->
            if (i < accs.size - 1) accs[i + 1].slotFirst
            else max(accs[i].slotFirst, accs[i].end)
        }
        var total = 0
        for (i in accs.indices) total += (ends[i] - accs[i].slotFirst).toInt()
        if (total < 2) throw RuntimeException("audio too short")
        val energy = FloatArray(total)
        var off = 0
        for (i in accs.indices)
            for (g in accs[i].slotFirst until ends[i])
                energy[off++] = accs[i].value(g)

        val logE = FloatArray(total) {
            log10(energy[it].toDouble() + 1e-9).toFloat()
        }
        val onset = FloatArray(total - 1) {
            max(logE[it + 1] - logE[it], 0f)
        }
        var mean = 0.0
        for (v in onset) mean += v
        mean /= onset.size
        var varSum = 0.0
        for (i in onset.indices) {
            onset[i] = onset[i] - mean.toFloat()
            varSum += onset[i] * onset[i]
        }
        val std = sqrt(varSum / onset.size).toFloat()
        if (std > 0) for (i in onset.indices) onset[i] /= std
        return onset
    }
}
