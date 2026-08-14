package com.renderedideas.adpatcher

import android.content.Context
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaExtractor
import android.media.MediaFormat
import android.media.MediaMuxer
import android.net.Uri
import java.nio.ByteBuffer
import kotlin.math.abs
import kotlin.math.min

/**
 * Applies the alignment and writes the output MP4:
 *   track 0: video, copied sample-by-sample (no re-encode)
 *   track 1: corrected AD audio, AAC 192k (offset + speed applied)
 *   track 2: original audio, copied when its codec fits in MP4
 *
 * The AD correction resamples on the fly: output frame j (at 48/44.1 kHz)
 * reads AD input position ((j/outRate - b) / a) * inRate with linear
 * interpolation - one pass handles offset, drift and sample-rate
 * conversion together. (When a != 1 pitch shifts by the same few percent;
 * inaudible for typical PAL drift, and zero when there is no drift.)
 */
object Patcher {
    private const val TAG = "ADPatcher"
    private const val OUT_RATE = 44100
    private const val OUT_CHANNELS = 2
    private const val AD_BITRATE = 192_000

    private val COPYABLE_AUDIO = setOf(
        MediaFormat.MIMETYPE_AUDIO_AAC, "audio/ac3", "audio/eac3")

    fun mux(
        context: Context, videoUri: Uri, adUri: Uri, outUri: Uri,
        a: Double, bSec: Double,
        log: (String) -> Unit, onProgress: (Float) -> Unit
    ) {
        val t0 = android.os.SystemClock.elapsedRealtime()
        fun logT(msg: String) = android.util.Log.d(TAG,
            "$msg at ${(android.os.SystemClock.elapsedRealtime() - t0) / 1000}s")
        val outPfd = context.contentResolver
            .openFileDescriptor(outUri, "rw")
            ?: throw RuntimeException("cannot open output")
        outPfd.use { pfd ->
            val muxer = MediaMuxer(pfd.fileDescriptor,
                MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4)

            // ---- discover input tracks
            val vext = MediaExtractor()
            context.contentResolver.openFileDescriptor(videoUri, "r")!!
                .use { vfd ->
                    vext.setDataSource(vfd.fileDescriptor)

                    var videoTrackIn = -1
                    var audioTrackIn = -1
                    var videoFormat: MediaFormat? = null
                    var audioFormat: MediaFormat? = null
                    for (i in 0 until vext.trackCount) {
                        val f = vext.getTrackFormat(i)
                        val mime = f.getString(MediaFormat.KEY_MIME)!!
                        if (videoTrackIn < 0 && mime.startsWith("video/")) {
                            videoTrackIn = i; videoFormat = f
                        } else if (audioTrackIn < 0 &&
                            mime.startsWith("audio/")) {
                            audioTrackIn = i; audioFormat = f
                        }
                    }
                    if (videoTrackIn < 0)
                        throw RuntimeException("no video track in input")

                    val videoTrackOut = muxer.addTrack(videoFormat!!)

                    // AD track: AAC-LC 44100 stereo; csd-0 built by hand so
                    // the muxer can start before the encoder produces output
                    val adFormat = MediaFormat.createAudioFormat(
                        MediaFormat.MIMETYPE_AUDIO_AAC,
                        OUT_RATE, OUT_CHANNELS)
                    adFormat.setByteBuffer("csd-0",
                        ByteBuffer.wrap(byteArrayOf(0x12, 0x10)))
                    val adTrackOut = muxer.addTrack(adFormat)

                    var origTrackOut = -1
                    val origMime =
                        audioFormat?.getString(MediaFormat.KEY_MIME)
                    if (audioFormat != null && origMime in COPYABLE_AUDIO) {
                        origTrackOut = muxer.addTrack(audioFormat)
                    } else if (audioFormat != null) {
                        log("original audio ($origMime) can't be copied " +
                            "into MP4 - output will have the AD track only")
                    }

                    muxer.start()

                    // ---- copy video samples
                    log("copying video stream...")
                    val durationUs =
                        if (videoFormat.containsKey(MediaFormat.KEY_DURATION))
                            videoFormat.getLong(MediaFormat.KEY_DURATION)
                        else 0L
                    copyTrack(vext, videoTrackIn, muxer, videoTrackOut) {
                        if (durationUs > 0)
                            onProgress(0.45f * it / durationUs)
                    }

                    logT("video copy done")

                    // ---- copy original audio samples
                    if (origTrackOut >= 0) {
                        log("copying original audio...")
                        copyTrack(vext, audioTrackIn, muxer, origTrackOut) {
                            if (durationUs > 0)
                                onProgress(0.45f + 0.1f * it / durationUs)
                        }
                    }
                    vext.release()

                    // ---- corrected AD audio
                    log("writing aligned AD audio...")
                    writeAdTrack(context, adUri, muxer, adTrackOut, a, bSec) {
                        onProgress(0.55f + 0.45f * it)
                    }

                    logT("AD encode + mux done")
                    muxer.stop()
                    muxer.release()
                }
        }
    }

    /** Copy every sample of one extractor track into a muxer track. */
    private fun copyTrack(
        extractor: MediaExtractor, trackIn: Int,
        muxer: MediaMuxer, trackOut: Int,
        onPts: (Long) -> Unit
    ) {
        extractor.selectTrack(trackIn)
        extractor.seekTo(0, MediaExtractor.SEEK_TO_PREVIOUS_SYNC)
        val buf = ByteBuffer.allocateDirect(4 shl 20)
        val info = MediaCodec.BufferInfo()
        while (true) {
            if (AudioEngine.cancelRequested)
                throw RuntimeException("cancelled")
            val size = extractor.readSampleData(buf, 0)
            if (size < 0) break
            info.set(0, size, extractor.sampleTime,
                if (extractor.sampleFlags and
                    MediaExtractor.SAMPLE_FLAG_SYNC != 0)
                    MediaCodec.BUFFER_FLAG_KEY_FRAME else 0)
            muxer.writeSampleData(trackOut, buf, info)
            onPts(extractor.sampleTime)
            extractor.advance()
        }
        extractor.unselectTrack(trackIn)
    }

    /**
     * Decode the AD file, apply `t_out = a * t_in + b` by resampled
     * streaming, AAC-encode and mux. Everything is pipelined inside the
     * decode callback; only a small PCM window is held in memory.
     */
    private fun writeAdTrack(
        context: Context, adUri: Uri, muxer: MediaMuxer, track: Int,
        a: Double, bSec: Double, onProgress: (Float) -> Unit
    ) {
        val encoder = MediaCodec.createEncoderByType(
            MediaFormat.MIMETYPE_AUDIO_AAC)
        val fmt = MediaFormat.createAudioFormat(
            MediaFormat.MIMETYPE_AUDIO_AAC, OUT_RATE, OUT_CHANNELS)
        fmt.setInteger(MediaFormat.KEY_BIT_RATE, AD_BITRATE)
        fmt.setInteger(MediaFormat.KEY_AAC_PROFILE,
            MediaCodecInfo.CodecProfileLevel.AACObjectLC)
        fmt.setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, 1 shl 16)
        encoder.configure(fmt, null, null,
            MediaCodec.CONFIGURE_FLAG_ENCODE)
        encoder.start()

        val sink = EncoderSink(encoder, muxer, track)

        // sliding window of decoded stereo PCM, absolute frame indexing
        val capacity = 1 shl 19            // ~12 s of stereo at 44.1k
        val window = FloatArray(capacity * OUT_CHANNELS)
        var winStart = 0L                   // absolute index of window[0]
        var winCount = 0                    // valid frames in window
        var inRate = 0
        var outFrame = 0L                   // next output frame index
        var totalIn = 0L
        var inputDone = false

        // generate as many output frames as the window allows
        fun pump() {
            if (inRate == 0) return
            val step = inRate / (OUT_RATE * a)
            val frame = ShortArray(1024 * OUT_CHANNELS)
            while (true) {
                var produced = 0
                while (produced < 1024) {
                    val t = outFrame / OUT_RATE.toDouble()
                    val p = (t - bSec) / a * inRate
                    if (p < 0) {                        // before AD start
                        frame[produced * 2] = 0
                        frame[produced * 2 + 1] = 0
                    } else {
                        val i = p.toLong()
                        if (!inputDone && i + 1 >= winStart + winCount) {
                            // need more decoded data for this frame
                            if (produced > 0) sink.feed(frame, produced,
                                outFrame - produced)
                            return
                        }
                        if (i + 1 >= totalIn && inputDone) {
                            if (produced > 0) sink.feed(frame, produced,
                                outFrame - produced)
                            sink.finish()
                            return
                        }
                        val local = ((i - winStart).toInt()) * OUT_CHANNELS
                        val f = (p - i).toFloat()
                        for (c in 0 until OUT_CHANNELS) {
                            val s0 = window[local + c]
                            val s1 = window[local + OUT_CHANNELS + c]
                            val v = (s0 + (s1 - s0) * f) * 32767f
                            frame[produced * OUT_CHANNELS + c] =
                                v.coerceIn(-32768f, 32767f).toInt().toShort()
                        }
                    }
                    produced++; outFrame++
                }
                sink.feed(frame, produced, outFrame - produced)
                // drop consumed window data (keep 4 frames of guard)
                val needed = ((outFrame / OUT_RATE.toDouble() - bSec)
                    / a * inRate).toLong() - 4
                if (needed > winStart) {
                    val drop = min(needed - winStart, winCount.toLong())
                        .toInt()
                    System.arraycopy(window, drop * OUT_CHANNELS, window, 0,
                        (winCount - drop) * OUT_CHANNELS)
                    winStart += drop; winCount -= drop
                }
                if (step <= 0) return
            }
        }

        var adDurationUs = 0L
        adDurationUs = AudioEngine.decodeAudio(context, adUri,
            onProgress = onProgress) { chunk ->
            if (inRate == 0) inRate = chunk.sampleRate
            val frames = chunk.samples.size / chunk.channels
            if (winCount + frames > capacity) pump()
            if (winCount + frames > capacity)
                throw RuntimeException("internal: PCM window overflow")
            var s = 0
            for (i in 0 until frames) {
                val base = winCount * OUT_CHANNELS
                when (chunk.channels) {
                    1 -> {
                        val v = chunk.samples[s++] / 32768f
                        window[base] = v; window[base + 1] = v
                    }
                    else -> {
                        window[base] = chunk.samples[s] / 32768f
                        window[base + 1] = chunk.samples[s + 1] / 32768f
                        s += chunk.channels
                    }
                }
                winCount++
            }
            totalIn += frames
            pump()
        }
        inputDone = true
        pump()          // flush the tail
        sink.finish()   // no-op if already finished
    }

    /**
     * Feeds PCM into the AAC encoder and writes encoded packets to the
     * muxer. CODEC_CONFIG packets are skipped (csd-0 was set manually).
     */
    private class EncoderSink(
        private val encoder: MediaCodec,
        private val muxer: MediaMuxer,
        private val track: Int
    ) {
        private val info = MediaCodec.BufferInfo()
        private var finished = false

        fun feed(frames: ShortArray, count: Int, firstFrame: Long) {
            if (finished) return
            var offset = 0
            while (offset < count) {
                val idx = encoder.dequeueInputBuffer(10_000)
                if (idx < 0) { drain(false); continue }
                val buf = encoder.getInputBuffer(idx)!!
                val maxFrames = buf.capacity() / (2 * OUT_CHANNELS)
                val n = min(count - offset, maxFrames)
                buf.clear()
                buf.asShortBuffer().put(frames, offset * OUT_CHANNELS,
                    n * OUT_CHANNELS)
                val ptsUs = (firstFrame + offset) * 1_000_000L / OUT_RATE
                encoder.queueInputBuffer(idx, 0, n * 2 * OUT_CHANNELS,
                    ptsUs, 0)
                offset += n
                drain(false)
            }
        }

        fun finish() {
            if (finished) return
            finished = true
            var tries = 0
            while (tries++ < 50) {   // keep draining until EOS is accepted
                val idx = encoder.dequeueInputBuffer(100_000)
                if (idx >= 0) {
                    encoder.queueInputBuffer(idx, 0, 0, 0,
                        MediaCodec.BUFFER_FLAG_END_OF_STREAM)
                    break
                }
                drain(false)
            }
            drain(true)
            encoder.stop(); encoder.release()
        }

        private fun drain(untilEos: Boolean) {
            while (true) {
                val idx = encoder.dequeueOutputBuffer(info,
                    if (untilEos) 100_000 else 0)
                if (idx == MediaCodec.INFO_TRY_AGAIN_LATER) {
                    if (!untilEos) return else continue
                }
                if (idx < 0) continue
                if (info.size > 0 && info.flags and
                    MediaCodec.BUFFER_FLAG_CODEC_CONFIG == 0) {
                    muxer.writeSampleData(track,
                        encoder.getOutputBuffer(idx)!!, info)
                }
                encoder.releaseOutputBuffer(idx, false)
                if (info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0)
                    return
            }
        }
    }
}
