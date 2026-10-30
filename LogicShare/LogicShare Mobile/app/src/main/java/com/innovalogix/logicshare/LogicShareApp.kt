package com.innovalogix.logicshare

import android.app.Application
import com.innovalogix.logicshare.ack.AckServer
import com.innovalogix.logicshare.ack.AnnouncementTracker
import com.innovalogix.logicshare.ack.TeacherIdentity
import com.innovalogix.logicshare.core.AppLog
import com.innovalogix.logicshare.core.HistoryStore
import com.innovalogix.logicshare.core.LegacyMigration
import com.innovalogix.logicshare.core.ProfileStore
import com.innovalogix.logicshare.net.NetWatch
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow

/**
 * Process-wide singletons. One tracker, one ack listener, one identity — the
 * UI and the foreground service share them, which is what lets an
 * acknowledgment arriving while the window is closed show up in the report
 * the moment it is opened (and live, if it is already open).
 */
class LogicShareApp : Application() {

    lateinit var profiles: ProfileStore; private set
    lateinit var history: HistoryStore; private set
    lateinit var identity: TeacherIdentity; private set
    lateinit var tracker: AnnouncementTracker; private set
    lateinit var ackServer: AckServer; private set
    lateinit var netWatch: NetWatch; private set

    /** Tray → Quit. The activity finishes itself when this fires. */
    private val _quit = MutableSharedFlow<Unit>(extraBufferCapacity = 1)
    val quitRequested: SharedFlow<Unit> = _quit

    override fun onCreate() {
        super.onCreate()
        instance = this
        AppLog.init(this)
        AppLog.i("=".repeat(60))
        AppLog.i("LogicShare starting up (v${BuildConfig.VERSION_NAME}, build ${BuildConfig.VERSION_CODE})")

        val dir = filesDir
        profiles = ProfileStore(dir)
        history = HistoryStore(dir)
        // Before the identity is created: the Flutter build's device id must
        // be adopted, not replaced — boards are holding acks addressed to it.
        val migrated = LegacyMigration.run(this, dir, profiles, history)
        identity = TeacherIdentity(dir, migrated.deviceId)
        tracker = AnnouncementTracker(dir, identity.deviceId)
        if (migrated.tracking.isNotEmpty()) tracker.importLegacy(migrated.tracking)
        ackServer = AckServer(tracker)
        netWatch = NetWatch(this)
    }

    fun requestQuit() {
        _quit.tryEmit(Unit)
    }

    companion object {
        lateinit var instance: LogicShareApp
            private set
    }
}
