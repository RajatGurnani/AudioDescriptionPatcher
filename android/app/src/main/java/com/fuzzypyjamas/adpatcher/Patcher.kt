package com.fuzzypyjamas.adpatcher

import android.content.Context
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaExtractor
import android.media.MediaFormat
import android.media.MediaMuxer
import android.net.Uri
import java.nio.ByteBuffer
import java.util.concurrent.ArrayBlockingQueue
import kotlin.math.abs
import kotlin.math.min

/**
 * Applies the alignment and writes the output MP4:
 *   track 0: video, copied sample-by-sample (no re-encode)
 *   track 1: corrected AD audio, AAC 192k (offset + speed applied)
 *   track 2: original audio, copied when its codec fits in MP4
 *
 * Wall-clock structure: the video/original-audio copy (I/O-bound) and the
 * AD decode->resample->encode chain (CPU-bound) run CONCURRENTLY into the
 * muxer, guarded by a lock. Inside the AD chain, decoding runs on its own
 * thread feeding a bounded queue, so the decoder, the sample math, and the
 * AAC encoder overlap instead of taking turns.
 *
 * The AD correction resamples on the fly: output frame j reads AD input
 * position ((j/outRate - b) / a) * inRate - maintained INCREMENTALLY
 * (position += step per frame; no divisions in the hot loop). When a != 1
 * pitch shifts by the same few percent; inaudible for typical drift and
 * zero when there is no drift.
 */
object Patcher {
    private const val TAG = "ADPatcher"
    private const val OUT_CHANNELS = 2
    private const val AD_BITRATE = 192_000

    private val COPYABLE_AUDIO = setOf(
        MediaFormat.MIMETYPE_AUDIO_AAC, "audio/ac3", "audio/eac3")

    // AAC AudioSpecificConfig frequency indices for supported output rates
    internal val AAC_FREQ_IDX = mapOf(
        96000 to 0, 88200 to 1, 64000 to 2,
        48000 to 3, 44100 to 4, 32000 to 5)

    /** csd-0 for AAC-LC stereo at [rate], built by hand so the muxer can
     *  start before the encoder produces any output. */
    internal fun aacCsd(rate: Int): ByteArray {
        val idx = AAC_FREQ_IDX[rate]!!
        val b0 = (2 shl 3) or (idx shr 1)               // objType=LC
        val b1 = ((idx and 1) shl 7) or (OUT_CHANNELS shl 3)
        return byteArrayOf(b0.toByte(), b1.toByte())
    }

    fun mux(
        context: Context, videoUri: Uri, adUri: Uri, outUri: Uri,
        a: Double, bSec: Double,
        log: (String) -> Unit, onProgress: (Float) -> Unit,
        pieces: List<Aligner.Piece> = emptyList(),
        boost: VoiceBoost.Options = VoiceBoost.Options.load(context)
    ) {
        val t0 = android.os.SystemClock.elapsedRealtime()
        fun logT(msg: String) = android.util.Log.d(TAG,
            "$msg at ${(android.os.SystemClock.elapsedRealtime() - t0) / 1000}s")

        // match the AD file's native rate when AAC supports it - the
        // common no-drift case then needs no rate conversion at all
        val (_, adNativeRate) = AudioEngine.probeAudio(context, adUri)
        val outRate =
            if (AAC_FREQ_IDX.containsKey(adNativeRate)) adNativeRate
            else 44100

        val outPfd = context.contentResolver
            .openFileDescriptor(outUri, "rw")
            ?: throw RuntimeException("cannot open output")
        outPfd.use { pfd ->
            val muxer = MediaMuxer(pfd.fileDescriptor,
                MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4)
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

                    val adFormat = MediaFormat.createAudioFormat(
                        MediaFormat.MIMETYPE_AUDIO_AAC,
                        outRate, OUT_CHANNELS)
                    adFormat.setByteBuffer("csd-0",
                        ByteBuffer.wrap(aacCsd(outRate)))
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

                    // optional extra track: original audio + voice boost
                    // (AD + original tracks are left as they are)
                    var boostTrackOut = -1
                    var boostRate = 0
                    if (audioFormat != null && boost.addsTrack) {
                        val r = audioFormat.getInteger(
                            MediaFormat.KEY_SAMPLE_RATE)
                        boostRate = if (AAC_FREQ_IDX.containsKey(r)) r
                            else 48000
                        val bf = MediaFormat.createAudioFormat(
                            MediaFormat.MIMETYPE_AUDIO_AAC,
                            boostRate, OUT_CHANNELS)
                        bf.setByteBuffer("csd-0",
                            ByteBuffer.wrap(aacCsd(boostRate)))
                        boostTrackOut = muxer.addTrack(bf)
                    }
                    if (boost.level != VoiceBoost.Level.OFF)
                        log("voice boost: $boost")

                    muxer.start()
                    val muxerLock = Any()
                    val durationUs =
                        if (videoFormat.containsKey(MediaFormat.KEY_DURATION))
                            videoFormat.getLong(MediaFormat.KEY_DURATION)
                        else 0L

                    // copy thread: video samples, then original audio -
                    // I/O-bound, hides inside the AD encode below
                    var copyError: Throwable? = null
                    val copyThread = Thread {
                        try {
                            log("copying video stream...")
                            copyTrack(vext, videoTrackIn, muxer,
                                muxerLock, videoTrackOut)
                            logT("video copy done")
                            if (origTrackOut >= 0) {
                                log("copying original audio...")
                                copyTrack(vext, audioTrackIn, muxer,
                                    muxerLock, origTrackOut)
                                logT("original audio copy done")
                            }
                        } catch (t: Throwable) { copyError = t }
                    }.apply { start() }

                    var boostError: Throwable? = null
                    val boostThread = if (boostTrackOut < 0) null else
                        Thread {
                            try {
                                VoiceBoost.writeTrack(context, videoUri,
                                    muxer, muxerLock, boostTrackOut,
                                    boostRate, boost.level)
                                logT("voice-boost track done")
                            } catch (t: Throwable) { boostError = t }
                        }.apply { start() }

                    log("writing aligned AD audio...")
                    var adError: Throwable? = null
                    try {
                        writeAdTrack(context, adUri, muxer, muxerLock,
                            adTrackOut, a, bSec, outRate, onProgress,
                            pieces,
                            if (boost.boostsAd) boost.level
                            else VoiceBoost.Level.OFF)
                    } catch (t: Throwable) { adError = t }
                    copyThread.join()
                    boostThread?.join()
                    logT("AD encode + mux done")
                    (adError ?: copyError ?: boostError)?.let { throw it }

                    muxer.stop()
                    muxer.release()
                }
        }
    }

    /** Copy every sample of one extractor track into a muxer track. */
    private fun copyTrack(
        extractor: MediaExtractor, trackIn: Int,
        muxer: MediaMuxer, muxerLock: Any, trackOut: Int
    ) {
        synchronized(extractor) {
            extractor.selectTrack(trackIn)
            extractor.seekTo(0, MediaExtractor.SEEK_TO_PREVIOUS_SYNC)
        }
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
            synchronized(muxerLock) {
                muxer.writeSampleData(trackOut, buf, info)
            }
            extractor.advance()
        }
        extractor.unselectTrack(trackIn)
    }

    internal fun encoderFormat(outRate: Int): MediaFormat =
        MediaFormat.createAudioFormat(
            MediaFormat.MIMETYPE_AUDIO_AAC, outRate, OUT_CHANNELS).apply {
            setInteger(MediaFormat.KEY_BIT_RATE, AD_BITRATE)
            setInteger(MediaFormat.KEY_AAC_PROFILE,
                MediaCodecInfo.CodecProfileLevel.AACObjectLC)
            setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, 1 shl 16)
        }

    // FDK's AAC-LC delay; used only if the loopback below fails
    private const val FALLBACK_ENC_DELAY = 2048
    private val delayCache = HashMap<Int, Int>()

    /**
     * Total delay (frames) between the AAC encoder's input and what a
     * decoder plays back. MediaMuxer writes no edit list, so players and
     * ffmpeg play the encoder's priming as content: on the CMF Phone 1
     * c2.android.aac.encoder put the whole AD track ~46 ms late. Measured
     * once per rate by a loopback - encode a noise burst, decode it, find
     * it - so it holds for whatever encoder the phone picks.
     */
    internal fun encoderDelay(outRate: Int): Int = synchronized(delayCache) {
        delayCache.getOrPut(outRate) {
            try {
                measureEncoderDelay(outRate).also {
                    android.util.Log.i(TAG, "AAC encoder delay: $it frames")
                }
            } catch (t: Throwable) {
                android.util.Log.w(TAG, "encoder delay loopback failed " +
                    "($t) - assuming $FALLBACK_ENC_DELAY frames")
                FALLBACK_ENC_DELAY
            }
        }
    }

    private fun measureEncoderDelay(outRate: Int): Int {
        val lead = 8192
        val burst = 8192
        val total = lead + burst + 8192
        val noise = FloatArray(burst)
        var seed = 12345
        for (i in 0 until burst) {
            seed = seed * 1103515245 + 12345
            noise[i] = ((seed ushr 16) and 0x7fff) / 16384f - 1f
        }
        val order = java.nio.ByteOrder.nativeOrder()
        val inputs = ArrayList<Pair<ByteArray, Long>>()
        for (f0 in 0 until total step 1024) {
            val bb = ByteBuffer.allocate(1024 * 2 * OUT_CHANNELS).order(order)
            for (i in f0 until f0 + 1024) {
                val v = if (i - lead in 0 until burst)
                    (noise[i - lead] * 8000).toInt().toShort() else 0
                repeat(OUT_CHANNELS) { bb.putShort(v) }
            }
            inputs += bb.array() to f0 * 1_000_000L / outRate
        }
        val packets = ArrayList<Pair<ByteArray, Long>>()
        var csd: ByteArray? = null
        val enc = MediaCodec.createEncoderByType(
            MediaFormat.MIMETYPE_AUDIO_AAC)
        try {
            enc.configure(encoderFormat(outRate), null, null,
                MediaCodec.CONFIGURE_FLAG_ENCODE)
            enc.start()
            runCodec(enc, inputs) { b, info ->
                if (info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG != 0)
                    csd = b
                else packets += b to info.presentationTimeUs
            }
        } finally { enc.release() }
        val df = MediaFormat.createAudioFormat(
            MediaFormat.MIMETYPE_AUDIO_AAC, outRate, OUT_CHANNELS)
        df.setByteBuffer("csd-0", ByteBuffer.wrap(csd ?: aacCsd(outRate)))
        val pcm = java.io.ByteArrayOutputStream()
        var ch = OUT_CHANNELS
        val dec = MediaCodec.createDecoderByType(
            MediaFormat.MIMETYPE_AUDIO_AAC)
        try {
            dec.configure(df, null, null, 0)
            dec.start()
            runCodec(dec, packets) { b, _ -> pcm.write(b) }
            ch = dec.outputFormat.getInteger(MediaFormat.KEY_CHANNEL_COUNT)
        } finally { dec.release() }
        val sb = ByteBuffer.wrap(pcm.toByteArray()).order(order)
            .asShortBuffer()
        val frames = sb.remaining() / ch
        if (frames < lead + burst) throw RuntimeException("short loopback")
        val out = FloatArray(frames) { sb.get(it * ch) / 8000f }
        val cc = Aligner.crossCorrelate(out, noise,
            Aligner.nextPow2(frames + burst))
        val pre = DoubleArray(frames + 1)
        for (i in 0 until frames)
            pre[i + 1] = pre[i] + out[i].toDouble() * out[i]
        var nn = 0.0
        for (v in noise) nn += v.toDouble() * v
        var best = -1
        var bestScore = 0.0
        for (k in lead..frames - burst) {
            val sc = cc[k] / (Math.sqrt(nn * (pre[k + burst] - pre[k])) + 1e-9)
            if (sc > bestScore) { bestScore = sc; best = k }
        }
        if (best < 0 || bestScore < 0.5)
            throw RuntimeException("no loopback match (%.2f)".format(
                bestScore))
        return best - lead
    }

    /** Push [inputs] through a started codec, then EOS; collect output. */
    private fun runCodec(
        codec: MediaCodec, inputs: List<Pair<ByteArray, Long>>,
        onOut: (ByteArray, MediaCodec.BufferInfo) -> Unit
    ) {
        val info = MediaCodec.BufferInfo()
        var next = 0
        var eosSent = false
        repeat(20_000) {
            if (!eosSent) {
                val idx = codec.dequeueInputBuffer(5_000)
                if (idx >= 0) {
                    if (next < inputs.size) {
                        val (data, pts) = inputs[next++]
                        codec.getInputBuffer(idx)!!.apply { clear(); put(data) }
                        codec.queueInputBuffer(idx, 0, data.size, pts, 0)
                    } else {
                        codec.queueInputBuffer(idx, 0, 0, 0,
                            MediaCodec.BUFFER_FLAG_END_OF_STREAM)
                        eosSent = true
                    }
                }
            }
            while (true) {
                val o = codec.dequeueOutputBuffer(info,
                    if (eosSent) 5_000 else 0)
                if (o == MediaCodec.INFO_TRY_AGAIN_LATER) break
                if (o < 0) continue
                if (info.size > 0) {
                    val b = ByteArray(info.size)
                    codec.getOutputBuffer(o)!!.apply {
                        position(info.offset); get(b)
                    }
                    onOut(b, info)
                }
                codec.releaseOutputBuffer(o, false)
                if (info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0)
                    return
            }
        }
        throw RuntimeException("codec loopback timed out")
    }

    /**
     * Decode the AD file on a feeder thread, resample/shift on this
     * thread with an incremental position accumulator, AAC-encode, and
     * mux. Only a small PCM window is ever in memory.
     */
    private fun writeAdTrack(
        context: Context, adUri: Uri, muxer: MediaMuxer, muxerLock: Any,
        track: Int, a: Double, bSec: Double, outRate: Int,
        onProgress: (Float) -> Unit,
        pieces: List<Aligner.Piece> = emptyList(),
        boostLevel: VoiceBoost.Level = VoiceBoost.Level.OFF
    ) {
        // Different edit: AD is placed piece by piece (output frame ranges
        // [startJ, endJ) read AD from adFrom on), silence in gaps, 10 ms
        // fades at splices. One piece = the plain linear path below.
        val placed = if (pieces.size > 1) Aligner.planPieces(a, pieces)
        else emptyList()
        val segStart = LongArray(placed.size) {
            Math.ceil(placed[it].at * outRate).toLong()
        }
        val segEnd = LongArray(placed.size) {
            ((placed[it].at + (placed[it].adTo - placed[it].adFrom) * a) *
                outRate).toLong()
        }
        var seg = 0
        var finishedEarly = false
        val fadeN = outRate / 100
        // the encoder's priming plays as content (no edit list): render
        // from that many frames in, so the decoded audio lands on time
        val delay = encoderDelay(outRate)
        val encoder = MediaCodec.createEncoderByType(
            MediaFormat.MIMETYPE_AUDIO_AAC)
        encoder.configure(encoderFormat(outRate), null, null,
            MediaCodec.CONFIGURE_FLAG_ENCODE)
        encoder.start()
        val sink = EncoderSink(encoder, muxer, muxerLock, track, outRate,
            delay, if (boostLevel == VoiceBoost.Level.OFF) null
            else VoiceBoost(outRate, boostLevel))

        // feeder: decoder runs concurrently, pushing into a bounded queue
        val sentinel = AudioEngine.PcmChunk(ShortArray(0), 0, 0, -1)
        val queue = ArrayBlockingQueue<AudioEngine.PcmChunk>(64)
        var decodeError: Throwable? = null
        val feeder = Thread {
            try {
                AudioEngine.decodeAudio(context, adUri,
                    onProgress = onProgress) { queue.put(it) }
            } catch (t: Throwable) {
                decodeError = t
            } finally {
                // offer (not put): if the consumer died, don't hang forever
                queue.offer(sentinel, 5,
                    java.util.concurrent.TimeUnit.SECONDS)
            }
        }.apply { start() }

        // sliding window of decoded stereo PCM, absolute frame indexing
        val capacity = 1 shl 19
        val window = FloatArray(capacity * OUT_CHANNELS)
        var winStart = 0L
        var winCount = 0
        var inRate = 0
        var outFrame = delay.toLong()
        var totalIn = 0L
        var inputDone = false
        // incremental resampler state: p = input-frame position of the
        // next output frame; advances by `step` per output frame
        var p = 0.0
        var step = 0.0

        val frame = ShortArray(1024 * OUT_CHANNELS)

        fun pump() {
            if (inRate == 0 || finishedEarly) return
            while (true) {
                var produced = 0
                while (produced < 1024) {
                    var gain = 1f
                    if (placed.isNotEmpty()) {
                        while (seg < placed.size && outFrame >= segEnd[seg])
                            seg++
                        if (seg >= placed.size) {   // past the last piece
                            if (produced > 0) sink.feed(frame, produced,
                                outFrame - produced)
                            sink.finish()
                            finishedEarly = true
                            return
                        }
                        if (outFrame == segStart[seg] ||
                            (outFrame > segStart[seg] && p < 0)) {
                            // entering a piece: jump to its AD position
                            p = (placed[seg].adFrom +
                                (outFrame.toDouble() / outRate -
                                    placed[seg].at) / a) * inRate
                        }
                        if (outFrame < segStart[seg]) {
                            p = -1.0   // gap: silence until the piece
                        } else {
                            gain = minOf(1f,
                                (outFrame - segStart[seg] + 1).toFloat() / fadeN,
                                (segEnd[seg] - outFrame).toFloat() / fadeN)
                        }
                    }
                    if (p < 0) {
                        frame[produced * 2] = 0
                        frame[produced * 2 + 1] = 0
                    } else {
                        val i = p.toLong()
                        if (i + 1 >= winStart + winCount) {
                            if (!inputDone) {
                                if (produced > 0) sink.feed(frame,
                                    produced, outFrame - produced)
                                return
                            }
                            if (i + 1 >= totalIn) {
                                if (produced > 0) sink.feed(frame,
                                    produced, outFrame - produced)
                                sink.finish()
                                return
                            }
                        }
                        val local = ((i - winStart).toInt()) * OUT_CHANNELS
                        val f = (p - i).toFloat()
                        for (c in 0 until OUT_CHANNELS) {
                            val s0 = window[local + c]
                            val s1 = window[local + OUT_CHANNELS + c]
                            val v = (s0 + (s1 - s0) * f) * 32767f * gain
                            frame[produced * OUT_CHANNELS + c] =
                                v.coerceIn(-32768f, 32767f).toInt().toShort()
                        }
                    }
                    produced++; outFrame++
                    // linear: a negative p counts up through the leading
                    // silence; piecewise gaps hold p at -1 instead
                    if (placed.isEmpty() || p >= 0) p += step
                }
                sink.feed(frame, produced, outFrame - produced)
                // drop consumed window data (keep 4 frames of guard)
                val needed = p.toLong() - 4
                if (needed > winStart) {
                    val drop = min(needed - winStart, winCount.toLong())
                        .toInt()
                    System.arraycopy(window, drop * OUT_CHANNELS, window, 0,
                        (winCount - drop) * OUT_CHANNELS)
                    winStart += drop; winCount -= drop
                }
            }
        }

        while (true) {
            if (AudioEngine.cancelRequested) {
                feeder.interrupt()
                throw RuntimeException("cancelled")
            }
            val chunk = queue.take()
            if (chunk === sentinel) break
            if (finishedEarly) continue   // AD past the last piece
            if (inRate == 0) {
                inRate = chunk.sampleRate
                step = inRate / (outRate * a)
                p = if (placed.isEmpty())
                    ((outFrame.toDouble() / outRate - bSec) / a) * inRate
                else -1.0
            }
            val frames = chunk.samples.size / chunk.channels
            // input wholly before the first needed position (large trim):
            // skip it without buffering, or the window would overflow
            if (p > 4 && totalIn + frames < p.toLong() - 4) {
                totalIn += frames
                winStart = totalIn
                continue
            }
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
        decodeError?.let { throw it }
        inputDone = true
        pump()
        sink.finish()
    }

    /**
     * Feeds PCM into the AAC encoder and writes encoded packets to the
     * muxer (under the shared lock). CODEC_CONFIG packets are skipped
     * (csd-0 was set manually).
     */
    internal class EncoderSink(
        private val encoder: MediaCodec,
        private val muxer: MediaMuxer,
        private val muxerLock: Any,
        private val track: Int,
        private val outRate: Int,
        private val delay: Int,       // frames rendered ahead of pts 0
        private val fx: VoiceBoost? = null
    ) {
        private val info = MediaCodec.BufferInfo()
        private var finished = false

        fun feed(frames: ShortArray, count: Int, firstFrame: Long) {
            if (finished) return
            fx?.processShorts(frames, count)
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
                val ptsUs = (firstFrame + offset - delay) * 1_000_000L /
                    outRate
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
            while (tries++ < 50) {
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
                    synchronized(muxerLock) {
                        muxer.writeSampleData(track,
                            encoder.getOutputBuffer(idx)!!, info)
                    }
                }
                encoder.releaseOutputBuffer(idx, false)
                if (info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0)
                    return
            }
        }
    }
}
