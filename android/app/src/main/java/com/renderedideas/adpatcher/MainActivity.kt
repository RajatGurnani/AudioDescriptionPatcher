package com.renderedideas.adpatcher

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.provider.OpenableColumns
import android.view.WindowManager
import android.widget.Button
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import java.io.File

/**
 * UI shell over [JobRunner]: pick video -> pick AD audio -> pick where to
 * save -> watch progress. The job itself runs in JobRunner under the
 * foreground [PatchService], so it survives this activity being closed
 * or swiped away; reopening the app reattaches to the live job.
 */
class MainActivity : AppCompatActivity(), JobRunner.Listener {

    private var videoUri: Uri? = null
    private var adUri: Uri? = null

    private lateinit var videoName: TextView
    private lateinit var adName: TextView
    private lateinit var goButton: Button
    private lateinit var stage: TextView
    private lateinit var bar: ProgressBar
    private lateinit var logView: TextView

    private var lastRenderedStatus: JobRunner.Status? = null

    private val pickVideo = registerForActivityResult(
        ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) {
            contentResolver.takePersistableUriPermission(uri,
                Intent.FLAG_GRANT_READ_URI_PERMISSION)
            videoUri = uri
            videoName.text = displayName(uri)
            JobStore.saveLastInputs(this, uri, null)
            updateButton()
        }
    }

    private val pickAd = registerForActivityResult(
        ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) {
            contentResolver.takePersistableUriPermission(uri,
                Intent.FLAG_GRANT_READ_URI_PERMISSION)
            adUri = uri
            adName.text = displayName(uri)
            JobStore.saveLastInputs(this, null, uri)
            updateButton()
        }
    }

    private val createOutput = registerForActivityResult(
        ActivityResultContracts.CreateDocument("video/mp4")) { uri ->
        if (uri != null) {
            try {
                contentResolver.takePersistableUriPermission(uri,
                    Intent.FLAG_GRANT_READ_URI_PERMISSION or
                    Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
            } catch (_: Exception) { /* some providers refuse; job still runs */ }
            startJob(uri)
        }
    }

    private val askNotifPermission = registerForActivityResult(
        ActivityResultContracts.RequestPermission()) { /* best effort */ }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        videoName = findViewById(R.id.videoName)
        adName = findViewById(R.id.adName)
        goButton = findViewById(R.id.goButton)
        stage = findViewById(R.id.stage)
        bar = findViewById(R.id.bar)
        logView = findViewById(R.id.log)

        findViewById<Button>(R.id.pickVideo).setOnClickListener {
            pickVideo.launch(arrayOf("video/*",
                "application/octet-stream", "*/*"))
        }
        findViewById<Button>(R.id.pickAd).setOnClickListener {
            pickAd.launch(arrayOf("audio/*", "video/*",
                "application/octet-stream", "*/*"))
        }
        goButton.setOnClickListener {
            if (JobRunner.status == JobRunner.Status.RUNNING) {
                JobRunner.cancel()
            } else {
                val base = displayName(videoUri!!).substringBeforeLast('.')
                createOutput.launch("$base.AD.mp4")
            }
        }
        findViewById<Button>(R.id.btnVault).setOnClickListener {
            startActivity(Intent(this, VaultActivity::class.java))
        }
        findViewById<Button>(R.id.btnLogs).setOnClickListener {
            startActivity(Intent(this, LogsActivity::class.java))
        }
        findViewById<Button>(R.id.btnUpdate).setOnClickListener {
            UpdateChecker.check(this, manual = true)
        }
        findViewById<Button>(R.id.btnClear).setOnClickListener {
            confirmClearCache()
        }

        restoreInputs()
        offerResumeIfAny()
        UpdateChecker.check(this, manual = false)
    }

    override fun onStart() {
        super.onStart()
        JobRunner.attach(this)
    }

    override fun onStop() {
        JobRunner.attach(null)
        super.onStop()
    }

    override fun onResume() {
        super.onResume()
        // an AudioVault download finished while we were away?
        JobStore.takeVaultDownload(this)?.let { path ->
            val f = File(path)
            if (f.exists()) {
                adUri = Uri.fromFile(f)
                adName.text = f.name
                JobStore.saveLastInputs(this, null, adUri)
                updateButton()
            }
        }
    }

    // ------------------------------------------------------ job rendering

    override fun onJobUpdate() {
        runOnUiThread { render() }
    }

    private fun render() {
        val s = JobRunner.status
        stage.text = if (JobRunner.stageDetail.isEmpty())
            JobRunner.stageBase
        else "${JobRunner.stageBase}  ${JobRunner.stageDetail}"
        bar.progress = JobRunner.barProgress
        logView.text = JobRunner.logSnapshot()

        if (s != lastRenderedStatus) {
            lastRenderedStatus = s
            val running = s == JobRunner.Status.RUNNING
            goButton.text = if (running) "✖  Cancel" else "▶  Patch it"
            goButton.isEnabled = running ||
                    (videoUri != null && adUri != null)
            if (running)
                window.addFlags(
                    WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            else
                window.clearFlags(
                    WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            // announce terminal states for TalkBack users
            if (s == JobRunner.Status.DONE ||
                s == JobRunner.Status.FAILED)
                stage.announceForAccessibility(stage.text)
        }
    }

    private fun updateButton() {
        if (JobRunner.status != JobRunner.Status.RUNNING)
            goButton.isEnabled = videoUri != null && adUri != null
    }

    // ------------------------------------------------------ start / resume

    private fun startJob(outUri: Uri, a: Double? = null, b: Double? = null) {
        if (android.os.Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(android.Manifest.permission
                .POST_NOTIFICATIONS) !=
            android.content.pm.PackageManager.PERMISSION_GRANTED) {
            askNotifPermission.launch(
                android.Manifest.permission.POST_NOTIFICATIONS)
        }
        lastRenderedStatus = null
        JobRunner.start(this, videoUri!!, adUri!!, outUri, a, b)
    }

    /** Reload the last-used inputs if their permissions still hold. */
    private fun restoreInputs() {
        fun tryRestore(uri: Uri?): Uri? {
            uri ?: return null
            return try {
                contentResolver.openFileDescriptor(uri, "r")?.close()
                uri
            } catch (e: Exception) { null }
        }
        videoUri = tryRestore(JobStore.lastVideo(this))
        adUri = tryRestore(JobStore.lastAd(this))
        videoUri?.let { videoName.text = displayName(it) }
        adUri?.let { adName.text = displayName(it) }
        updateButton()
    }

    private fun offerResumeIfAny() {
        if (JobRunner.status == JobRunner.Status.RUNNING) return
        val job = JobStore.incompleteJob(this) ?: return
        val skipAnalysis = job.a != null && job.b != null
        AlertDialog.Builder(this)
            .setTitle("Unfinished job")
            .setMessage("The last patch of " +
                "\"${displayName(job.video)}\" did not finish." +
                if (skipAnalysis)
                    "\n\nAlignment is already computed - resuming " +
                    "skips straight to writing the output."
                else "\n\nResume it from the start?")
            .setPositiveButton("Resume") { _, _ ->
                videoUri = job.video
                adUri = job.ad
                videoName.text = displayName(job.video)
                adName.text = displayName(job.ad)
                lastRenderedStatus = null
                JobRunner.start(this, job.video, job.ad, job.out,
                    job.a, job.b)
            }
            .setNegativeButton("Discard") { _, _ ->
                JobStore.markComplete(this)
            }
            .show()
    }

    private fun confirmClearCache() {
        AlertDialog.Builder(this)
            .setTitle("Clear cache")
            .setMessage("Deletes saved logs, downloaded AD files, update " +
                "downloads and remembered jobs. Your patched videos are " +
                "not touched.")
            .setPositiveButton("Clear") { _, _ ->
                var freed = 0L
                fun wipe(dir: File?) {
                    dir?.walkBottomUp()?.forEach {
                        if (it.isFile) { freed += it.length(); it.delete() }
                        else if (it != dir) it.delete()
                    }
                }
                wipe(JobRunner.logsDir(this))
                wipe(getExternalFilesDir(
                    android.os.Environment.DIRECTORY_DOWNLOADS))
                wipe(cacheDir)
                JobStore.clearAll(this)
                videoUri = null; adUri = null
                videoName.text = "no video selected"
                adName.text = "no AD file selected"
                updateButton()
                Toast.makeText(this,
                    "Cleared ${freed / 1_048_576} MB",
                    Toast.LENGTH_SHORT).show()
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun displayName(uri: Uri): String {
        if (uri.scheme == "file")
            return uri.lastPathSegment ?: "file"
        contentResolver.query(uri, null, null, null, null)?.use { c ->
            val idx = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
            if (idx >= 0 && c.moveToFirst()) return c.getString(idx)
        }
        return uri.lastPathSegment ?: "file"
    }
}
