package com.innovalogix.logicshare.core

import android.content.Context
import com.innovalogix.logicshare.ack.Announcement
import org.json.JSONArray
import org.json.JSONObject
import org.json.JSONTokener
import java.io.File

/**
 * One-time import of what the Flutter build of LogicShare stored.
 *
 * This app installs OVER the Flutter one (same package, same signing key), so
 * a teacher updating keeps the app's private storage — and the Flutter build
 * kept everything in SharedPreferences, under the keys below. Without this
 * they would be greeted by the first-run setup as if they had never used
 * LogicShare, and, worse, get a NEW teacher device id: every acknowledgment a
 * board was still holding for the old id would be addressed to a device that
 * no longer exists.
 *
 * Keys, from the Flutter source: profile, history, ack_teacher_device_id,
 * ack_tracking. The legacy shared_preferences plugin stores them in
 * "FlutterSharedPreferences" with a "flutter." prefix.
 *
 * Runs once; a marker file records that it has. Nothing is deleted from the
 * old store, so a failed import can never lose the original.
 */
object LegacyMigration {
    private const val PREFS = "FlutterSharedPreferences"
    private const val P = "flutter."
    /** shared_preferences' marker for a List<String> value. */
    private const val LIST_PREFIX = "VGhpcyBpcyB0aGUgcHJlZml4IGZvciBhIGxpc3Qu"

    data class Result(val deviceId: String?, val tracking: List<Announcement>)

    fun run(ctx: Context, dir: File, profiles: ProfileStore, history: HistoryStore): Result {
        val marker = File(dir, ".migrated_from_flutter")
        if (marker.exists()) return Result(null, emptyList())
        val sp = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val all = sp.all
        if (all.isEmpty()) {
            marker.writeText("nothing to migrate")
            return Result(null, emptyList())
        }
        AppLog.i("Importing data from the previous LogicShare (Flutter) build")

        // Profile — only if this app has none yet.
        if (profiles.load() == null) {
            (parse(all[P + "profile"]) as? JSONObject)?.let(Profile::fromJson)?.let {
                profiles.save(it)
                AppLog.i("Imported profile for ${it.initials}")
            }
        }

        // History — announcements only. The Flutter build also logged file
        // sends there; Windows' history is announcements, and so is this one.
        if (history.load().isEmpty()) {
            val entries = jsonObjects(all[P + "history"])
                .filter { it.optStr("kind").let { k -> k.isEmpty() || k == "announcement" } }
                .mapNotNull { runCatching { HistoryEntry.fromJson(it) }.getOrNull() }
                .filter { it.title.isNotEmpty() }
            if (entries.isNotEmpty()) {
                history.replaceAll(entries)
                AppLog.i("Imported ${entries.size} history entr${if (entries.size == 1) "y" else "ies"}")
            }
        }

        val deviceId = (all[P + "ack_teacher_device_id"] as? String)?.takeIf { it.startsWith("td_") }
        val tracking = jsonObjects(all[P + "ack_tracking"])
            .mapNotNull { runCatching { Announcement.fromJson(it) }.getOrNull() }
            .filter { it.announcementId.isNotEmpty() }

        marker.writeText("migrated")
        return Result(deviceId, tracking)
    }

    private fun parse(v: Any?): Any? {
        val s = v as? String ?: return null
        return try { JSONTokener(s).nextValue() } catch (_: Exception) { null }
    }

    /** A JSON array stored as one string, or a List<String> of JSON objects
     *  in either of the plugin's encodings. */
    private fun jsonObjects(v: Any?): List<JSONObject> {
        val s = v as? String ?: return emptyList()
        val arr: JSONArray? = when {
            s.startsWith("$LIST_PREFIX!") -> runCatching { JSONArray(s.removePrefix("$LIST_PREFIX!")) }.getOrNull()
            s.startsWith(LIST_PREFIX) -> null      // Java-serialised form: not readable safely
            else -> parse(s) as? JSONArray
        }
        arr ?: return emptyList()
        return (0 until arr.length()).mapNotNull { i ->
            when (val e = arr.opt(i)) {
                is JSONObject -> e
                is String -> parse(e) as? JSONObject
                else -> null
            }
        }
    }
}
