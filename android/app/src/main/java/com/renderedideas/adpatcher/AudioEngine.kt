package com.renderedideas.adpatcher

import android.content.Context
import android.media.MediaCodec
import android.media.MediaExtractor
import android.media.MediaFormat
import android.net.Uri
import kotlin.math.log10
import kotlin.math.max
import kotlin.math.sqrt

/**
 * Streaming audio decode + onset-envelope extraction.
 *
 * Mirrors adpatch.py's onset_envelope(): audio is decoded, downmixed to
 * mono, resampled to [SR] Hz, and reduced to one loudness-change value per
 * [HOP] samples (a 100 Hz "onset envelope"). All alignment happens on these
 * envelopes - a 3-hour film becomes ~1M floats.
 */
object AudioEngine {
    private const val TAG = "ADPatcher"
    const val SR = 8000
    const val HOP = 80
    const val FPS = SR.toDouble() / HOP   // envelope frames per second

    /** Decoded PCM chunk: interleaved 16-bit samples + stream properties. */
    class PcmChunk(
        val samples: ShortArray, val channels: Int,
        val sampleRate: Int, val ptsUs: Long
    )

    /**
     * Decode the first audio track of [uri], invoking [onChunk] for every
     * PCM buffer. Returns the track duration in microseconds (0 if
     * unknown). This is the single decode loop used by both envelope
     * extraction and mux-time AD processing.
     */
    fun decodeAudio(
        context: Context, uri: Uri,
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
            val durationUs =
                if (format!!.containsKey(MediaFormat.KEY_DURATION))
                    format.getLong(MediaFormat.KEY_DURATION) else 0L

            val mime = format.getString(MediaFormat.KEY_MIME)!!
            android.util.Log.d(TAG, "decode start: $mime " +
                "${format.getInteger(MediaFormat.KEY_SAMPLE_RATE)}Hz " +
                "${format.getInteger(MediaFormat.KEY_CHANNEL_COUNT)}ch " +
                "dur=${durationUs / 1_000_000}s uri=$uri")
            val codec = try {
                MediaCodec.createDecoderByType(mime).apply {
                    configure(format, null, null, 0)
                    start()
                }
            } catch (e: Exception) {
                throw RuntimeException(
                    "this phone has no decoder for audio codec '$mime' " +
                    "(common for AC3/DTS in MKVs). Re-encode the audio to " +
                    "AAC on a PC, or use the web app instead.")
            }

            val info = MediaCodec.BufferInfo()
            var inputDone = false
            var outputDone = false
            while (!outputDone) {
                if (!inputDone) {
                    val inIdx = codec.dequeueInputBuffer(10_000)
                    if (inIdx >= 0) {
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
                val outIdx = codec.dequeueOutputBuffer(info, 10_000)
                if (outIdx >= 0) {
                    if (info.size > 0) {
                        val outBuf = codec.getOutputBuffer(outIdx)!!
                        val shorts = ShortArray(info.size / 2)
                        outBuf.position(info.offset)
                        outBuf.asShortBuffer().get(shorts)
                        val of = codec.outputFormat
                        onChunk(PcmChunk(
                            shorts,
                            of.getInteger(MediaFormat.KEY_CHANNEL_COUNT),
                            of.getInteger(MediaFormat.KEY_SAMPLE_RATE),
                            info.presentationTimeUs))
                        if (durationUs > 0) onProgress(
                            (info.presentationTimeUs.toFloat() / durationUs)
                                .coerceIn(0f, 1f))
                    }
                    codec.releaseOutputBuffer(outIdx, false)
                    if (info.flags and
                        MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0)
                        outputDone = true
                }
            }
            codec.stop(); codec.release(); extractor.release()
            return durationUs
        }
    }

    /**
     * Onset-strength envelope of [uri]'s audio: half-wave-rectified change
     * in log energy per 10 ms frame, z-normalized. Loudness *changes*
     * survive narration mixed over the soundtrack and level differences
     * between releases, which is what makes the alignment robust.
     */
    fun onsetEnvelope(
        context: Context, uri: Uri, onProgress: (Float) -> Unit
    ): FloatArray {
        val t0 = android.os.SystemClock.elapsedRealtime()
        val energies = ArrayList<Float>(1 shl 18)
        var acc = 0.0f
        var accCount = 0
        // streaming linear-resampler state (native rate -> SR)
        var nextPos = 0.0        // position of next output sample, in input units
        var globalIn = 0L        // input samples consumed before current chunk
        var prevLast = 0.0f      // last input sample of the previous chunk

        decodeAudio(context, uri, onProgress) { chunk ->
            val n = chunk.samples.size / chunk.channels
            if (n == 0) return@decodeAudio
            val mono = FloatArray(n)
            var s = 0
            for (i in 0 until n) {
                var sum = 0f
                for (c in 0 until chunk.channels) sum += chunk.samples[s++]
                mono[i] = sum / (chunk.channels * 32768.0f)
            }
            val step = chunk.sampleRate.toDouble() / SR
            while (nextPos < globalIn + n - 1) {
                val i = nextPos.toLong()
                val frac = (nextPos - i).toFloat()
                val local = (i - globalIn).toInt()
                val s0 = if (local < 0) prevLast else mono[local]
                val s1 = mono[local + 1]
                val v = s0 + (s1 - s0) * frac
                acc += v * v
                if (++accCount == HOP) {
                    energies.add(acc); acc = 0f; accCount = 0
                }
                nextPos += step
            }
            prevLast = mono[n - 1]
            globalIn += n
        }
        if (energies.size < 2) throw RuntimeException("audio too short")

        // log-energy delta, rectified, z-normalized (same as adpatch.py)
        val logE = FloatArray(energies.size) {
            log10(energies[it].toDouble() + 1e-9).toFloat()
        }
        val onset = FloatArray(logE.size - 1) {
            max(logE[it + 1] - logE[it], 0f)
        }
        var mean = 0.0
        for (v in onset) mean += v
        mean /= onset.size
        var varSum = 0.0
        for (i in onset.indices) {
            onset[i] = (onset[i] - mean.toFloat())
            varSum += onset[i] * onset[i]
        }
        val std = sqrt(varSum / onset.size).toFloat()
        if (std > 0) for (i in onset.indices) onset[i] /= std
        android.util.Log.d(TAG, "envelope done: ${onset.size} frames " +
            "(${"%.1f".format(onset.size / FPS / 60)} min) in " +
            "${(android.os.SystemClock.elapsedRealtime() - t0) / 1000}s")
        return onset
    }
}
