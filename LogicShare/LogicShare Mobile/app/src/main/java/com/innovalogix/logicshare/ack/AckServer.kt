package com.innovalogix.logicshare.ack

import com.innovalogix.logicshare.core.AppLog
import org.json.JSONObject
import java.io.BufferedInputStream
import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.io.OutputStream
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import kotlin.concurrent.thread

/**
 * The small HTTP listener boards POST acknowledgments to — Windows' AckServer.
 *
 * Runs for the whole life of the app, hosted by AckService so it survives the
 * UI being closed (the Android equivalent of hiding into the tray). Tries the
 * agreed port 16385, then up to eleven above it; every announcement carries
 * the port actually in use, so a non-default one still works end to end.
 */
class AckServer(private val tracker: AnnouncementTracker,
                private val preferredPort: Int = TEACHER_ACK_PORT) {

    @Volatile private var server: ServerSocket? = null
    @Volatile var port: Int = 0
        private set

    @Synchronized
    fun start(): Boolean {
        if (server != null) return true
        var lastErr: Exception? = null
        for (offset in 0 until 12) {
            val p = preferredPort + offset
            try {
                // reuseAddress stays OFF. On Linux it only permits rebinding a
                // port in TIME_WAIT — but there is no reason to want even that:
                // a second live LogicShare must fail to bind, which is what
                // drives the fallback to the next port.
                val s = ServerSocket()
                s.reuseAddress = false
                s.bind(java.net.InetSocketAddress(InetAddress.getByName("0.0.0.0"), p), 32)
                server = s
                port = p
                if (offset > 0) AppLog.w("Ack port $preferredPort unavailable; listening on $p instead")
                AppLog.i("Acknowledgment listener on 0.0.0.0:$p")
                thread(name = "LogicShare-AckServer", isDaemon = true) { acceptLoop(s) }
                return true
            } catch (e: Exception) {
                lastErr = e
            }
        }
        AppLog.e("Could not bind an acknowledgment port: ${lastErr?.message}")
        return false
    }

    @Synchronized
    fun stop() {
        try { server?.close() } catch (_: Exception) {}
        server = null
    }

    private fun acceptLoop(s: ServerSocket) {
        while (server === s) {
            try {
                val client = s.accept()
                // One short-lived thread per connection: a board's POST is a
                // few hundred bytes and there is at most a classroom of them.
                thread(isDaemon = true) { handle(client) }
            } catch (e: Exception) {
                if (server === s) AppLog.w("accept failed: ${e.message}")
            }
        }
    }

    private fun handle(client: Socket) {
        try {
            client.soTimeout = 8000
            val input = BufferedInputStream(client.getInputStream())
            val requestLine = readLine(input) ?: return
            val parts = requestLine.split(" ")
            if (parts.size < 2) return
            val method = parts[0]
            val path = parts[1].substringBefore('?').trimEnd('/')

            var contentLength = 0
            while (true) {
                val line = readLine(input) ?: break
                if (line.isEmpty()) break
                if (line.lowercase().startsWith("content-length:"))
                    contentLength = line.substringAfter(':').trim().toIntOrNull() ?: 0
            }
            val out = client.getOutputStream()

            if (method == "GET") {
                if (path == "/ping" || path.isEmpty()) {
                    reply(out, 200, JSONObject()
                        .put("status", "ok")
                        .put("role", "logicshare-teacher")
                        .put("ack_version", ACK_PROTOCOL_VERSION)
                        .put("teacher_device_id", tracker.deviceId))
                } else reply(out, 404, JSONObject().put("status", "error"))
                return
            }
            if (method != "POST" || path != "/ack") {
                reply(out, 404, JSONObject().put("status", "error"))
                return
            }

            val body = readBody(input, contentLength.coerceIn(0, 64 * 1024))
            val payload = try {
                JSONObject(if (body.isEmpty()) "{}" else body)
            } catch (e: Exception) {
                AppLog.w("Malformed acknowledgment body")
                reply(out, 400, JSONObject().put("status", "error").put("message", "bad json"))
                return
            }

            // Stamp the peer address. The SOCKET knows who connected; the body
            // only claims to. Overwrites anything in the body on purpose.
            var peer = client.inetAddress?.hostAddress.orEmpty()
            if (peer.startsWith("::ffff:")) peer = peer.substring(7)
            if (peer.isNotEmpty()) payload.put("ip", peer)

            val accepted = try {
                tracker.recordAck(payload)
            } catch (e: Exception) {
                AppLog.e("record_ack failed", e)
                // 500 so the board RETRIES — a 200 here would make it drop an
                // acknowledgment this device never stored.
                reply(out, 500, JSONObject().put("status", "error"))
                return
            }
            // 200 even for a duplicate or an unknown announcement: the board
            // has done its job and should stop retrying.
            reply(out, 200, JSONObject().put("status", "ok").put("accepted", accepted))
        } catch (e: Exception) {
            AppLog.w("ack handling failed: ${e.message}")
        } finally {
            try { client.close() } catch (_: Exception) {}
        }
    }

    private fun readLine(input: InputStream): String? {
        val buf = ByteArrayOutputStream()
        while (true) {
            val c = input.read()
            if (c == -1) return if (buf.size() == 0) null else buf.toString("UTF-8")
            if (c == '\n'.code) break
            if (c != '\r'.code) buf.write(c)
            if (buf.size() > 8192) return null
        }
        return buf.toString("UTF-8")
    }

    private fun readBody(input: InputStream, n: Int): String {
        val b = ByteArray(n)
        var read = 0
        while (read < n) {
            val r = input.read(b, read, n - read)
            if (r <= 0) break
            read += r
        }
        return String(b, 0, read, Charsets.UTF_8)
    }

    private fun reply(out: OutputStream, code: Int, body: JSONObject) {
        val reason = when (code) { 200 -> "OK"; 400 -> "Bad Request"; 404 -> "Not Found"
            else -> "Internal Server Error" }
        val bytes = body.toString().toByteArray(Charsets.UTF_8)
        out.write(("HTTP/1.1 $code $reason\r\nContent-Type: application/json\r\n" +
                "Content-Length: ${bytes.size}\r\nConnection: close\r\n\r\n")
            .toByteArray(Charsets.UTF_8))
        out.write(bytes)
        out.flush()
    }
}
