package com.renderedideas.adpatcher

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.IBinder

/**
 * Foreground service held open while a patch job runs (the job itself
 * lives in [JobRunner]). Being foreground exempts the process from
 * background CPU throttling - a wake lock alone is not enough on modern
 * Android - and its notification doubles as a progress display.
 */
class PatchService : Service() {

    override fun onStartCommand(intent: Intent?, flags: Int,
                                startId: Int): Int {
        ensureChannel(this)
        startForeground(NOTIF_ID, buildNotification(this, 0))
        return START_NOT_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    companion object {
        private const val CHANNEL = "patching"
        private const val NOTIF_ID = 1

        private fun ensureChannel(context: Context) {
            val nm = context.getSystemService(
                NotificationManager::class.java)
            nm.createNotificationChannel(NotificationChannel(
                CHANNEL, "Patching progress",
                NotificationManager.IMPORTANCE_LOW))
        }

        private fun buildNotification(context: Context,
                                      percent: Int): Notification {
            val tap = PendingIntent.getActivity(context, 0,
                Intent(context, MainActivity::class.java),
                PendingIntent.FLAG_IMMUTABLE)
            return Notification.Builder(context, CHANNEL)
                .setContentTitle("AD Patcher")
                .setContentText(
                    if (percent > 0) "Patching… $percent%"
                    else "Patching in progress")
                .setSmallIcon(android.R.drawable.stat_sys_download)
                .setProgress(100, percent, percent == 0)
                .setContentIntent(tap)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .build()
        }

        fun start(context: Context) =
            context.startForegroundService(
                Intent(context, PatchService::class.java))

        fun stop(context: Context) =
            context.stopService(Intent(context, PatchService::class.java))

        /** Update the progress notification (safe from any thread). */
        fun updateProgress(context: Context, percent: Int) {
            try {
                ensureChannel(context)
                context.getSystemService(NotificationManager::class.java)
                    .notify(NOTIF_ID, buildNotification(context, percent))
            } catch (_: Exception) { /* notification is best-effort */ }
        }
    }
}
