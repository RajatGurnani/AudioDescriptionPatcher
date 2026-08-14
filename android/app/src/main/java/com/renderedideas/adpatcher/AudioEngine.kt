package com.renderedideas.adpatcher

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
            if (rangeUs != null)
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
     * appends 100 Hz energy values to an internal growth array.
     *
     * [firstOutFrame] anchors the output on the global 100 Hz grid, which
     * lets parallel range-decodes produce seamlessly mergeable segments:
     * the input position of output sample s is s * (inRate/SR), aligned
     * via each chunk's pts rather than a running sample count.
     */
    private class EnvelopeAccumulator(val firstOutFrame: Long) {
        var energies = FloatArray(1 shl 16)
        var count = 0
        private var acc = 0f
        private var accCount = 0
        private var step = 0.0        // input samples per output sample
        private var nextPos = -1.0    // global input-sample position
        private var globalIn = -1L    // input index of current chunk start
        private var prevLast = 0f

        fun feed(chunk: PcmChunk) {
            val n = chunk.samples.size / chunk.channels
            if (n == 0) return
            if (nextPos < 0) {   // first chunk: anchor on pts
                step = chunk.sampleRate.toDouble() / SR
                globalIn = (chunk.ptsUs * chunk.sampleRate / 1e6)
                    .roundToLong()
                nextPos = max(firstOutFrame * HOP * step,
                    globalIn.toDouble())
            } else {
                globalIn = (chunk.ptsUs * chunk.sampleRate / 1e6)
                    .roundToLong()
            }
            val ch = chunk.channels
            val scale = 1.0f / (ch * 32768.0f)
            val mono = FloatArray(n)
            var s = 0
            for (i in 0 until n) {
                var sum = 0f
                for (c in 0 until ch) sum += chunk.samples[s++]
                mono[i] = sum * scale
            }
            var pos = nextPos
            val limit = globalIn + n - 1
            while (pos < limit) {
                val i = pos.toLong()
                val local = (i - globalIn).toInt()
                val s0 = if (local < 0) prevLast else mono[local]
                val s1 = mono[local + 1]
                val v = s0 + (s1 - s0) * (pos - i).toFloat()
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
        }
    }

    /** Serial envelope of the full stream (fallback / small files). */
    fun onsetEnvelope(
        context: Context, uri: Uri, onProgress: (Float) -> Unit
    ): FloatArray {
        val t0 = android.os.SystemClock.elapsedRealtime()
        val acc = EnvelopeAccumulator(firstOutFrame = 0)
        decodeAudio(context, uri, onProgress = onProgress) { acc.feed(it) }
        val env = finishEnvelope(arrayOf(acc))
        android.util.Log.d(TAG, "envelope done: ${env.size} frames " +
            "(${"%.1f".format(env.size / FPS / 60)} min) in " +
            "${(android.os.SystemClock.elapsedRealtime() - t0) / 1000}s")
        return env
    }

    /**
     * Chunk-parallel envelope: the timeline is split into [workers]
     * ranges, each decoded with its own extractor + codec, and the energy
     * segments are concatenated on the global 100 Hz grid. Seam cost is at
     * most one 10 ms frame per boundary - irrelevant to alignment.
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
                    val firstFrame =
                        ceil(startUs * FPS / 1e6).toLong()
                    val lastFrame = if (endUs == Long.MAX_VALUE)
                        Long.MAX_VALUE
                    else ceil(endUs * FPS / 1e6).toLong()
                    val acc = EnvelopeAccumulator(firstFrame)
                    accs[w] = acc
                    decodeAudio(context, uri,
                        rangeUs = startUs..(if (endUs == Long.MAX_VALUE)
                            Long.MAX_VALUE - 1 else endUs)) { chunk ->
                        acc.feed(chunk)
                        fractions[w] = if (endUs == Long.MAX_VALUE) 0f
                        else ((chunk.ptsUs - startUs).toFloat() /
                                (endUs - startUs)).coerceIn(0f, 1f)
                        onProgress(fractions.sum() / workers)
                        if (lastFrame != Long.MAX_VALUE &&
                            acc.count >= lastFrame - firstFrame)
                            throw StopDecode()
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

        // trim each segment to its grid slot and concatenate
        val segs = accs.filterNotNull()
        val env = finishEnvelope(segs.toTypedArray(), trimToNext = true)
        android.util.Log.d(TAG,
            "parallel envelope ($workers workers): ${env.size} frames in " +
            "${(android.os.SystemClock.elapsedRealtime() - t0) / 1000}s")
        return env
    }

    /** Concatenate accumulator segments, then log-diff + z-normalize. */
    private fun finishEnvelope(
        accs: Array<EnvelopeAccumulator>, trimToNext: Boolean = false
    ): FloatArray {
        var total = 0
        val takes = IntArray(accs.size)
        for (i in accs.indices) {
            takes[i] = if (trimToNext && i < accs.size - 1) {
                val span = (accs[i + 1].firstOutFrame -
                        accs[i].firstOutFrame).toInt()
                minOf(accs[i].count, span)
            } else accs[i].count
            total += takes[i]
        }
        if (total < 2) throw RuntimeException("audio too short")
        val energy = FloatArray(total)
        var off = 0
        for (i in accs.indices) {
            System.arraycopy(accs[i].energies, 0, energy, off, takes[i])
            off += takes[i]
        }

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
