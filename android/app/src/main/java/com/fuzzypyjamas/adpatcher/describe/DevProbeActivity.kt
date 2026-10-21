package com.fuzzypyjamas.adpatcher.describe

import android.app.Activity
import android.graphics.BitmapFactory
import android.net.Uri
import android.os.Bundle
import android.os.Debug
import android.os.SystemClock
import android.util.Log
import com.fuzzypyjamas.adpatcher.AudioEngine
import java.io.File
import kotlin.concurrent.thread

/**
 * Developer probe for AI audio description (docs/plan/10), logs to
 * logcat tag ADP-VLM. Only adb can start it (android.permission.DUMP).
 * Files live under getExternalFilesDir(): models/, frames/, clips/, out/.
 *
 *   # A1: caption every jpg/png in frames/ with a VLM
 *   adb shell am start -n com.fuzzypyjamas.adpatcher/.describe.DevProbeActivity \
 *       --es model X.gguf --es mmproj mmproj-X.gguf [--ei maxImgTok 256]
 *   # A2: narration gaps of clips/NAME -> out/NAME.gaps.json
 *   adb shell am start -n com.fuzzypyjamas.adpatcher/.describe.DevProbeActivity \
 *       --es task gaps --es clip NAME [--ei workers 4] [--ei threads 2]
 */
class DevProbeActivity : Activity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val task = intent.getStringExtra("task") ?: "vlm"
        thread(name = "dev-probe") {
            try {
                when (task) {
                    "gaps" -> runGaps(intent.getStringExtra("clip") ?: "")
                    "decode" -> runDecode(intent.getStringExtra("clip") ?: "")
                    else -> runVlm()
                }
            } catch (t: Throwable) {
                Log.e(TAG, "probe failed", t)
            }
            Log.i(TAG, "DONE")
            runOnUiThread { finish() }
        }
    }

    private fun runGaps(clip: String) {
        val src = File(getExternalFilesDir("clips"), clip)
        val out = File(getExternalFilesDir("out"), "$clip.gaps.json")
        val t0 = SystemClock.elapsedRealtime()
        var lastPct = -1
        var split = ""
        val workers = intent.getIntExtra("workers", 4)
        val threads = intent.getIntExtra("threads", 2)
        val res = GapFinder(this, workers, threads).use { gf ->
            gf.analyze(this, Uri.fromFile(src)) { f ->
                val pct = (f * 100).toInt()
                if (pct / 10 != lastPct / 10) { lastPct = pct; Log.i(TAG, "decode $pct%") }
            }.also { split = "wall ${gf.wallMs} ms, model ${gf.modelMs} ms summed (workers=$workers threads/run=$threads)" }
        }
        Log.i(TAG, split)
        val ms = SystemClock.elapsedRealtime() - t0
        out.writeText(res.toJson().toString())
        Log.i(TAG, "gaps $clip: ${res.speech.size} speech, ${res.gaps.size} gaps " +
            "(${res.gaps.sumOf { it[1] - it[0] }.toInt()} s) in ${res.durationS.toInt()} s audio; " +
            "$ms ms (${"%.1f".format(res.durationS * 1000 / ms)}x realtime), ${memMb()} -> ${out.name}")
    }

    /** Decode-only and decode+resample timings (no model). */
    private fun runDecode(clip: String) {
        val uri = Uri.fromFile(File(getExternalFilesDir("clips"), clip))
        var n = 0L
        var t = SystemClock.elapsedRealtime()
        AudioEngine.decodeAudio(this, uri) { n += it.samples.size }
        Log.i(TAG, "decode only: ${SystemClock.elapsedRealtime() - t} ms, $n samples")
        t = SystemClock.elapsedRealtime()
        val m = GapFinder.Mono16k()
        var inFeed = 0L
        AudioEngine.decodeAudio(this, uri) {
            val a = System.nanoTime(); m.feed(it); m.discardBefore(m.endAbs); inFeed += System.nanoTime() - a
        }
        Log.i(TAG, "decode+resample: ${SystemClock.elapsedRealtime() - t} ms (${inFeed / 1_000_000} ms in resampler), ${m.endAbs} out")
    }

    private fun runVlm() {
        val models = getExternalFilesDir("models")!!
        val frames = getExternalFilesDir("frames")!!
        val model = File(models, intent.getStringExtra("model") ?: "")
        val mmproj = File(models, intent.getStringExtra("mmproj") ?: "")
        val maxImgTok = intent.getIntExtra("maxImgTok", 256)
        val threads = intent.getIntExtra("threads", 4)
        val prompt = intent.getStringExtra("prompt") ?: PROMPT
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
