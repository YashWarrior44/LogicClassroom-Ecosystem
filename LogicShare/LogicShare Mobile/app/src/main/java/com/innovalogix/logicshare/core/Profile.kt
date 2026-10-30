package com.innovalogix.logicshare.core

import org.json.JSONObject
import java.io.File

/** The teacher. Same fields, same file name and same JSON as Windows'
 *  data.json. */
data class Profile(
    val firstName: String,
    val surname: String,
    val shortName: String,
    val title: String,          // "Sir" | "Ma'am"
) {
    val displayName: String get() = "$firstName $surname $title".trim()
    val initials: String get() = shortName.ifBlank { deriveShortName(firstName, surname) }

    fun toJson(): JSONObject = JSONObject()
        .put("first_name", firstName)
        .put("surname", surname)
        .put("short_name", shortName)
        .put("title", title)

    companion object {
        /** Windows: REQUIRED_FIELDS = ("first_name", "surname", "title"). */
        fun fromJson(o: JSONObject): Profile? {
            val first = o.optStr("first_name").ifBlank { o.optStr("firstName") }
            val sur = o.optStr("surname")
            val title = o.optStr("title")
            if (first.isBlank() || sur.isBlank() || title.isBlank()) return null
            val short = o.optStr("short_name").ifBlank { o.optStr("shortName") }
                .ifBlank { deriveShortName(first, sur) }
            return Profile(first, sur, short, title)
        }
    }
}

/** Windows' derive_short_name: initials of every word, upper-cased, max 4. */
fun deriveShortName(firstName: String, surname: String): String =
    "$firstName $surname".split(Regex("\\s+"))
        .filter { it.isNotEmpty() }
        .joinToString("") { it.first().uppercase() }
        .take(4)

class ProfileStore(private val dir: File) {
    private val file get() = File(dir, "data.json")

    fun load(): Profile? {
        val o = JsonStore.read(file) as? JSONObject ?: return null
        val p = Profile.fromJson(o)
        if (p == null) AppLog.w("Profile file missing required fields; treating as no profile")
        return p
    }

    fun save(p: Profile): Boolean =
        JsonStore.write(file, p.toJson()).also { if (it) AppLog.i("Profile saved") }
}
