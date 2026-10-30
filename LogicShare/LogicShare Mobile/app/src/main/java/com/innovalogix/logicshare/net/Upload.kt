package com.innovalogix.logicshare.net

import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import com.innovalogix.logicshare.core.AppLog
import com.innovalogix.logicshare.core.Profile
import com.innovalogix.logicshare.core.formatSize
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.InputStream
import java.net.InetSocketAddress
import java.net.Socket
import java.net.SocketTimeoutException

/** A file the teacher picked, or shared into LogicShare from another app. */
data class PickedFile(val uri: Uri, val name: String, val size: Long)

/** Read the display name and size Android's content resolver reports. */
fun describeUri(ctx: Context, uri: Uri): PickedFile {
    var name = uri.lastPathSegment?.substringAfterLast('/') ?: "file"
    var size = -1L
    try {
        ctx.contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE),
            null, null, null)?.use { c ->
            if (c.moveToFirst()) {
                val ni = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                val si = c.getColumnIndex(OpenableColumns.SIZE)
                if (ni >= 0 && !c.isNull(ni)) name = c.getString(ni)
                if (si >= 0 && !c.isNull(si)) size = c.getLong(si)
            }
        }
    } catch (e: Exception) {
        AppLog.w("Could not describe $uri: ${e.message}")
    }
    return PickedFile(uri, name, size)
}

sealed class UploadOutcome {
    data class Success(val response: JSONObject) : UploadOutcome()
    data class Failure(val message: String) : UploadOutcome()
}

internal class UploadError(val kind: String, msg: String) : Exception(msg)

private const val CONNECT_TIMEOUT_MS = 5_000          // UPLOAD_CONNECT_TIMEOUT
private const val IDLE_TIMEOUT_MS = 300_000           // UPLOAD_IDLE_TIMEOUT
private const val PROGRESS_THROTTLE_MS = 50L          // PROGRESS_THROTTLE
private const val SNDBUF = 4 * 1024 * 1024            // UPLOAD_SO_SNDBUF
private const val CHUNK = 256 * 1024

/**
 * Send a file to a LogicBoard receiver — Windows' UploadWorker +
 * _send_file_over_socket.
 *
 * A hand-written HTTP/1.1 POST rather than HttpURLConnection, for the same
 * reasons the Windows build bypasses requests: one buffer, straight to the
 * socket, with progress measured on the actual bytes written. Headers,
 * timeouts and every user-facing message are the Windows ones.
 *
 * ``onProgress(sent, total)`` is throttled to 50 ms and always ends with a
 * (total, total) call. Blocking — run it off the main thread.
 */
fun uploadFile(ctx: Context, ip: String, file: PickedFile, profile: Profile,
               onProgress: (Long, Long) -> Unit): UploadOutcome {
    val url = "http://$ip:$BOARD_PORT/upload"
    // Content-Length is required, and a few providers do not report a size.
    // Spool those to the cache first so the length is known exactly.
    var spooled: File? = null
    return try {
        var size = file.size
        if (size < 0) {
            spooled = File(ctx.cacheDir, "upload_${System.nanoTime()}.bin")
            ctx.contentResolver.openInputStream(file.uri)!!.use { i ->
                spooled.outputStream().use { o -> i.copyTo(o) }
            }
            size = spooled.length()
        }
        val headers = linkedMapOf(
            "X-First-Name" to pyQuote(profile.firstName),
            "X-Surname" to pyQuote(profile.surname),
            "X-Short-Name" to profile.initials,
            "X-Title" to profile.title,
            "X-Filename" to pyQuote(file.name),
            "Content-Type" to "application/octet-stream",
        )
        AppLog.i("Upload start: ${file.name} (${formatSize(size)}) → $url")
        val input: InputStream = spooled?.inputStream()
            ?: ctx.contentResolver.openInputStream(file.uri)
            ?: throw UploadError("io", "Could not open the file")

        val (code, body) = input.use { sendOverSocket(ip, BOARD_PORT, "/upload", headers, it, size, onProgress) }
        if (code == 200) {
            AppLog.i("Upload OK: ${file.name}")
            UploadOutcome.Success(parseJsonObject(body) ?: JSONObject().put("status", "ok"))
        } else {
            AppLog.e("Upload failed HTTP $code: ${file.name}")
            UploadOutcome.Failure("Something went wrong (HTTP $code)💀")
        }
    } catch (e: UploadError) {
        when (e.kind) {
            "connect" -> {
                AppLog.w("Upload connect error → $url: ${e.message}")
                UploadOutcome.Failure("Couldn't reach the Smart Board…💀")
            }
            "timeout" -> {
                AppLog.w("Upload timeout → $url: ${e.message}")
                UploadOutcome.Failure("Took too long — try again 😅")
            }
            else -> {
                AppLog.w("Upload ${e.kind} error → $url: ${e.message}")
                UploadOutcome.Failure("Something went wrong while sending the file…💀")
            }
        }
    } catch (e: SecurityException) {
        // Android withdrew our access to a file shared in from another app
        // (the sharing app's grant can lapse). Say what to do about it rather
        // than "something went wrong".
        AppLog.w("No permission to read ${file.uri}: ${e.message}")
        UploadOutcome.Failure("LogicShare can't open that file any more — share or pick it again 😅")
    } catch (e: Exception) {
        AppLog.e("Unexpected upload error", e)
        UploadOutcome.Failure("Something went wrong while sending the file…💀")
    } finally {
        spooled?.delete()
    }
}

internal fun sendOverSocket(host: String, port: Int, path: String,
                           headers: Map<String, String>, body: InputStream, size: Long,
                           onProgress: (Long, Long) -> Unit): Pair<Int, String> {
    val head = StringBuilder("POST $path HTTP/1.1\r\n")
    head.append("Host: $host:$port\r\n")
    head.append("Content-Length: $size\r\n")
    head.append("Connection: close\r\n")
    headers.forEach { (k, v) -> head.append("$k: $v\r\n") }
    head.append("\r\n")

    val sock = Socket()
    try {
        try {
            sock.connect(InetSocketAddress(host, port), CONNECT_TIMEOUT_MS)
        } catch (e: SocketTimeoutException) {
            throw UploadError("connect", e.message ?: "connect timed out")
        } catch (e: Exception) {
            throw UploadError("connect", e.message ?: "connect failed")
        }
        runCatching { sock.tcpNoDelay = true }
        runCatching { sock.sendBufferSize = SNDBUF }
        sock.soTimeout = IDLE_TIMEOUT_MS

        val out = sock.getOutputStream()
        try {
            // Headers are ASCII by construction: every value that could carry
            // anything else went through pyQuote.
            out.write(head.toString().toByteArray(Charsets.US_ASCII))
            val buf = ByteArray(CHUNK)
            var sent = 0L
            var lastEmit = 0L
            while (sent < size) {
                val n = body.read(buf, 0, minOf(buf.size.toLong(), size - sent).toInt())
                if (n < 0) throw UploadError("io", "File ended before its reported size")
                out.write(buf, 0, n)
                sent += n
                val now = System.currentTimeMillis()
                if (now - lastEmit >= PROGRESS_THROTTLE_MS || sent >= size) {
                    lastEmit = now
                    onProgress(sent, size)
                }
            }
            out.flush()
        } catch (e: UploadError) {
            throw e
        } catch (e: SocketTimeoutException) {
            throw UploadError("timeout", "Upload idle for too long")
        } catch (e: Exception) {
            throw UploadError("io", e.message ?: "write failed")
        }

        // Response: status line, headers, then a small JSON body.
        val input = sock.getInputStream()
        val raw = ByteArrayOutputStream()
        val one = ByteArray(4096)
        try {
            while (true) {
                val n = input.read(one)
                if (n < 0) break
                raw.write(one, 0, n)
                val txt = raw.toString("ISO-8859-1")
                val he = txt.indexOf("\r\n\r\n")
                if (he >= 0) {
                    val cl = Regex("(?im)^content-length:\\s*(\\d+)").find(txt.substring(0, he))
                        ?.groupValues?.get(1)?.toIntOrNull()
                    if (cl == null || raw.size() - (he + 4) >= cl) break
                }
            }
        } catch (e: SocketTimeoutException) {
            if (raw.size() == 0) throw UploadError("timeout", "No response from receiver")
        }
        val bytes = raw.toByteArray()
        val txt = String(bytes, Charsets.ISO_8859_1)
        val he = txt.indexOf("\r\n\r\n")
        if (he < 0) throw UploadError("protocol", "Malformed response (no header)")
        val status = txt.substringBefore("\r\n").split(" ").getOrNull(1)?.toIntOrNull()
            ?: throw UploadError("protocol", "Malformed status line")
        val respBody = String(bytes, he + 4, bytes.size - he - 4, Charsets.UTF_8)
        return status to respBody
    } finally {
        runCatching { sock.shutdownOutput() }
        runCatching { sock.close() }
    }
}
