package com.innovalogix.logicshare.net

import com.innovalogix.logicshare.ack.ACK_PROTOCOL_VERSION
import com.innovalogix.logicshare.core.AppLog
import com.innovalogix.logicshare.core.Profile
import org.json.JSONObject
import java.io.InputStream
import java.net.ConnectException
import java.net.HttpURLConnection
import java.net.InetSocketAddress
import java.net.Socket
import java.net.SocketTimeoutException
import java.net.URL

/** The LogicBoard receiver's port. */
const val BOARD_PORT = 16384

/**
 * Windows' is_host_reachable: a TCP connect with a 1.5 s budget. A REFUSED
 * connection counts as reachable — the machine answered, it is there; only
 * silence means the board has gone.
 */
fun isHostReachable(ip: String, port: Int = BOARD_PORT, timeoutMs: Int = 1500): Boolean =
    try {
        Socket().use { it.connect(InetSocketAddress(ip, port), timeoutMs) }
        true
    } catch (_: ConnectException) {
        true
    } catch (_: Exception) {
        false
    }

/** Python's urllib.parse.quote with its default safe="/": percent-encode the
 *  UTF-8 bytes of everything except A-Z a-z 0-9 _ . - ~ and /. NOT
 *  URLEncoder, which writes spaces as "+" — the board decodes with
 *  unquote(), which leaves a "+" as a literal plus, so "My File.pdf" would
 *  arrive as "My+File.pdf". */
fun pyQuote(s: String): String {
    val sb = StringBuilder()
    for (b in s.toByteArray(Charsets.UTF_8)) {
        val c = b.toInt() and 0xff
        val ch = c.toChar()
        if (ch in 'A'..'Z' || ch in 'a'..'z' || ch in '0'..'9' || ch in "_.-~/") sb.append(ch)
        else sb.append('%').append("%02X".format(c))
    }
    return sb.toString()
}

/** Outcome of one HTTP call, classified the way Windows' workers classify
 *  requests' exceptions. */
sealed class HttpResult {
    data class Ok(val code: Int, val body: String) : HttpResult()
    data object ConnectFailed : HttpResult()
    data object TimedOut : HttpResult()
    data class Failed(val message: String) : HttpResult()
}

/** requests.post(url, json=..., timeout=(connect, read)). */
fun postJson(url: String, payload: JSONObject, connectMs: Int, readMs: Int): HttpResult {
    var conn: HttpURLConnection? = null
    return try {
        conn = (URL(url).openConnection() as HttpURLConnection).apply {
            requestMethod = "POST"
            connectTimeout = connectMs
            readTimeout = readMs
            doOutput = true
            setRequestProperty("Content-Type", "application/json")
            setRequestProperty("Accept", "application/json")
        }
        conn.outputStream.use { it.write(payload.toString().toByteArray(Charsets.UTF_8)) }
        val code = conn.responseCode
        val stream: InputStream? = if (code in 200..299) conn.inputStream else conn.errorStream
        val body = stream?.use { it.readBytes().toString(Charsets.UTF_8) }.orEmpty()
        HttpResult.Ok(code, body)
    } catch (_: SocketTimeoutException) {
        HttpResult.TimedOut
    } catch (_: ConnectException) {
        HttpResult.ConnectFailed
    } catch (e: java.net.NoRouteToHostException) {
        HttpResult.ConnectFailed
    } catch (e: java.net.UnknownHostException) {
        HttpResult.ConnectFailed
    } catch (e: Exception) {
        HttpResult.Failed(e.message ?: e.javaClass.simpleName)
    } finally {
        conn?.disconnect()
    }
}

fun parseJsonObject(body: String): JSONObject? =
    try { JSONObject(body) } catch (_: Exception) { null }

/** The /announce payload — Windows' AnnouncementWorker / BroadcastWorker. */
fun announcePayload(title: String, message: String, profile: Profile,
                    announcementId: String = "", teacherDeviceId: String = "",
                    ackPort: Int = 0): JSONObject {
    val o = JSONObject()
        .put("title", title)
        .put("message", message)
        .put("first_name", profile.firstName)
        .put("surname", profile.surname)
        .put("short_name", profile.initials)
        .put("title_honorific", profile.title)
    // Acknowledgment fields ONLY when we have them, so the traffic is
    // byte-identical to the old client otherwise and old boards stay happy.
    if (announcementId.isNotEmpty() && teacherDeviceId.isNotEmpty()) {
        o.put("announcement_id", announcementId)
            .put("teacher_device_id", teacherDeviceId)
            .put("ack_port", ackPort)
            .put("ack_version", ACK_PROTOCOL_VERSION)
    }
    return o
}

/** Single-board announcement — Windows' AnnouncementWorker. Returns null on
 *  success, or the exact user-facing failure line the Windows app shows. */
fun sendAnnouncement(ip: String, title: String, message: String, profile: Profile): String? {
    val url = "http://$ip:$BOARD_PORT/announce"
    return when (val r = postJson(url, announcePayload(title, message, profile), 5000, 10000)) {
        is HttpResult.Ok ->
            if (r.code == 200) null
            else { AppLog.e("Announce failed HTTP ${r.code} → $url"); "Something went wrong (HTTP ${r.code})💀" }
        HttpResult.ConnectFailed -> { AppLog.w("Announce connection error → $url"); "Couldn't reach the Smart Board…💀" }
        HttpResult.TimedOut -> { AppLog.w("Announce timed out → $url"); "Took too long — try again 😅" }
        is HttpResult.Failed -> { AppLog.e("Unexpected announce error: ${r.message}"); "Something went wrong while sending it…💀" }
    }
}
