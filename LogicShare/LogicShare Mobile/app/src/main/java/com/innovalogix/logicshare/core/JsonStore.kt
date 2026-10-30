package com.innovalogix.logicshare.core

import org.json.JSONArray
import org.json.JSONObject
import org.json.JSONTokener
import java.io.File
import java.io.FileOutputStream

/**
 * Atomic JSON files — the same discipline as LogicShare Windows'
 * ``_atomic_write_json``: write a temp file beside the target, fsync it,
 * then rename over the original. A crash or a dead battery mid-write leaves
 * either the old file or the new one, never half of each.
 */
object JsonStore {

    fun write(file: File, value: Any): Boolean {
        val tmp = File(file.parentFile, file.name + ".tmp")
        return try {
            file.parentFile?.mkdirs()
            val text = when (value) {
                is JSONObject -> value.toString(2)
                is JSONArray -> value.toString(2)
                else -> value.toString()
            }
            FileOutputStream(tmp).use { out ->
                out.write(text.toByteArray(Charsets.UTF_8))
                out.flush()
                out.fd.sync()
            }
            if (!tmp.renameTo(file)) {
                // renameTo will not replace an existing file on every
                // filesystem Android ships with; delete and retry.
                file.delete()
                if (!tmp.renameTo(file)) throw IllegalStateException("rename failed")
            }
            true
        } catch (e: Exception) {
            AppLog.e("Atomic write failed for ${file.name}", e)
            tmp.delete()
            false
        }
    }

    /** Parsed JSON, or null when absent. A corrupt file is quarantined to
     *  ``<name>.corrupt`` so the next save starts clean instead of failing
     *  on every launch — Windows' load_profile does the same. */
    fun read(file: File): Any? {
        if (!file.exists()) return null
        return try {
            JSONTokener(file.readText(Charsets.UTF_8)).nextValue()
        } catch (e: Exception) {
            AppLog.w("${file.name} is corrupt; quarantining and starting fresh", e)
            try {
                val q = File(file.parentFile, file.nameWithoutExtension + ".corrupt")
                q.delete()
                file.renameTo(q)
            } catch (_: Exception) {
            }
            null
        }
    }
}

/** Seconds since the epoch as a Double — the unit every LogicShare file and
 *  every wire payload uses (Python's ``time.time()``). */
fun nowSeconds(): Double = System.currentTimeMillis() / 1000.0

/** A JSON number that may have been written in milliseconds (the Flutter
 *  build stored ``millisecondsSinceEpoch``) normalised to seconds. Anything
 *  past the year 5138 in seconds is certainly milliseconds. */
fun toSeconds(v: Double): Double = if (v > 1e11) v / 1000.0 else v

fun JSONObject.optStr(key: String): String =
    if (has(key) && !isNull(key)) opt(key)?.toString() ?: "" else ""

fun JSONObject.optNullableDouble(key: String): Double? =
    if (has(key) && !isNull(key)) optDouble(key).takeIf { !it.isNaN() } else null

fun JSONObject.putNullable(key: String, value: Any?): JSONObject =
    put(key, value ?: JSONObject.NULL)
