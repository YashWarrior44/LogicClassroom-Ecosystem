package com.innovalogix.logicshare

import com.innovalogix.logicshare.ack.ACK_GIVE_UP_SECONDS
import com.innovalogix.logicshare.ack.ACK_TIMEOUT_SECONDS
import com.innovalogix.logicshare.ack.Announcement
import com.innovalogix.logicshare.ack.AnnouncementTracker
import com.innovalogix.logicshare.ack.BoardState
import com.innovalogix.logicshare.ack.BoardStatus
import com.innovalogix.logicshare.ack.TeacherIdentity
import com.innovalogix.logicshare.core.nowSeconds
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeoutOrNull
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.File
import java.nio.file.Files

/**
 * The acknowledgment tracker's edge cases — each one a rule LogicShare
 * Windows' logicshare_ack.py states and defends. The tracker is the one part
 * of the app whose failures are silent: a wrongly credited acknowledgment
 * looks exactly like a classroom that replied.
 */
class TrackerTest {
    private lateinit var dir: File
    private val me = "td_0123456789abcdef"

    @Before fun setUp() { dir = Files.createTempDirectory("ls_tracker").toFile() }

    private fun tracker() = AnnouncementTracker(dir, me)

    private fun target(ip: String, cls: String = "11", sec: String = "A", room: String = "12") =
        mapOf("ip" to ip, "class" to cls, "section" to sec, "room" to room)

    private fun ack(aid: String, board: String, ip: String = "", teacher: String = me) =
        JSONObject().put("announcement_id", aid).put("board_device_id", board)
            .put("teacher_device_id", teacher).put("acknowledged_at", nowSeconds())
            .apply { if (ip.isNotEmpty()) put("ip", ip) }

    @Test fun deliveryRekeysAndTheAckFindsItsRow() {
        val t = tracker()
        t.recordSent("an_1", "Test", "msg", listOf(target("10.0.0.5")))
        t.recordDelivery("an_1", "10.0.0.5", true, "Delivered",
            JSONObject().put("board_device_id", "bd_room12"))
        assertTrue(t.recordAck(ack("an_1", "bd_room12")))
        val b = t.get("an_1")!!.boards.values.single()
        assertEquals(BoardState.READ, b.state)
        assertEquals("bd_room12", b.boardDeviceId)
    }

    @Test fun anIpKeyedRowIsAdoptedOnlyByTheMatchingIp() {
        val t = tracker()
        t.recordSent("an_1", "T", "m", listOf(target("10.0.0.5", room = "5"), target("10.0.0.6", room = "6")))
        // No delivery response, so both rows are still keyed by IP. An ack
        // from .6 must land on room 6 — not on "the first row missing an id".
        t.recordAck(ack("an_1", "bd_six", ip = "10.0.0.6"))
        val rows = t.get("an_1")!!.boards.values
        assertEquals(BoardState.READ, rows.single { it.room == "6" }.state)
        assertEquals(BoardState.PENDING, rows.single { it.room == "5" }.state)
    }

    @Test fun withNoIpNothingIsAdoptedAndARowIsAdded() {
        val t = tracker()
        t.recordSent("an_1", "T", "m", listOf(target("10.0.0.5")))
        t.recordAck(ack("an_1", "bd_unknown"))
        val ann = t.get("an_1")!!
        assertEquals(2, ann.boards.size)
        assertEquals(BoardState.PENDING, ann.boards.values.first { it.ip == "10.0.0.5" }.state)
    }

    @Test fun duplicatesAndOtherTeachersAreIgnored() {
        val t = tracker()
        t.recordSent("an_1", "T", "m", listOf(target("10.0.0.5")))
        t.recordDelivery("an_1", "10.0.0.5", true, "", JSONObject().put("board_device_id", "bd_a"))
        assertFalse("someone else's ack", t.recordAck(ack("an_1", "bd_a", teacher = "td_other")))
        assertTrue(t.recordAck(ack("an_1", "bd_a")))
        assertFalse("duplicate", t.recordAck(ack("an_1", "bd_a")))
        assertFalse("unknown announcement", t.recordAck(ack("an_nope", "bd_a")))
    }

    @Test fun aLateDeliveryNeverDowngradesARead() {
        val t = tracker()
        t.recordSent("an_1", "T", "m", listOf(target("10.0.0.5")))
        t.recordDelivery("an_1", "10.0.0.5", true, "", JSONObject().put("board_device_id", "bd_a"))
        t.recordAck(ack("an_1", "bd_a"))
        t.recordDelivery("an_1", "10.0.0.5", true, "Delivered", null)
        assertEquals(BoardState.READ, t.get("an_1")!!.boards.values.single().state)
    }

    @Test fun hostilePayloadsCannotThrowOrRenderJunk() {
        val t = tracker()
        t.recordSent("an_1", "T", "m", listOf(target("10.0.0.5")))
        t.recordDelivery("an_1", "10.0.0.5", true, "", JSONObject().put("board_device_id", "bd_a"))
        val p = ack("an_1", "bd_a")
            .put("acknowledged_at", "not a number")
            .put("room", JSONObject().put("nested", "dict"))
        assertTrue(t.recordAck(p))
        val b = t.get("an_1")!!.boards.values.single()
        assertNotNull("bad timestamp should fall back to now", b.readAt)
        assertEquals("a nested object must not become the room", "12", b.room)
    }

    private fun old(aid: String, ageSeconds: Double, vararg boards: BoardStatus) =
        Announcement(aid, "Old", "m", nowSeconds() - ageSeconds,
            LinkedHashMap(boards.associateBy { it.key }))

    /** Subscribe BEFORE sweeping — timeoutReached has no replay, which is
     *  exactly the trap AckService.begin() fell into. */
    private fun warnings(t: AnnouncementTracker, block: () -> Unit): List<String> = runBlocking {
        val got = mutableListOf<String>()
        val job = launch(start = CoroutineStart.UNDISPATCHED) {
            t.timeoutReached.collect { got += it.first }
        }
        block()
        delay(200)
        job.cancel()
        got
    }

    @Test fun sweepTimesOutAndNotifiesExactlyOnce() {
        val t = tracker()
        t.importLegacy(listOf(old("an_1", ACK_TIMEOUT_SECONDS + 5.0,
            BoardStatus(boardDeviceId = "bd_a", state = BoardState.DELIVERED))))
        assertEquals("five minutes passed and nobody was told", listOf("an_1"), warnings(t) { t.sweep() })
        assertEquals(BoardState.TIMEOUT, t.get("an_1")!!.boards.values.single().state)
        assertEquals("told twice about the same announcement", emptyList<String>(), warnings(t) { t.sweep() })
    }

    @Test fun everythingReadInTimeProducesNoWarning() {
        val t = tracker()
        t.importLegacy(listOf(old("an_1", ACK_TIMEOUT_SECONDS + 5.0,
            BoardStatus(boardDeviceId = "bd_a", state = BoardState.READ))))
        assertEquals(emptyList<String>(), warnings(t) { t.sweep() })
    }

    @Test fun aWarningEmittedBeforeAnyoneListensIsLost() {
        // Documents the trap the service now avoids: an ordinary (queued)
        // subscription misses a sweep that runs straight after it.
        val t = tracker()
        t.importLegacy(listOf(old("an_1", ACK_TIMEOUT_SECONDS + 5.0,
            BoardStatus(boardDeviceId = "bd_a", state = BoardState.DELIVERED))))
        t.sweep()
        assertNull(runBlocking { withTimeoutOrNull(200) { t.timeoutReached.first() } })
    }

    @Test fun afterEightHoursTheyExpireAndCloseTheAnnouncement() {
        val t = tracker()
        t.importLegacy(listOf(old("an_1", ACK_GIVE_UP_SECONDS + 5.0,
            BoardStatus(boardDeviceId = "bd_a", state = BoardState.DELIVERED),
            BoardStatus(ip = "10.0.0.9", state = BoardState.OFFLINE))))
        t.sweep()
        val ann = t.get("an_1")!!
        assertTrue(ann.boards.values.all { it.state == BoardState.EXPIRED })
        assertFalse("the boot receiver would keep listening for nothing", t.hasOpenAnnouncements())
    }

    @Test fun persistsInTheWindowsFileFormat() {
        val t = tracker()
        t.recordSent("an_1", "Title", "Msg", listOf(target("10.0.0.5")))
        val raw = JSONArray(File(dir, "announcement_tracking.json").readText())
        val board = raw.getJSONObject(0).getJSONObject("boards").getJSONObject("ip:10.0.0.5")
        // Python's asdict(BoardStatus) keys — the Windows app reads these.
        for (k in listOf("board_device_id", "ip", "board_name", "room", "class_standard",
            "section", "state", "detail", "delivered_at", "read_at", "timeout_at",
            "expired_at", "board_version")) assertTrue("missing $k", board.has(k))
        val reloaded = tracker().get("an_1")!!
        assertEquals("Title", reloaded.title)
        assertEquals("11", reloaded.boards.values.single().classStandard)
    }

    @Test fun readsTheFlutterBuildsTrackingRecords() {
        // The Flutter build wrote "class" and integer milliseconds.
        val o = JSONObject().put("announcement_id", "an_f").put("title", "Old app")
            .put("message", "m").put("sent_at", 1_756_000_000_000L)
            .put("boards", JSONObject().put("bd_x", JSONObject()
                .put("board_device_id", "bd_x").put("class", "9").put("section", "B")
                .put("state", "read").put("read_at", 1_756_000_060_000L)))
        val a = Announcement.fromJson(o)
        assertEquals(1_756_000_000.0, a.sentAt, 0.001)
        val b = a.boards["bd_x"]!!
        assertEquals("9", b.classStandard)
        assertEquals(1_756_000_060.0, b.readAt!!, 0.001)
        assertEquals(BoardState.READ, b.state)
    }

    @Test fun theTeacherIdSurvivesAndAMigratedOneIsKept() {
        val a = TeacherIdentity(dir, migratedId = "td_fromflutter00001").deviceId
        assertEquals("td_fromflutter00001", a)
        assertEquals("regenerated — every queued ack would be orphaned", a, TeacherIdentity(dir).deviceId)
    }

    @Test fun labelsMatchWindows() {
        assertEquals("11A  ·  Room 12", BoardStatus(classStandard = "11", section = "A", room = "12").label)
        assertEquals("10.0.0.5", BoardStatus(ip = "10.0.0.5").label)
        assertEquals("Unknown board", BoardStatus().label)
    }
}
