package com.fuzzypyjamas.adpatcher.describe

import android.app.Activity
import android.graphics.BitmapFactory
import android.os.Bundle
import android.os.Debug
import android.os.SystemClock
import android.util.Log
import java.io.File
import kotlin.concurrent.thread

/**
 * Developer probe for iteration A1 (docs/plan/10): loads a VLM in-process
 * and captions frames, logging timings + memory to logcat (tag ADP-VLM).
 * Only adb can start it (android.permission.DUMP). Files are read from
 * getExternalFilesDir("models") / ("frames").
 *
 *   adb shell am start -n com.fuzzypyjamas.adpatcher/.describe.VlmProbeActivity \
 *       --es model X.gguf --es mmproj mmproj-X.gguf [--ei maxImgTok 256]
 */
class VlmProbeActivity : Activity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val models = getExternalFilesDir("models")!!
        val frames = getExternalFilesDir("frames")!!
        val model = File(models, intent.getStringExtra("model") ?: "")
        val mmproj = File(models, intent.getStringExtra("mmproj") ?: "")
        val maxImgTok = intent.getIntExtra("maxImgTok", 256)
        val threads = intent.getIntExtra("threads", 4)
        val prompt = intent.getStringExtra("prompt") ?: PROMPT
        thread(name = "vlm-probe") {
            try {
                run(model, mmproj, frames, threads, maxImgTok, prompt)
            } catch (t: Throwable) {
                Log.e(TAG, "probe failed", t)
            }
            Log.i(TAG, "DONE")
            runOnUiThread { finish() }
        }
    }

    private fun run(model: File, mmproj: File, frames: File, threads: Int, maxImgTok: Int, prompt: String) {
        val t0 = SystemClock.elapsedRealtime()
        VlmDescriber(model.path, mmproj.path, threads, imageMaxTokens = maxImgTok).use { vlm ->
            Log.i(TAG, "load ${model.name}: ${SystemClock.elapsedRealtime() - t0} ms, ${memMb()}")
            val files = frames.listFiles { f -> f.extension in setOf("jpg", "png") }!!.sorted()
            for (f in files) {
                val bmp = BitmapFactory.decodeFile(f.path) ?: continue
                val t = SystemClock.elapsedRealtime()
                val line = vlm.describe(bmp, prompt)
                val tm = vlm.lastTimings()
                Log.i(TAG, "${f.name} ${bmp.width}x${bmp.height}: ${SystemClock.elapsedRealtime() - t} ms " +
                    "(eval ${tm.evalMs} ms/${tm.promptTokens} tok, gen ${tm.genMs} ms/${tm.genTokens} tok) " +
                    "${memMb()} | $line")
            }
        }
    }

    private fun memMb(): String {
        val mi = Debug.MemoryInfo().also { Debug.getMemoryInfo(it) }
        return "pss=${mi.totalPss / 1024} MB native=${Debug.getNativeHeapAllocatedSize() / (1 shl 20)} MB"
    }

    companion object {
        private const val TAG = "ADP-VLM"
        const val PROMPT = "Describe this film frame in one short sentence for audio description: " +
            "present tense, third person, only what is visible, no interpretation, at most 15 words."
    }
}
