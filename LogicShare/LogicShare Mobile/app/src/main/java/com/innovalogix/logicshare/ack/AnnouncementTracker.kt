package com.innovalogix.logicshare.ack

import com.innovalogix.logicshare.core.AppLog
import com.innovalogix.logicshare.core.JsonStore
import com.innovalogix.logicshare.core.nowSeconds
import com.innovalogix.logicshare.core.optStr
import com.innovalogix.logicshare.core.randomHex
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * This installation's stable device id — Windows' TeacherIdentity, same
 * device.json. Boards address acknowledgments to it, so regenerating it would
 * orphan every acknowledgment still queued on every board.
 */
class TeacherIdentity(dir: File, migratedId: String? = null) {
    val deviceId: String

    init {
        val f = File(dir, "device.json")
        val existing = (JsonStore.read(f) as? JSONObject)?.optStr("device_id").orEmpty()
        deviceId = when {
            existing.isNotEmpty() -> existing.also { AppLog.i("Teacher device_id: $it") }
            !migratedId.isNullOrEmpty() -> migratedId.also {
                JsonStore.write(f, JSONObject().put("device_id", it).put("created_at", nowSeconds()))
                AppLog.i("Kept teacher device_id from the previous app: $it")
            }
            else -> ("td_" + randomHex(8)).also {
                JsonStore.write(f, JSONObject().put("device_id", it).put("created_at", nowSeconds()))
                AppLog.i("Generated stable teacher device_id: $it")
            }
        }
    }
}

/**
 * Per-board announcement state, timeouts and persistence. A port of Windows'
 * AnnouncementTracker; every rule below carries the same reasoning and the
 * Python docstrings are the long form of it.
 *
 * Thread-safety: acknowledgments arrive on AckServer worker threads while the
 * UI reads the same structures. All mutation happens under one lock, and the
 * UI is told through flows. Readers get COPIES, so a recomposition can never
 * observe a row half-way through being updated.
 */
class AnnouncementTracker(dir: File, val deviceId: String) {
    private val file = File(dir, "announcement_tracking.json")
    private val lock = Any()
    private val items = LinkedHashMap<String, Announcement>()

    private val _statusChanged = MutableSharedFlow<String>(extraBufferCapacity = 64)
    /** announcement_id — something about this announcement changed. */
    val statusChanged: SharedFlow<String> = _statusChanged

    private val _timeoutReached =
        MutableSharedFlow<Pair<String, List<BoardStatus>>>(extraBufferCapacity = 16)
    /** Five minutes elapsed with boards outstanding — tell the teacher. */
    val timeoutReached: SharedFlow<Pair<String, List<BoardStatus>>> = _timeoutReached

    init { load() }

    // ---- persistence ----
    private fun load() {
        val raw = JsonStore.read(file) as? JSONArray ?: return
        for (i in 0 until raw.length()) {
            val o = raw.optJSONObject(i) ?: continue
            val ann = runCatching { Announcement.fromJson(o) }.getOrNull() ?: continue
            if (ann.announcementId.isNotEmpty()) items[ann.announcementId] = ann
        }
        if (items.isNotEmpty()) AppLog.i("Restored ${items.size} tracked announcement(s)")
    }

    /** Newest first and bounded at 200 — a working record, not an archive. */
    private fun saveLocked() {
        val sorted = items.values.sortedByDescending { it.sentAt }.take(200)
        items.clear()
        sorted.forEach { items[it.announcementId] = it }
        JsonStore.write(file, JSONArray().apply { sorted.forEach { put(it.toJson()) } })
    }

    /** One-time import of the Flutter build's tracking list. */
    fun importLegacy(list: List<Announcement>) {
        synchronized(lock) {
            for (a in list) if (a.announcementId.isNotEmpty() && a.announcementId !in items)
                items[a.announcementId] = a
            saveLocked()
        }
    }

    // ---- queries ----
    fun get(announcementId: String): Announcement? =
        synchronized(lock) { items[announcementId]?.copy() }

    fun all(): List<Announcement> =
        synchronized(lock) { items.values.sortedByDescending { it.sentAt }.map { it.copy() } }

    fun hasOpenAnnouncements(): Boolean =
        synchronized(lock) { items.values.any { it.isOpen() } }

    // ---- mutations ----
    /** Called BEFORE delivery, so a send that dies half way still leaves a
     *  report of which boards were meant to receive it. */
    fun recordSent(announcementId: String, title: String, message: String,
                   targets: List<Map<String, String>>): Announcement {
        val ann: Announcement
        synchronized(lock) {
            val boards = LinkedHashMap<String, BoardStatus>()
            for (t in targets) {
                val st = BoardStatus(
                    boardDeviceId = t["board_device_id"].orEmpty(),
                    ip = t["ip"].orEmpty(),
                    boardName = t["device"] ?: t["board_name"].orEmpty(),
                    room = t["room"].orEmpty(),
                    classStandard = t["class"] ?: t["class_standard"].orEmpty(),
                    section = t["section"].orEmpty(),
                    state = BoardState.PENDING,
                )
                boards[st.key] = st
            }
            ann = Announcement(announcementId, title, message, nowSeconds(), boards)
            items[announcementId] = ann
            saveLocked()
        }
        AppLog.i("Announcement sent: id=${announcementId.take(8)} " +
                "title=${title.take(40)} boards=${targets.size}")
        _statusChanged.tryEmit(announcementId)
        return ann.copy()
    }

    /** Result of the /announce POST to one board. ``boardInfo`` is the board's
     *  reply — where its stable device id comes from. */
    fun recordDelivery(announcementId: String, ip: String, ok: Boolean,
                       detail: String = "", boardInfo: JSONObject? = null) {
        val info = boardInfo ?: JSONObject()
        synchronized(lock) {
            val ann = items[announcementId] ?: return
            val st = ann.boards.values.firstOrNull { it.ip == ip } ?: return

            val newId = info.optStr("board_device_id")
            if (newId.isNotEmpty() && newId != st.boardDeviceId) {
                // Re-key: stored under "ip:..." and now has a real identity.
                ann.boards.remove(st.key)
                st.boardDeviceId = newId
                ann.boards[st.key] = st
            }
            info.optStr("board_name").takeIf { it.isNotEmpty() }?.let { st.boardName = it }
            info.optStr("room").takeIf { it.isNotEmpty() }?.let { st.room = it }
            info.optStr("class").takeIf { it.isNotEmpty() }?.let { st.classStandard = it }
            info.optStr("section").takeIf { it.isNotEmpty() }?.let { st.section = it }

            if (ok) {
                // Never downgrade a board that has ALREADY been read.
                if (st.state != BoardState.READ) {
                    st.state = BoardState.DELIVERED
                    st.deliveredAt = nowSeconds()
                }
                st.detail = detail.ifEmpty { "Delivered" }
            } else {
                if (st.state != BoardState.READ) st.state = BoardState.OFFLINE
                st.detail = detail.ifEmpty { "Unreachable" }
            }
            saveLocked()
        }
        AppLog.i("Announcement ${announcementId.take(8)} -> $ip: " +
                "${if (ok) "delivered" else "FAILED"} ($detail)")
        _statusChanged.tryEmit(announcementId)
    }

    /** An acknowledgment POSTed by a board. True if it moved something.
     *  Either way the server answers 200 — see AckServer. */
    fun recordAck(payload: JSONObject): Boolean {
        val announcementId = payload.optStr("announcement_id").trim()
        val boardDeviceId = payload.optStr("board_device_id").trim()
        val teacherDeviceId = payload.optStr("teacher_device_id").trim()
        if (announcementId.isEmpty() || boardDeviceId.isEmpty()) {
            AppLog.w("Acknowledgment missing ids; ignoring")
            return false
        }
        // Addressed to a different teacher device: a board with a stale
        // registry entry. Accepting it would put someone else's data here.
        if (teacherDeviceId.isNotEmpty() && teacherDeviceId != deviceId) {
            AppLog.w("Acknowledgment for teacher ${teacherDeviceId.take(8)} " +
                    "arrived at ${deviceId.take(8)}; ignoring")
            return false
        }

        val remaining: Int
        synchronized(lock) {
            val ann = items[announcementId]
            if (ann == null) {
                AppLog.i("Acknowledgment for unknown announcement " +
                        "${announcementId.take(8)} (cleared history?) — ignoring")
                return false
            }
            var st = ann.boards[boardDeviceId]
            if (st == null) {
                // Adopt an IP-keyed row — but only when the IP MATCHES the
                // address the acknowledgment actually came from (stamped by
                // AckServer). Adopting "the first row missing an id" credited
                // one classroom's acknowledgment to another.
                val ip = payload.optStr("ip")
                if (ip.isNotEmpty()) {
                    val b = ann.boards.values.firstOrNull { it.boardDeviceId.isEmpty() && it.ip == ip }
                    if (b != null) {
                        ann.boards.remove(b.key)
                        b.boardDeviceId = boardDeviceId
                        ann.boards[b.key] = b
                        st = b
                    }
                }
            }
            if (st == null) {
                // Real, so recorded rather than dropped.
                st = BoardStatus(boardDeviceId = boardDeviceId)
                ann.boards[st.key] = st
                AppLog.w("Acknowledgment from board ${boardDeviceId.take(8)} " +
                        "not in the original target list; added")
            }
            if (st.state == BoardState.READ) {
                AppLog.i("Duplicate acknowledgment ignored: announcement=" +
                        "${announcementId.take(8)} board=${boardDeviceId.take(8)}")
                return false
            }
            st.state = BoardState.READ
            // Coerced defensively: a malformed value off the network must not
            // be able to throw here, or the board retries for eight hours.
            st.readAt = payload.opt("acknowledged_at")?.toString()?.toDoubleOrNull()
                ?: nowSeconds()
            st.detail = "Read"
            for ((src, set) in listOf<Pair<String, (String) -> Unit>>(
                "board_name" to { v -> st.boardName = v },
                "room" to { v -> st.room = v },
                "class" to { v -> st.classStandard = v },
                "section" to { v -> st.section = v },
                "board_version" to { v -> st.boardVersion = v },
            )) {
                // Scalars only — a nested object would render as "{...}".
                when (val v = payload.opt(src)) {
                    is String, is Number -> v.toString().takeIf { it.isNotEmpty() }
                        ?.let { set(it.take(80)) }
                }
            }
            saveLocked()
            remaining = ann.boards.values.count { it.state != BoardState.READ }
        }
        AppLog.i("Acknowledgment received: announcement=${announcementId.take(8)} " +
                "board=${boardDeviceId.take(8)} ($remaining board(s) still unacknowledged)")
        _statusChanged.tryEmit(announcementId)
        return true
    }

    // ---- timeouts ----
    /** Move elapsed announcements to TIMEOUT / EXPIRED and notify once. */
    fun sweep() {
        val now = nowSeconds()
        val toNotify = mutableListOf<Pair<String, List<BoardStatus>>>()
        val changed = linkedSetOf<String>()
        synchronized(lock) {
            for (ann in items.values) {
                val age = now - ann.sentAt
                if (age < ACK_TIMEOUT_SECONDS) continue
                var dirty = false
                for (b in ann.boards.values) {
                    if (b.state == BoardState.DELIVERED || b.state == BoardState.PENDING) {
                        b.state = BoardState.TIMEOUT
                        b.timeoutAt = now
                        b.detail = "No acknowledgment yet"
                        dirty = true
                    }
                    if (age >= ACK_GIVE_UP_SECONDS &&
                        (b.state == BoardState.TIMEOUT || b.state == BoardState.OFFLINE)) {
                        b.state = BoardState.EXPIRED
                        b.expiredAt = now
                        b.detail = "Expired — no acknowledgment"
                        dirty = true
                    }
                }
                // Notify once per announcement, and only if something is
                // actually outstanding.
                if (ann.timeoutNotifiedAt == null) {
                    val outstanding = ann.boards.values.filter { it.state != BoardState.READ }
                    ann.timeoutNotifiedAt = now
                    if (outstanding.isNotEmpty())
                        toNotify += ann.announcementId to outstanding.map { it.copy() }
                    dirty = true
                }
                if (dirty) changed += ann.announcementId
            }
            if (changed.isNotEmpty()) saveLocked()
        }
        changed.forEach { _statusChanged.tryEmit(it) }
        for ((aid, boards) in toNotify) {
            AppLog.w("Acknowledgment TIMEOUT (${ACK_TIMEOUT_SECONDS / 60} min): " +
                    "announcement=${aid.take(8)} ${boards.size} board(s) have not acknowledged")
            _timeoutReached.tryEmit(aid to boards)
        }
    }

    // ---- maintenance ----
    fun forget(announcementId: String) {
        synchronized(lock) { items.remove(announcementId); saveLocked() }
    }

    fun clear() {
        synchronized(lock) { items.clear(); saveLocked() }
    }
}
