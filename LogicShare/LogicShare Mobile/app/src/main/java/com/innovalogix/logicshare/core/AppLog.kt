package com.innovalogix.logicshare.core

import android.content.Context
import android.util.Log
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Logcat plus a file, like LogicShare Windows' logicshare.log.
 *
 * The file is what makes a classroom failure diagnosable afterwards: a
 * teacher cannot attach a debugger, but they can send a log. Rotated at
 * 1 MB with one previous generation kept, so it can never grow without bound
 * on a phone that is never cleaned up.
 */
object AppLog {
    private const val TAG = "LogicShare"
    private const val MAX_BYTES = 1L shl 20

    @Volatile private var file: File? = null
    private val lock = Any()
    private val stamp = SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US)

    fun init(ctx: Context) {
        file = File(ctx.filesDir, "logicshare.log")
    }

    fun d(msg: String) { Log.d(TAG, msg) }
    fun i(msg: String) { Log.i(TAG, msg); write("INFO", msg) }
    fun w(msg: String, t: Throwable? = null) { Log.w(TAG, msg, t); write("WARNING", msg, t) }
    fun e(msg: String, t: Throwable? = null) { Log.e(TAG, msg, t); write("ERROR", msg, t) }

    private fun write(level: String, msg: String, t: Throwable? = null) {
        val f = file ?: return
        try {
            synchronized(lock) {
                if (f.exists() && f.length() > MAX_BYTES) {
                    val old = File(f.parentFile, "logicshare.log.1")
                    old.delete()
                    f.renameTo(old)
                }
                val line = buildString {
                    append(stamp.format(Date())).append("  ")
                    append(level.padEnd(8)).append(' ').append(msg).append('\n')
                    if (t != null) append(Log.getStackTraceString(t)).append('\n')
                }
                f.appendText(line)
            }
        } catch (_: Exception) {
            // A log file must never be the reason anything fails.
        }
    }
}
