package com.innovalogix.logicshare.core

import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.security.SecureRandom

/** One target an announcement went to — Windows' {class, section, room, ip}. */
data class Recipient(
    val cls: String,
    val section: String,
    val room: String,
    val ip: String,
) {
    fun toJson(): JSONObject = JSONObject()
        .put("class", cls).put("section", section).put("room", room).put("ip", ip)

    companion object {
        fun fromJson(o: JSONObject) = Recipient(
            o.optStr("class"), o.optStr("section"), o.optStr("room"), o.optStr("ip"))
    }
}

/** A row in announcements.json — identical keys to Windows. */
data class HistoryEntry(
    val id: String,
    val announcementId: String,
    val title: String,
    val message: String,
    val recipients: List<Recipient>,
    val sentAt: Double,
) {
    fun toJson(): JSONObject = JSONObject()
        .put("id", id)
        .put("announcement_id", announcementId)
        .put("title", title)
        .put("message", message)
        .put("recipients", JSONArray().apply { recipients.forEach { put(it.toJson()) } })
        .put("sent_at", sentAt)

    companion object {
        fun fromJson(o: JSONObject): HistoryEntry {
            val r = o.optJSONArray("recipients") ?: JSONArray()
            val recips = (0 until r.length()).mapNotNull { i ->
                when (val v = r.opt(i)) {
                    is JSONObject -> Recipient.fromJson(v)
                    // The Flutter build stored recipients as class labels.
                    is String -> Recipient(v, "", "", "")
                    else -> null
                }
            }
            return HistoryEntry(
                id = o.optStr("id").ifBlank { randomHex(6) },
                announcementId = o.optStr("announcement_id"),
                title = o.optStr("title"),
                message = o.optStr("message").ifBlank { o.optStr("detail") },
                recipients = recips,
                sentAt = toSeconds(o.optDouble("sent_at",
                    o.optDouble("sentAt", 0.0))),
            )
        }
    }
}

private val rng = SecureRandom()

fun randomHex(bytes: Int): String {
    val b = ByteArray(bytes)
    rng.nextBytes(b)
    return b.joinToString("") { "%02x".format(it) }
}

/** Windows' new_announcement_id: "an_" + 8 random bytes in hex. Generated on
 *  the teacher device so one announcement carries one identity across every
 *  board, every acknowledgment and the history row. */
fun newAnnouncementId(): String = "an_" + randomHex(8)

/** announcements.json, newest first, capped at 50 — Windows' HISTORY_LIMIT. */
class HistoryStore(private val dir: File) {
    private val file get() = File(dir, "announcements.json")
    private val lock = Any()

    fun load(): List<HistoryEntry> = synchronized(lock) {
        val a = JsonStore.read(file) as? JSONArray ?: return emptyList()
        (0 until a.length()).mapNotNull { i ->
            (a.opt(i) as? JSONObject)?.let { runCatching { HistoryEntry.fromJson(it) }.getOrNull() }
        }
    }

    private fun save(entries: List<HistoryEntry>) {
        val a = JSONArray()
        entries.take(LIMIT).forEach { a.put(it.toJson()) }
        if (!JsonStore.write(file, a)) AppLog.e("Failed to persist announcement history")
    }

    fun add(title: String, message: String, recipients: List<Recipient>,
            announcementId: String): List<HistoryEntry> = synchronized(lock) {
        val entries = load().toMutableList()
        entries.add(0, HistoryEntry(randomHex(6), announcementId, title, message,
            recipients, nowSeconds()))
        save(entries)
        entries
    }

    fun delete(id: String) = synchronized(lock) { save(load().filter { it.id != id }) }

    fun clear() = synchronized(lock) { save(emptyList()) }

    /** Used by the one-time Flutter migration. */
    fun replaceAll(entries: List<HistoryEntry>) = synchronized(lock) { save(entries) }

    companion object { const val LIMIT = 50 }
}
