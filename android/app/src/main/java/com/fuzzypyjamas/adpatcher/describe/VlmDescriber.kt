package com.fuzzypyjamas.adpatcher.describe

import android.graphics.Bitmap
import java.io.Closeable

/**
 * On-device vision-language model (llama.cpp libmtmd via JNI, see
 * src/main/cpp/vlm_jni.cpp): one frame + instruction -> one short line.
 * Model + mmproj are GGUF files in app storage (downloaded, never bundled).
 * Not thread-safe: calls are serialized on this instance.
 */
class VlmDescriber(
    modelPath: String,
    mmprojPath: String,
    threads: Int = 4,          // big cores only; 8 is slower on 4+4 SoCs
    nCtx: Int = 4096,
    imageMaxTokens: Int = 256, // caps Qwen-VL's dynamic resolution cost
) : Closeable {

    /** ms spent on image encode + prompt prefill / on generation, token counts. */
    data class Timings(val evalMs: Long, val genMs: Long, val promptTokens: Long, val genTokens: Long)

    private var handle: Long = nativeInit(modelPath, mmprojPath, threads, nCtx, imageMaxTokens)

    init {
        check(handle != 0L) { "VLM init failed (model=$modelPath)" }
    }

    @Synchronized
    fun describe(frame: Bitmap, prompt: String, maxTokens: Int = 48): String {
        check(handle != 0L) { "closed" }
        val w = frame.width
        val h = frame.height
        val px = IntArray(w * h)
        frame.getPixels(px, 0, w, 0, 0, w, h)
        val rgb = ByteArray(w * h * 3)
        var o = 0
        for (c in px) {
            rgb[o++] = (c shr 16).toByte()
            rgb[o++] = (c shr 8).toByte()
            rgb[o++] = c.toByte()
        }
        return nativeDescribe(handle, rgb, w, h, prompt, maxTokens)?.trim()
            ?: error("VLM describe failed")
    }

    @Synchronized
    fun lastTimings(): Timings {
        val t = nativeLastTimings(handle)
        return Timings(t[0], t[1], t[2], t[3])
    }

    @Synchronized
    override fun close() {
        if (handle != 0L) nativeFree(handle)
        handle = 0L
    }

    companion object {
        init {
            System.loadLibrary("adpvlm")
        }

        @JvmStatic private external fun nativeInit(
            model: String, mmproj: String, threads: Int, nCtx: Int, imageMaxTokens: Int,
        ): Long

        @JvmStatic private external fun nativeDescribe(
            handle: Long, rgb: ByteArray, w: Int, h: Int, prompt: String, maxTokens: Int,
        ): String?

        @JvmStatic private external fun nativeLastTimings(handle: Long): LongArray
        @JvmStatic private external fun nativeFree(handle: Long)
    }
}
