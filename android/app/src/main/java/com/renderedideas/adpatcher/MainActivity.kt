package com.renderedideas.adpatcher

import android.content.Context
import android.net.Uri
import android.os.Bundle
import android.os.PowerManager
import android.provider.OpenableColumns
import android.view.WindowManager
import android.widget.Button
import android.widget.ProgressBar
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import kotlin.concurrent.thread

/**
 * One-screen flow: pick video -> pick AD audio -> pick where to save ->
 * watch progress. The heavy lifting lives in AudioEngine (decode +
 * envelopes), Aligner (find offset/speed) and Patcher (write the MP4).
 *
 * A partial wake lock + FLAG_KEEP_SCREEN_ON hold the device awake for the
 * whole job so long movies don't get killed mid-patch.
 */
class MainActivity : AppCompatActivity() {

    private var videoUri: Uri? = null
    private var adUri: Uri? = null

    private lateinit var videoName: TextView
    private lateinit var adName: TextView
    private lateinit var goButton: Button
    private lateinit var stage: TextView
    private lateinit var bar: ProgressBar
    private lateinit var logView: TextView

    private val pickVideo = registerForActivityResult(
        ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) {
            contentResolver.takePersistableUriPermission(uri,
                android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION)
            videoUri = uri
            videoName.text = displayName(uri)
            updateButton()
        }
    }

    private val pickAd = registerForActivityResult(
        ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) {
            contentResolver.takePersistableUriPermission(uri,
                android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION)
            adUri = uri
            adName.text = displayName(uri)
            updateButton()
        }
    }

    private val createOutput = registerForActivityResult(
        ActivityResultContracts.CreateDocument("video/mp4")) { uri ->
        if (uri != null) runPatch(uri)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        videoName = findViewById(R.id.videoName)
        adName = findViewById(R.id.adName)
        goButton = findViewById(R.id.goButton)
        stage = findViewById(R.id.stage)
        bar = findViewById(R.id.bar)
        logView = findViewById(R.id.log)

        // "*/*" keeps files selectable even when the provider reports an
        // unexpected mime type (mkv often shows as octet-stream or worse)
        findViewById<Button>(R.id.pickVideo).setOnClickListener {
            pickVideo.launch(arrayOf("video/*",
                "application/octet-stream", "*/*"))
        }
        findViewById<Button>(R.id.pickAd).setOnClickListener {
            pickAd.launch(arrayOf("audio/*", "video/*",
                "application/octet-stream", "*/*"))
        }
        goButton.setOnClickListener {
            val base = displayName(videoUri!!).substringBeforeLast('.')
            createOutput.launch("$base.AD.mp4")
        }
    }

    private fun updateButton() {
        goButton.isEnabled = videoUri != null && adUri != null
    }

    private fun displayName(uri: Uri): String {
        contentResolver.query(uri, null, null, null, null)?.use { c ->
            val idx = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
            if (idx >= 0 && c.moveToFirst()) return c.getString(idx)
        }
        return uri.lastPathSegment ?: "file"
    }

    private fun ui(block: () -> Unit) = runOnUiThread(block)

    private fun log(line: String) = ui {
        logView.append(line + "\n")
    }

    private fun setStage(text: String) = ui { stage.text = text }

    /** Map a phase's 0..1 progress into the overall 0..1000 bar,
     *  throttled so decode callbacks don't flood the UI thread. */
    @Volatile private var lastBarUpdate = 0L
    private fun phaseProgress(from: Int, to: Int): (Float) -> Unit = { f ->
        val now = System.currentTimeMillis()
        if (f >= 1f || now - lastBarUpdate > 100) {
            lastBarUpdate = now
            val target = from + ((to - from) * f).toInt()
            // never move backwards - jitter reads as flicker
            ui { if (target > bar.progress) bar.progress = target }
        }
    }

    private fun runPatch(outUri: Uri) {
        goButton.isEnabled = false
        bar.progress = 0
        logView.text = ""

        // stay awake for the whole job (long movies take a while)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        val wakeLock = (getSystemService(Context.POWER_SERVICE)
                as PowerManager).newWakeLock(
            PowerManager.PARTIAL_WAKE_LOCK, "adpatcher:patch")
        wakeLock.acquire(4 * 60 * 60 * 1000L)   // 4 h safety limit

        thread {
            try {
                // decode both audio streams concurrently (separate codecs).
                // Progress is COMBINED into one monotonic value - two
                // threads writing separate bar ranges makes it flicker.
                setStage("analyzing both audio streams…")
                var vidEnv: FloatArray? = null
                var adEnv: FloatArray? = null
                var decodeError: Throwable? = null
                var pv = 0f
                var pa = 0f
                val combined = phaseProgress(0, 550)
                val tv = Thread {
                    try {
                        vidEnv = AudioEngine.onsetEnvelope(this, videoUri!!)
                        { f -> pv = f; combined((pv + pa) * 0.5f) }
                    } catch (t: Throwable) { decodeError = t }
                }
                val ta = Thread {
                    try {
                        adEnv = AudioEngine.onsetEnvelope(this, adUri!!)
                        { f -> pa = f; combined((pv + pa) * 0.5f) }
                    } catch (t: Throwable) { decodeError = t }
                }
                tv.start(); ta.start(); tv.join(); ta.join()
                decodeError?.let { throw it }
                log("video: %.1f min".format(
                    vidEnv!!.size / AudioEngine.FPS / 60))
                log("AD audio: %.1f min".format(
                    adEnv!!.size / AudioEngine.FPS / 60))

                setStage("aligning (speed + offset scan)…")
                val result = Aligner.fitAlignment(adEnv!!, vidEnv!!,
                    ::log, phaseProgress(550, 700))
                log("offset %+.3fs, speed %.6f"
                    .format(result.bSec, result.a))
                for (cp in result.report) {
                    log("  %5.1fm -> %5.1fm  score %.2f  %+dms%s".format(
                        cp.adSec / 60, cp.videoSec / 60, cp.score,
                        cp.residualMs.toInt(),
                        if (cp.score <= Aligner.WEAK_SCORE)
                            "  (weak)" else ""))
                }
                if (result.suspect) log("⚠ most segments matched poorly - " +
                        "the AD file may be for a different cut!")

                setStage("writing output…")
                Patcher.mux(this, videoUri!!, adUri!!, outUri,
                    result.a, result.bSec, ::log, phaseProgress(700, 1000))

                setStage("done ✅ saved as new file")
                ui { bar.progress = 1000 }
            } catch (t: Throwable) {
                setStage("failed ❌")
                log("error: ${t.message ?: t.toString()}")
            } finally {
                wakeLock.release()
                ui {
                    window.clearFlags(
                        WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
                    goButton.isEnabled = true
                }
            }
        }
    }
}
