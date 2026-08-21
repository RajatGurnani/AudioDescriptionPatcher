package com.renderedideas.adpatcher

import android.content.Context
import android.net.Uri
import android.os.PowerManager
import android.util.Log
import java.io.File
import kotlin.concurrent.thread

/**
 * Owns a patch job end-to-end, independent of any Activity.
 *
 * The pipeline thread lives here (with the process kept foreground by
 * [PatchService]), so the job survives the UI being closed or swiped
 * away. MainActivity just attaches as a listener and renders state.
 *
 * Progress through the job is checkpointed into [JobStore]; if the app
 * dies mid-job, the next launch can offer to resume - and once the
 * alignment result is known, resuming skips the whole analysis phase.
 */
object JobRunner {
    private const val TAG = "ADPatcher"

    enum class Status { IDLE, RUNNING, DONE, FAILED, CANCELLED }

    interface Listener { fun onJobUpdate() }   // may fire on any thread

    @Volatile var status = Status.IDLE; private set
    @Volatile var stageBase = ""; private set
    @Volatile var stageDetail = ""; private set
    @Volatile var barProgress = 0; private set   // 0..1000
    @Volatile var outputName = ""; private set
    val logText = StringBuilder()                // guard: synchronized(this)

    @Volatile private var listener: Listener? = null
    @Volatile private var phaseStartMs = 0L
    @Volatile private var lastUiUpdate = 0L

    fun attach(l: Listener?) { listener = l; l?.onJobUpdate() }

    fun logSnapshot(): String = synchronized(this) { logText.toString() }

    private fun ping() { listener?.onJobUpdate() }

    private fun log(line: String) {
        Log.i(TAG, line)
        synchronized(this) { logText.append(line).append('\n') }
        ping()
    }

    private fun setStage(text: String) {
        stageBase = text
        stageDetail = ""
        phaseStartMs = System.currentTimeMillis()
        ping()
    }

    /** Phase-local progress -> global bar + "42% · ~3 min left" detail. */
    private fun phaseProgress(from: Int, to: Int): (Float) -> Unit = { f ->
        val now = System.currentTimeMillis()
        if (f >= 1f || now - lastUiUpdate > 150) {
            lastUiUpdate = now
            val target = from + ((to - from) * f).toInt()
            if (target > barProgress) barProgress = target
            if (f in 0.03f..0.999f && phaseStartMs > 0) {
                val elapsed = now - phaseStartMs
                val remain = (elapsed * (1 - f) / f).toLong()
                val eta = if (remain > 90_000)
                    "~${remain / 60_000 + 1} min left"
                else "~${remain / 1000 + 1}s left"
                stageDetail = "${(f * 100).toInt()}% · $eta"
            }
            appContext?.let {
                PatchService.updateProgress(it, barProgress / 10)
            }
            ping()
        }
    }

    private fun <T> timed(name: String, block: () -> T): T {
        val start = android.os.SystemClock.elapsedRealtime()
        val r = block()
        log("⏱ %s: %.1fs".format(name,
            (android.os.SystemClock.elapsedRealtime() - start) / 1000f))
        return r
    }

    @Volatile private var appContext: Context? = null

    /**
     * Start a job. With [resumeA]/[resumeB] set (from a saved manifest),
     * the analysis + alignment phases are skipped entirely.
     */
    fun start(context: Context, videoUri: Uri, adUri: Uri, outUri: Uri,
              resumeA: Double? = null, resumeB: Double? = null) {
        if (status == Status.RUNNING) return
        val app = context.applicationContext
        appContext = app
        status = Status.RUNNING
        barProgress = 0
        synchronized(this) { logText.setLength(0) }
        AudioEngine.cancelRequested = false

        JobStore.saveJob(app, videoUri, adUri, outUri, resumeA, resumeB)
        PatchService.start(app)
        val wakeLock = (app.getSystemService(Context.POWER_SERVICE)
                as PowerManager).newWakeLock(
            PowerManager.PARTIAL_WAKE_LOCK, "adpatcher:job")
        wakeLock.acquire(6 * 60 * 60 * 1000L)

        log("ADPatcher ${BuildConfig.VERSION_NAME} on " +
            "${android.os.Build.MANUFACTURER} ${android.os.Build.MODEL}, " +
            "Android ${android.os.Build.VERSION.RELEASE}, " +
            "${Runtime.getRuntime().availableProcessors()} cores")
        if (resumeA != null) log("resuming with saved alignment " +
                "(analysis skipped)")

        thread(name = "adpatch-job") {
            try {
                val a: Double
                val b: Double
                if (resumeA != null && resumeB != null) {
                    a = resumeA; b = resumeB
                    barProgress = 700
                } else {
                    setStage("analyzing both audio streams…")
                    var vidEnv: FloatArray? = null
                    var adEnv: FloatArray? = null
                    var decodeError: Throwable? = null
                    var pv = 0f
                    var pa = 0f
                    val combined = phaseProgress(0, 550)
                    timed("audio analysis") {
                        val tv = Thread {
                            try {
                                vidEnv = AudioEngine.onsetEnvelopeParallel(
                                    app, videoUri, 2)
                                { f -> pv = f; combined((pv + pa) * 0.5f) }
                            } catch (t: Throwable) { decodeError = t }
                        }
                        val ta = Thread {
                            try {
                                adEnv = AudioEngine.onsetEnvelopeParallel(
                                    app, adUri, 2)
                                { f -> pa = f; combined((pv + pa) * 0.5f) }
                            } catch (t: Throwable) { decodeError = t }
                        }
                        tv.start(); ta.start(); tv.join(); ta.join()
                    }
                    decodeError?.let { throw it }
                    log("video: %.1f min".format(
                        vidEnv!!.size / AudioEngine.FPS / 60))
                    log("AD audio: %.1f min".format(
                        adEnv!!.size / AudioEngine.FPS / 60))

                    setStage("aligning (speed + offset scan)…")
                    val result = timed("alignment") {
                        Aligner.fitAlignment(adEnv!!, vidEnv!!,
                            ::log, phaseProgress(550, 700))
                    }
                    a = result.a; b = result.bSec
                    log("offset %+.3fs, speed %.6f".format(b, a))
                    for (cp in result.report) {
                        log("  %5.1fm -> %5.1fm  score %.2f  %+dms%s"
                            .format(cp.adSec / 60, cp.videoSec / 60,
                                cp.score, cp.residualMs.toInt(),
                                if (cp.score <= Aligner.WEAK_SCORE)
                                    "  (weak)" else ""))
                    }
                    if (result.suspect)
                        log("⚠ most segments matched poorly - the AD " +
                            "file may be for a different cut!")
                    // checkpoint: from here a resume can skip to muxing
                    JobStore.saveAlignment(app, a, b)
                }

                setStage("writing output…")
                timed("writing output") {
                    Patcher.mux(app, videoUri, adUri, outUri, a, b,
                        ::log, phaseProgress(700, 1000))
                }

                barProgress = 1000
                stageBase = "done ✅ saved as new file"
                stageDetail = ""
                status = Status.DONE
                JobStore.markComplete(app)
                dumpSessionLog(app)?.let { log("session log: $it") }
            } catch (t: Throwable) {
                if (t.message == "cancelled") {
                    stageBase = "cancelled ✋"
                    status = Status.CANCELLED
                    log("job cancelled by user")
                    JobStore.markComplete(app)   // don't offer resume
                } else {
                    stageBase = "failed ❌"
                    status = Status.FAILED
                    log("error: ${t.message ?: t.toString()}")
                    Log.e(TAG, "patch failed", t)
                    dumpSessionLog(app)?.let { log("session log: $it") }
                }
                stageDetail = ""
            } finally {
                wakeLock.release()
                PatchService.stop(app)
                ping()
            }
        }
    }

    fun cancel() {
        if (status == Status.RUNNING) {
            AudioEngine.cancelRequested = true
            stageBase = "cancelling…"
            ping()
        }
    }

    fun logsDir(context: Context): File =
        File(context.getExternalFilesDir(null), "logs")

    private fun dumpSessionLog(context: Context): String? = try {
        val dir = logsDir(context)
        dir.mkdirs()
        val f = File(dir, "adpatch-${System.currentTimeMillis()}.log")
        f.writeText(logSnapshot())
        f.absolutePath
    } catch (e: Exception) {
        Log.w(TAG, "could not write session log", e)
        null
    }
}
