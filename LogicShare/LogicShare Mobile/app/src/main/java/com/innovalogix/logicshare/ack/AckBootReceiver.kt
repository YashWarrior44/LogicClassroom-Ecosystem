package com.innovalogix.logicshare.ack

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import com.innovalogix.logicshare.LogicShareApp
import com.innovalogix.logicshare.core.AppLog
import com.innovalogix.logicshare.update.Updater

/**
 * Brings the acknowledgment listener back after a reboot or an app update —
 * but only when something can still be acknowledged. A phone restarted
 * between an announcement going out and a board replying would otherwise
 * never hear the reply, and the teacher would be told that classroom stayed
 * silent. With nothing open there is nothing to listen for, and no reason to
 * put a notification in the shade.
 */
class AckBootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val action = intent.action ?: return
        if (action != Intent.ACTION_BOOT_COMPLETED &&
            action != Intent.ACTION_MY_PACKAGE_REPLACED &&
            action != "android.intent.action.QUICKBOOT_POWERON") return
        if (action == Intent.ACTION_MY_PACKAGE_REPLACED) {
            // The update just installed. Its APK is spent NOW — not "the next
            // time the teacher happens to open the main screen", which is the
            // only place the sweep used to run, and Android does not reopen
            // the app after an update. Off the main thread; a receiver must
            // return quickly.
            val pending = goAsync()
            Thread {
                try { Updater.pruneSpent(context.applicationContext) }
                catch (e: Exception) { AppLog.w("Post-update sweep failed: ${e.message}") }
                finally { pending.finish() }
            }.start()
        }
        val app = context.applicationContext as LogicShareApp
        if (!app.tracker.hasOpenAnnouncements()) {
            AppLog.i("Boot: nothing awaiting acknowledgment — listener not started")
            return
        }
        AppLog.i("Boot: announcements still open — restarting the acknowledgment listener")
        AckService.start(context)
    }
}
