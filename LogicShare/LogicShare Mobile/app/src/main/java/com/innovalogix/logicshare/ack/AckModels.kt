package com.innovalogix.logicshare.ack

import com.innovalogix.logicshare.core.nowSeconds
import com.innovalogix.logicshare.core.optNullableDouble
import com.innovalogix.logicshare.core.optStr
import com.innovalogix.logicshare.core.putNullable
import com.innovalogix.logicshare.core.toSeconds
import org.json.JSONObject

// ---------------------------------------------------------------------------
// Protocol — must match LogicBoard/ui/logicshare/ack.py and LogicShare
// Windows' logicshare_ack.py. Port of the latter, line for line where it
// carries a decision.
// ---------------------------------------------------------------------------
const val ACK_PROTOCOL_VERSION = 1
const val TEACHER_ACK_PORT = 16385

/** Five minutes: long enough that a class mid-activity is not reported as
 *  unresponsive, short enough that the teacher still has the lesson. */
const val ACK_TIMEOUT_SECONDS = 5 * 60

/** A sweep rather than one timer per board. */
const val SWEEP_INTERVAL_SECONDS = 15

/** After this no board will ever send an acknowledgment (LogicBoard's TTL). */
const val ACK_GIVE_UP_SECONDS = 8 * 60 * 60

/** Per-board lifecycle within ONE announcement. Wire values are the
 *  lowercase names, exactly as Windows writes them. */
object BoardState {
    const val PENDING = "pending"        // send attempted, no result yet
    const val DELIVERED = "delivered"    // board accepted it; nobody pressed OK
    const val READ = "read"              // a human pressed OK — the only success
    const val OFFLINE = "offline"        // could not be reached at send time
    const val TIMEOUT = "timeout"        // delivered, 5 minutes, no ack
    const val EXPIRED = "expired"        // past the point any ack could arrive

    /** Worst news first — what a teacher opening the report needs to see. */
    val SEVERITY = mapOf(OFFLINE to 0, TIMEOUT to 1, EXPIRED to 2, PENDING to 3,
        DELIVERED to 4, READ to 5)

    /** States that could still become READ. */
    val OPEN = setOf(PENDING, DELIVERED, TIMEOUT)

    /** The Flutter build stored states as its enum names. */
    fun fromWire(s: String): String = when (s.lowercase()) {
        PENDING, DELIVERED, READ, OFFLINE, TIMEOUT, EXPIRED -> s.lowercase()
        "sending" -> PENDING
        "timedout", "timed_out" -> TIMEOUT
        else -> PENDING
    }
}

/** One board's state within one announcement. Mutable on purpose — the
 *  tracker updates rows in place under its lock, exactly as Windows does. */
class BoardStatus(
    var boardDeviceId: String = "",
    var ip: String = "",
    var boardName: String = "",
    var room: String = "",
    var classStandard: String = "",
    var section: String = "",
    var state: String = BoardState.PENDING,
    var detail: String = "",
    var deliveredAt: Double? = null,
    var readAt: Double? = null,
    var timeoutAt: Double? = null,
    var expiredAt: Double? = null,
    var boardVersion: String = "",
) {
    /** Device id once known; the IP is only a transport address. */
    val key: String get() = boardDeviceId.ifEmpty { "ip:$ip" }

    /** Human name for the report, best available. */
    val label: String
        get() {
            val bits = mutableListOf<String>()
            if (classStandard.isNotEmpty()) bits += "$classStandard$section"
            if (room.isNotEmpty()) bits += "Room $room"
            if (bits.isNotEmpty()) return bits.joinToString("  ·  ")
            return boardName.ifEmpty { ip.ifEmpty { "Unknown board" } }
        }

    fun copy(): BoardStatus = BoardStatus(boardDeviceId, ip, boardName, room,
        classStandard, section, state, detail, deliveredAt, readAt, timeoutAt,
        expiredAt, boardVersion)

    /** Same keys as Python's asdict(BoardStatus). */
    fun toJson(): JSONObject = JSONObject()
        .put("board_device_id", boardDeviceId)
        .put("ip", ip)
        .put("board_name", boardName)
        .put("room", room)
        .put("class_standard", classStandard)
        .put("section", section)
        .put("state", state)
        .put("detail", detail)
        .putNullable("delivered_at", deliveredAt)
        .putNullable("read_at", readAt)
        .putNullable("timeout_at", timeoutAt)
        .putNullable("expired_at", expiredAt)
        .put("board_version", boardVersion)

    companion object {
        fun fromJson(o: JSONObject) = BoardStatus(
            boardDeviceId = o.optStr("board_device_id"),
            ip = o.optStr("ip"),
            boardName = o.optStr("board_name"),
            room = o.optStr("room"),
            // "class" is what the Flutter build wrote.
            classStandard = o.optStr("class_standard").ifEmpty { o.optStr("class") },
            section = o.optStr("section"),
            state = BoardState.fromWire(o.optStr("state")),
            detail = o.optStr("detail"),
            deliveredAt = o.optNullableDouble("delivered_at")?.let(::toSeconds),
            readAt = o.optNullableDouble("read_at")?.let(::toSeconds),
            timeoutAt = o.optNullableDouble("timeout_at")?.let(::toSeconds),
            expiredAt = o.optNullableDouble("expired_at")?.let(::toSeconds),
            boardVersion = o.optStr("board_version"),
        )
    }
}

/** One announcement and every board it was sent to. */
class Announcement(
    val announcementId: String,
    val title: String,
    val message: String,
    val sentAt: Double,
    val boards: LinkedHashMap<String, BoardStatus> = LinkedHashMap(),
    var timeoutNotifiedAt: Double? = null,
) {
    fun counts(): Map<String, Int> =
        boards.values.groupingBy { it.state }.eachCount()

    /** Boards not yet read, worst news first. */
    fun unacknowledged(): List<BoardStatus> =
        boards.values.filter { it.state != BoardState.READ }
            .sortedWith(compareBy({ BoardState.SEVERITY[it.state] ?: 9 }, { it.label }))

    fun isOpen(): Boolean {
        if (nowSeconds() - sentAt > ACK_GIVE_UP_SECONDS) return false
        return boards.values.any { it.state in BoardState.OPEN }
    }

    fun allRead(): Boolean = boards.isNotEmpty() && boards.values.all { it.state == BoardState.READ }

    fun copy(): Announcement = Announcement(announcementId, title, message, sentAt,
        LinkedHashMap(boards.mapValues { it.value.copy() }), timeoutNotifiedAt)

    fun toJson(): JSONObject = JSONObject()
        .put("announcement_id", announcementId)
        .put("title", title)
        .put("message", message)
        .put("sent_at", sentAt)
        .putNullable("timeout_notified_at", timeoutNotifiedAt)
        .put("boards", JSONObject().apply { boards.forEach { (k, v) -> put(k, v.toJson()) } })

    companion object {
        fun fromJson(o: JSONObject): Announcement {
            val boards = LinkedHashMap<String, BoardStatus>()
            o.optJSONObject("boards")?.let { b ->
                b.keys().forEach { k ->
                    b.optJSONObject(k)?.let { bv ->
                        runCatching { BoardStatus.fromJson(bv) }.getOrNull()?.let { boards[k] = it }
                    }
                }
            }
            return Announcement(
                announcementId = o.optStr("announcement_id"),
                title = o.optStr("title"),
                message = o.optStr("message"),
                sentAt = toSeconds(o.optDouble("sent_at", 0.0)),
                boards = boards,
                timeoutNotifiedAt = o.optNullableDouble("timeout_notified_at")?.let(::toSeconds),
            )
        }
    }
}
