package com.innovalogix.logicshare.core

import java.text.Normalizer

/**
 * Understand what a teacher types into the search box — a port of LogicShare
 * Windows' smart_search.py. Both are held to the same cases in
 * LogicShare/shared/smart_search_cases.json, so a query means the same thing
 * on a laptop and on a phone. See the Python module for the long form.
 *
 * "11a", "11-A", "class 11 A", "XI A", "11th A", "eleventh a", "std 11 sec a"
 * all mean Class 11 section A; "room 12", "rm12", "r 12" mean room 12; a bare
 * "12" means class 12 OR room 12; "29.127" matches an address. Keywords
 * tolerate one typo ("clas", "romm").
 */
object SmartSearch {

    data class Query(
        val classes: List<String> = emptyList(),
        val sections: List<String> = emptyList(),
        val rooms: List<String> = emptyList(),
        val numbers: List<String> = emptyList(),
        val words: List<String> = emptyList(),
        val ip: String = "",
    ) {
        val empty: Boolean
            get() = classes.isEmpty() && sections.isEmpty() && rooms.isEmpty() &&
                numbers.isEmpty() && words.isEmpty() && ip.isEmpty()
    }

    private val CLASS_WORDS = setOf("class", "cls", "std", "standard", "grade", "gr", "klass")
    private val ROOM_WORDS = setOf("room", "rm", "roomno")
    private val SECTION_WORDS = setOf("section", "sec", "div", "division")
    private val STOP = setOf("board", "boards", "smart", "the", "of", "in", "for", "no",
        "number", "and", "to", "a_")
    private val FUZZY_KEYWORDS = linkedMapOf("class" to "class", "standard" to "class",
        "grade" to "class", "room" to "room", "section" to "section", "division" to "section")
    private val ORDINAL_SUFFIX = setOf("st", "nd", "rd", "th")
    private val ROMAN = mapOf("i" to 1, "ii" to 2, "iii" to 3, "iv" to 4, "v" to 5, "vi" to 6,
        "vii" to 7, "viii" to 8, "ix" to 9, "x" to 10, "xi" to 11, "xii" to 12)
    private val WORD_NUMBERS = mapOf(
        "one" to 1, "two" to 2, "three" to 3, "four" to 4, "five" to 5, "six" to 6,
        "seven" to 7, "eight" to 8, "nine" to 9, "ten" to 10, "eleven" to 11, "twelve" to 12,
        "first" to 1, "second" to 2, "third" to 3, "fourth" to 4, "fifth" to 5,
        "sixth" to 6, "seventh" to 7, "eighth" to 8, "ninth" to 9, "tenth" to 10,
        "eleventh" to 11, "twelfth" to 12)
    private val IP_RE = Regex("""\d{1,3}(?:\.\d{1,3})+\.?|\.\d{1,3}(?:\.\d{1,3})*\.?""")

    /** Lower-case and strip accents, keeping every script intact. */
    fun normalize(text: String?): String =
        Normalizer.normalize(text ?: "", Normalizer.Form.NFKD)
            .replace(Regex("\\p{Mn}+"), "")
            .lowercase()

    /** Within one insert, delete, substitute or adjacent transposition. */
    fun edit1(a0: String, b0: String): Boolean {
        if (a0 == b0) return true
        var a = a0; var b = b0
        if (kotlin.math.abs(a.length - b.length) > 1) return false
        if (a.length == b.length) {
            val diff = a.indices.filter { a[it] != b[it] }
            if (diff.size == 1) return true
            return diff.size == 2 && diff[1] == diff[0] + 1 &&
                a[diff[0]] == b[diff[1]] && a[diff[1]] == b[diff[0]]
        }
        if (a.length > b.length) { val t = a; a = b; b = t }
        var i = 0; var j = 0; var skipped = false
        while (i < a.length && j < b.length) {
            if (a[i] != b[j]) {
                if (skipped) return false
                skipped = true; j++
            } else { i++; j++ }
        }
        return true
    }

    private fun keyword(t: String): String {
        if (t in CLASS_WORDS) return "class"
        if (t in ROOM_WORDS) return "room"
        if (t in SECTION_WORDS) return "section"
        if (t.length >= 4) for ((kw, kind) in FUZZY_KEYWORDS) if (edit1(t, kw)) return kind
        return ""
    }

    private fun number(t: String): String? {
        if (t.isNotEmpty() && t.all { it in '0'..'9' }) return t.trimStart('0').ifEmpty { "0" }
        WORD_NUMBERS[t]?.let { return it.toString() }
        if (t.length >= 2) ROMAN[t]?.let { return it.toString() }
        return null
    }

    private fun normValue(v: String?): String {
        val s = normalize(v).trim()
        return number(s) ?: s
    }

    fun parse(query: String): Query {
        val classes = mutableListOf<String>()
        val sections = mutableListOf<String>()
        val rooms = mutableListOf<String>()
        val numbers = mutableListOf<String>()
        val words = mutableListOf<String>()
        var ip = ""

        var s = normalize(query)
        IP_RE.find(s)?.let { m ->
            ip = m.value.trim('.')
            s = s.substring(0, m.range.first) + " " + s.substring(m.range.last + 1)
        }
        s = s.replace(Regex("""[-_/,;:·|()#.'"]+"""), " ")
        s = s.replace(Regex("(?<=\\d)(?=[a-z])|(?<=[a-z])(?=\\d)"), " ")
        val toks = s.split(Regex("\\s+")).filter { it.isNotEmpty() }

        var pending = ""
        var lastNumIdx: Int? = null
        var i = 0
        while (i < toks.size) {
            val t = toks[i]
            val nxt = toks.getOrElse(i + 1) { "" }

            if (t in ORDINAL_SUFFIX && i > 0 && number(toks[i - 1]) != null) {
                lastNumIdx?.let { classes += numbers.removeAt(it) }
                lastNumIdx = null
                i++; continue
            }

            var kw = keyword(t)
            if (t == "r" && number(nxt) != null) kw = "room"
            if (kw.isNotEmpty()) {
                pending = kw; lastNumIdx = null
                i++; continue
            }

            val num = number(t)
            if (num != null) {
                when {
                    pending == "class" -> { classes += num; lastNumIdx = null }
                    pending == "room" -> { rooms += num; lastNumIdx = null }
                    pending == "section" -> { sections += t; lastNumIdx = null }
                    t in WORD_NUMBERS && listOf("th", "st", "nd", "rd").any { t.endsWith(it) } &&
                        t !in setOf("one", "two", "three") -> { classes += num; lastNumIdx = null }
                    t in ROMAN -> { classes += num; lastNumIdx = null }
                    else -> { numbers += num; lastNumIdx = numbers.lastIndex }
                }
                pending = ""
                i++; continue
            }

            if (t.length == 1 && t[0] in 'a'..'z') {
                sections += t
                lastNumIdx?.let { classes += numbers.removeAt(it) }
                lastNumIdx = null
                pending = ""
                i++; continue
            }

            if (pending == "section") sections += t
            else if (t !in STOP) words += t
            pending = ""
            lastNumIdx = null
            i++
        }
        return Query(classes, sections, rooms, numbers, words, ip)
    }

    private fun roomMatches(want: String, room: String?): Boolean {
        val r = normValue(room)
        if (r == want) return true
        val lead = Regex("^\\d+").find(r)?.value ?: return false
        return (lead.trimStart('0').ifEmpty { "0" }) == want
    }

    private fun textHas(word: String, hay: String): Boolean {
        if (word in hay) return true
        if (word.length >= 4) return hay.split(Regex("[^\\p{L}\\p{N}_]+")).any { it.isNotEmpty() && edit1(word, it) }
        return false
    }

    fun matchBoard(q: Query, cls: String, section: String, room: String, ip: String): Boolean {
        if (q.empty) return true
        val c = normValue(cls); val sec = normValue(section)
        if (q.ip.isNotEmpty() && q.ip !in ip) return false
        if (q.classes.any { c != it }) return false
        if (q.sections.any { sec != it }) return false
        if (q.rooms.any { !roomMatches(it, room) }) return false
        if (q.numbers.any { c != it && !roomMatches(it, room) }) return false
        val hay = normalize("class $cls $section $cls$section room $room $ip")
        return q.words.all { textHas(it, hay) }
    }

    /** ``recipients``: (class, section, room) triples. */
    fun matchHistory(q: Query, title: String, message: String,
                     recipients: List<Triple<String, String, String>>): Boolean {
        if (q.empty) return true
        val text = normalize("$title $message")
        fun recipOk(r: Triple<String, String, String>): Boolean {
            val c = normValue(r.first); val s = normValue(r.second)
            return q.classes.all { c == it } && q.sections.all { s == it } &&
                q.rooms.all { roomMatches(it, r.third) }
        }
        if (q.classes.isNotEmpty() || q.sections.isNotEmpty() || q.rooms.isNotEmpty()) {
            if (recipients.none(::recipOk)) return false
        }
        for (n in q.numbers) {
            val inRecips = recipients.any { normValue(it.first) == n || roomMatches(n, it.third) }
            if (!inRecips && !Regex("(?<!\\d)${Regex.escape(n)}(?!\\d)").containsMatchIn(text)) return false
        }
        val labels = normalize(recipients.joinToString(" ") {
            "class ${it.first} ${it.second} ${it.first}${it.second} room ${it.third}" })
        return q.words.all { textHas(it, text) || textHas(it, labels) }
    }
}
