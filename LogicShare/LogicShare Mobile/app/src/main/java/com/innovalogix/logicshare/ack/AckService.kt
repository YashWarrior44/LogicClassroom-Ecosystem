package com.innovalogix.logicshare.ack

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.wifi.WifiManager
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import com.innovalogix.logicshare.LogicShareApp
import com.innovalogix.logicshare.MainActivity
import com.innovalogix.logicshare.R
import com.innovalogix.logicshare.core.AppLog
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/**
 * The Android equivalent of LogicShare Windows' tray presence.
 *
 * On Windows, closing the window hides it into the tray and everything
 * underneath keeps running: the acknowledgment listener and the five-minute
 * timeout sweep. A teacher sends an announcement, closes the app and walks to
 * the next class — the warning they are owed still arrives.
 *
 * Android's only way to keep a listening socket alive after the user leaves
 * an app is a foreground service, and the OS's price for one is a persistent
 * notification. That notification IS the tray icon here: it carries the same
 * two entries as the Windows tray menu, "Open LogicShare" and
 * "Quit LogicShare", on a silent minimum-importance channel.
 *
 * Service type is connectedDevice, not the dataSync the Flutter build used:
 * from Android 15 a dataSync service is stopped after six hours a day, which
 * would silently end the listener mid-afternoon. connectedDevice — talking to
 * devices on the local network — is what this does, and it carries no such
 * limit. Its prerequisite permission, CHANGE_WIFI_MULTICAST_STATE, is one
 * discovery needs anyway.
 *
 * Battery: the Wi-Fi and wake locks that keep a dozing phone reachable are
 * held ONLY while some announcement can still be acknowledged. Once every
 * board has replied or the eight-hour window has passed, nothing can arrive,
 * and the locks go.
 */
class AckService : Service() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private var sweepJob: Job? = null
    private var wifiLock: WifiManager.WifiLock? = null
    private var wakeLock: PowerManager.WakeLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_QUIT) {
            AppLog.i("Quit requested from the notification")
            shutdown()
            (application as LogicShareApp).requestQuit()
            stopSelf()
            return START_NOT_STICKY
        }
        startInForeground()
        if (sweepJob == null) begin()
        // START_STICKY: a listener that quietly stops is indistinguishable,
        // from the teacher's side, from a classroom that never replied.
        return START_STICKY
    }

    private fun startInForeground() {
        ensureChannels(this)
        val n = listenerNotification()
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                startForeground(LISTENER_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE)
            } else {
                startForeground(LISTENER_ID, n)
            }
        } catch (e: Exception) {
            // Android 12+ can refuse a foreground start from the background.
            // The listener still runs while the process lives; it comes back
            // properly the next time the app is opened.
            AppLog.w("Could not enter the foreground: ${e.message}")
        }
    }

    private fun begin() {
        val app = application as LogicShareApp
        app.ackServer.start()
        sweepJob = scope.launch {
            // UNDISPATCHED: subscribe NOW, before the sweep below runs.
            // timeoutReached has no replay, and an ordinary launch is only
            // queued — the startup sweep would emit the warnings for
            // everything that timed out while the phone was off into a flow
            // nobody was listening to yet, and they would simply be lost.
            launch(start = kotlinx.coroutines.CoroutineStart.UNDISPATCHED) {
                app.tracker.timeoutReached.collect { (aid, boards) ->
                    val ann = app.tracker.get(aid)
                    notifyTimeout(this@AckService, aid, ann?.title ?: "Announcement", boards)
                }
            }
            // Catch anything that timed out while the app was closed.
            app.tracker.sweep()
            AppLog.i("Announcement timeout sweep started (every ${SWEEP_INTERVAL_SECONDS}s, " +
                    "timeout ${ACK_TIMEOUT_SECONDS / 60} min)")
            while (isActive) {
                updateLocks(app.tracker.hasOpenAnnouncements())
                delay(SWEEP_INTERVAL_SECONDS * 1000L)
                app.tracker.sweep()
            }
        }
    }

    private fun updateLocks(needed: Boolean) {
        if (needed && wakeLock == null) {
            try {
                val wifi = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
                @Suppress("DEPRECATION")
                val mode = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q)
                    WifiManager.WIFI_MODE_FULL_LOW_LATENCY else WifiManager.WIFI_MODE_FULL_HIGH_PERF
                wifiLock = wifi.createWifiLock(mode, "LogicShare:ack").apply {
                    setReferenceCounted(false); acquire()
                }
            } catch (e: Exception) {
                AppLog.w("Wi-Fi lock unavailable: ${e.message}")
            }
            try {
                val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
                wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "LogicShare:ack").apply {
                    setReferenceCounted(false); acquire()
                }
            } catch (e: Exception) {
                AppLog.w("Wake lock unavailable: ${e.message}")
            }
        } else if (!needed && wakeLock != null) {
            releaseLocks()
        }
    }

    private fun releaseLocks() {
        runCatching { wifiLock?.release() }
        runCatching { wakeLock?.release() }
        wifiLock = null
        wakeLock = null
    }

    private fun shutdown() {
        sweepJob?.cancel()
        sweepJob = null
        releaseLocks()
        (application as LogicShareApp).ackServer.stop()
    }

    override fun onDestroy() {
        shutdown()
        scope.cancel()
        super.onDestroy()
    }

    private fun listenerNotification(): Notification {
        val open = PendingIntent.getActivity(this, 1,
            Intent(this, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val quit = PendingIntent.getService(this, 2,
            Intent(this, AckService::class.java).setAction(ACTION_QUIT),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        return NotificationCompat.Builder(this, CHANNEL_LISTENER)
            .setSmallIcon(R.drawable.ic_stat_logicshare)
            .setContentTitle("LogicShare")
            // Windows' tray tooltip, word for word.
            .setContentText("LogicShare — watching for replies")
            .setOngoing(true)
            .setSilent(true)
            .setPriority(NotificationCompat.PRIORITY_MIN)
            .setContentIntent(open)
            .addAction(0, "Open LogicShare", open)
            .addAction(0, "Quit LogicShare", quit)
            .build()
    }

    companion object {
        const val CHANNEL_LISTENER = "logicshare_ack_listener"
        const val CHANNEL_ALERTS = "logicshare_ack_alerts"
        const val LISTENER_ID = 4711
        const val ACTION_QUIT = "com.innovalogix.logicshare.QUIT"
        const val EXTRA_REPORT = "open_report"

        fun start(ctx: Context) {
            try {
                ContextCompat.startForegroundService(ctx, Intent(ctx, AckService::class.java))
            } catch (e: Exception) {
                AppLog.w("Could not start the acknowledgment service: ${e.message}")
            }
        }

        fun ensureChannels(ctx: Context) {
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
            val nm = ctx.getSystemService(NotificationManager::class.java)
            nm.createNotificationChannel(NotificationChannel(CHANNEL_LISTENER, "Board replies",
                NotificationManager.IMPORTANCE_MIN).apply {
                description = "Keeps LogicShare listening for replies from boards while it is closed."
                setShowBadge(false); enableVibration(false); setSound(null, null)
            })
            nm.createNotificationChannel(NotificationChannel(CHANNEL_ALERTS, "No-reply warnings",
                NotificationManager.IMPORTANCE_HIGH).apply {
                description = "Tells you when boards have not acknowledged an announcement after five minutes."
            })
        }

        /** Windows' AckNotifier.notify_timeout: the same title, the same
         *  body, at most four names, and clicking it opens the report for
         *  THIS announcement — never "the latest". */
        fun notifyTimeout(ctx: Context, announcementId: String, title: String,
                          boards: List<BoardStatus>) {
            val names = boards.map { it.label }
            val shown = names.take(4)
            val more = names.size - shown.size
            var body = "No acknowledgment from:\n• " + shown.joinToString("\n• ")
            if (more > 0) body += "\n…and $more more"
            AppLog.w("Notifying teacher: ${names.size} board(s) have not acknowledged ${title.take(40)}")
            ensureChannels(ctx)
            val open = PendingIntent.getActivity(ctx, announcementId.hashCode(),
                Intent(ctx, MainActivity::class.java)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
                    .putExtra(EXTRA_REPORT, announcementId),
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
            val n = NotificationCompat.Builder(ctx, CHANNEL_ALERTS)
                .setSmallIcon(R.drawable.ic_stat_logicshare)
                .setContentTitle("No reply yet — ${title.take(48)}")
                .setContentText(body.lineSequence().drop(1).joinToString("  "))
                .setStyle(NotificationCompat.BigTextStyle().bigText(body))
                .setPriority(NotificationCompat.PRIORITY_HIGH)
                .setAutoCancel(true)
                .setContentIntent(open)
                .build()
            try {
                NotificationManagerCompat.from(ctx).notify(announcementId.hashCode(), n)
            } catch (e: SecurityException) {
                AppLog.w("Notification permission not granted — timeout warning logged only")
            }
        }
    }
}
