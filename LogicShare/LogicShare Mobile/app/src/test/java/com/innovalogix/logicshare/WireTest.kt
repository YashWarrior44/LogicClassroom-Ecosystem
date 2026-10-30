package com.innovalogix.logicshare

import com.innovalogix.logicshare.ack.AckServer
import com.innovalogix.logicshare.ack.AnnouncementTracker
import com.innovalogix.logicshare.ack.BoardState
import com.innovalogix.logicshare.core.Profile
import com.innovalogix.logicshare.net.announcePayload
import com.innovalogix.logicshare.net.isHostReachable
import com.innovalogix.logicshare.net.pyQuote
import com.innovalogix.logicshare.net.sendOverSocket
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.net.ServerSocket
import java.net.Socket
import java.nio.file.Files
import kotlin.concurrent.thread

/**
 * Real sockets. The bytes a board receives and the replies a board sends are
 * the contract; a mock would agree with whatever the code already does.
 */
class WireTest {

    /** A fake LogicBoard /upload: captures the request, answers like the
     *  real receiver. */
    @Test fun uploadSendsExactlyTheWindowsRequest() {
        val server = ServerSocket(0)
        var request = ByteArray(0)
        val t = thread {
            server.accept().use { s ->
                val input = s.getInputStream()
                val buf = ByteArrayOutputStream()
                val chunk = ByteArray(8192)
                // Read headers, then exactly Content-Length bytes.
                while (true) {
                    val n = input.read(chunk); if (n < 0) break
                    buf.write(chunk, 0, n)
                    val txt = buf.toString("ISO-8859-1")
                    val he = txt.indexOf("\r\n\r\n")
                    if (he >= 0) {
                        val cl = Regex("(?im)^content-length: (\\d+)").find(txt)!!.groupValues[1].toInt()
                        if (buf.size() - he - 4 >= cl) break
                    }
                }
                request = buf.toByteArray()
                val body = """{"status":"ok","saved_as":"My File (1).pdf"}""".toByteArray()
                s.getOutputStream().write(("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n" +
                        "Content-Length: ${body.size}\r\n\r\n").toByteArray() + body)
            }
        }
        val payload = ByteArray(300_000) { (it % 251).toByte() }
        val progress = mutableListOf<Pair<Long, Long>>()
        val headers = linkedMapOf(
            "X-First-Name" to pyQuote("Yash"), "X-Surname" to pyQuote("Jain"),
            "X-Short-Name" to "YJ", "X-Title" to "Sir",
            "X-Filename" to pyQuote("My File.pdf"), "Content-Type" to "application/octet-stream")
        val (code, body) = sendOverSocket("127.0.0.1", server.localPort, "/upload", headers,
            ByteArrayInputStream(payload), payload.size.toLong()) { s, tot -> progress += s to tot }
        t.join(5000); server.close()

        assertEquals(200, code)
        assertEquals("My File (1).pdf", JSONObject(body).getString("saved_as"))
        val text = String(request, Charsets.ISO_8859_1)
        val head = text.substringBefore("\r\n\r\n")
        assertTrue(head.startsWith("POST /upload HTTP/1.1\r\n"))
        for (h in listOf("Content-Length: 300000", "Connection: close", "X-First-Name: Yash",
            "X-Surname: Jain", "X-Short-Name: YJ", "X-Title: Sir",
            "X-Filename: My%20File.pdf", "Content-Type: application/octet-stream"))
            assertTrue("missing header $h in\n$head", head.contains(h))
        assertTrue("body corrupted", request.copyOfRange(request.size - payload.size, request.size)
            .contentEquals(payload))
        assertEquals("the last progress call must be (total, total)",
            300_000L to 300_000L, progress.last())
    }

    @Test fun theAnnouncePayloadIsTheWindowsPayload() {
        val p = Profile("Yash", "Jain", "YJ", "Sir")
        val plain = announcePayload("T", "M", p)
        assertEquals(setOf("title", "message", "first_name", "surname", "short_name", "title_honorific"),
            plain.keys().asSequence().toSet())
        val tracked = announcePayload("T", "M", p, "an_1", "td_x", 16385)
        assertEquals("an_1", tracked.getString("announcement_id"))
        assertEquals("td_x", tracked.getString("teacher_device_id"))
        assertEquals(16385, tracked.getInt("ack_port"))
        assertEquals(1, tracked.getInt("ack_version"))
    }

    private fun http(port: Int, req: String): Pair<Int, JSONObject> =
        Socket("127.0.0.1", port).use { s ->
            s.getOutputStream().write(req.toByteArray()); s.getOutputStream().flush()
            val resp = s.getInputStream().readBytes().toString(Charsets.UTF_8)
            resp.substringAfter(' ').substringBefore(' ').toInt() to JSONObject(resp.substringAfter("\r\n\r\n"))
        }

    private fun post(port: Int, path: String, body: String) = http(port,
        "POST $path HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n" +
                "Content-Length: ${body.toByteArray().size}\r\n\r\n$body")

    @Test fun theAckServerSpeaksTheProtocol() {
        val dir = Files.createTempDirectory("ls_ack").toFile()
        val tracker = AnnouncementTracker(dir, "td_me")
        tracker.recordSent("an_1", "T", "M", listOf(mapOf("ip" to "127.0.0.1", "class" to "11")))
        val free = ServerSocket(0).use { it.localPort }
        val server = AckServer(tracker, free)
        assertTrue(server.start())
        try {
            val (pc, ping) = http(server.port, "GET /ping HTTP/1.1\r\nHost: x\r\n\r\n")
            assertEquals(200, pc)
            assertEquals("logicshare-teacher", ping.getString("role"))
            assertEquals("td_me", ping.getString("teacher_device_id"))

            assertEquals(400, post(server.port, "/ack", "{not json").first)
            assertEquals(404, post(server.port, "/nope", "{}").first)

            // No ip in the body: the server stamps the peer (127.0.0.1), which
            // is what lets this ack adopt the IP-keyed row.
            val (c, r) = post(server.port, "/ack",
                """{"announcement_id":"an_1","board_device_id":"bd_1","teacher_device_id":"td_me"}""")
            assertEquals(200, c)
            assertTrue(r.getBoolean("accepted"))
            val b = tracker.get("an_1")!!.boards.values.single()
            assertEquals(BoardState.READ, b.state)
            assertEquals("bd_1", b.boardDeviceId)

            // A duplicate is still 200 (the board must stop retrying) but not accepted.
            val (c2, r2) = post(server.port, "/ack",
                """{"announcement_id":"an_1","board_device_id":"bd_1","teacher_device_id":"td_me"}""")
            assertEquals(200, c2)
            assertFalse(r2.getBoolean("accepted"))
        } finally {
            server.stop()
        }
    }

    @Test fun aTakenPortFallsBackToTheNextOne() {
        val dir = Files.createTempDirectory("ls_ack2").toFile()
        val tracker = AnnouncementTracker(dir, "td_me")
        val blocker = ServerSocket(0)
        val server = AckServer(tracker, blocker.localPort)
        try {
            assertTrue(server.start())
            assertTrue("bound the port another socket holds", server.port != blocker.localPort)
        } finally {
            server.stop(); blocker.close()
        }
    }

    @Test fun reachabilityTreatsRefusedAsPresent() {
        // Nothing listening on a just-freed localhost port: the host is there
        // and refuses — Windows counts that as reachable.
        val port = ServerSocket(0).use { it.localPort }
        assertTrue(isHostReachable("127.0.0.1", port))
        // TEST-NET-1 is unroutable by definition: silence means gone.
        assertFalse(isHostReachable("192.0.2.1", 9, timeoutMs = 300))
    }
}
