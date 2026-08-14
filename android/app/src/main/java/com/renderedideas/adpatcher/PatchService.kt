package com.renderedideas.adpatcher

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.IBinder

/**
 * Minimal foreground service held open while a patch job runs.
 *
 * The heavy work happens on threads in MainActivity; this service exists
 * so the whole process gets foreground scheduling. A wake lock alone is
 * not enough on modern Android - once the user switches apps, a
 * background process is moved to a restricted cgroup (little cores,
 * capped CPU quota) and a two-hour movie takes several times longer.
 */
class PatchService : Service() {

    override fun onStartCommand(intent: Intent?, flags: Int,
                                startId: Int): Int {
        val channelId = "patching"
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(
            channelId, "Patching progress",
            NotificationManager.IMPORTANCE_LOW))
        val notification = Notification.Builder(this, channelId)
            .setContentTitle("AD Patcher")
            .setContentText("Patching in progress - keep me running")
            .setSmallIcon(android.R.drawable.stat_sys_download)
            .setOngoing(true)
            .build()
        startForeground(1, notification)
        return START_NOT_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    companion object {
        fun start(context: Context) =
            context.startForegroundService(
                Intent(context, PatchService::class.java))

        fun stop(context: Context) =
            context.stopService(Intent(context, PatchService::class.java))
    }
}
