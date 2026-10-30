package com.innovalogix.logicshare

import com.innovalogix.logicshare.core.SmartSearch
import org.json.JSONObject
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * Runs LogicShare/shared/smart_search_cases.json — the SAME file LogicShare
 * Windows' test_smart_search.py runs — so a query means the same thing on a
 * phone as on a laptop.
 */
class SmartSearchTest {

    private fun cases(): JSONObject {
        // toRealPath: the build may run through an ASCII junction, whose
        // parent is not the LogicShare folder.
        val app = File(System.getProperty("user.dir")).toPath().toRealPath().toFile()
        val f = File(app, "../../shared/smart_search_cases.json").canonicalFile
        assertTrue("shared cases not found at $f", f.exists())
        return JSONObject(f.readText(Charsets.UTF_8))
    }

    private fun names(o: JSONObject) = o.keys().asSequence().toList()

    @Test fun everySharedBoardCase() {
        val data = cases()
        val boards = data.getJSONObject("boards")
        val bad = mutableListOf<String>()
        val list = data.getJSONArray("board_cases")
        for (i in 0 until list.length()) {
            val c = list.getJSONObject(i)
            val q = SmartSearch.parse(c.getString("q"))
            val got = names(boards).filter { k ->
                val b = boards.getJSONObject(k)
                SmartSearch.matchBoard(q, b.getString("class"), b.getString("section"),
                    b.getString("room"), b.getString("ip"))
            }.sorted()
            val want = (0 until c.getJSONArray("match").length()).map { c.getJSONArray("match").getString(it) }.sorted()
            if (got != want) bad += "'${c.getString("q")}': expected $want, got $got  ($q)"
        }
        assertTrue(bad.joinToString("\n"), bad.isEmpty())
    }

    @Test fun everySharedHistoryCase() {
        val data = cases()
        val hist = data.getJSONObject("history")
        val bad = mutableListOf<String>()
        val list = data.getJSONArray("history_cases")
        for (i in 0 until list.length()) {
            val c = list.getJSONObject(i)
            val q = SmartSearch.parse(c.getString("q"))
            val got = names(hist).filter { k ->
                val h = hist.getJSONObject(k)
                val r = h.getJSONArray("recipients")
                val recips = (0 until r.length()).map {
                    val t = r.getJSONArray(it); Triple(t.getString(0), t.getString(1), t.getString(2))
                }
                SmartSearch.matchHistory(q, h.getString("title"), h.getString("message"), recips)
            }.sorted()
            val want = (0 until c.getJSONArray("match").length()).map { c.getJSONArray("match").getString(it) }.sorted()
            if (got != want) bad += "'${c.getString("q")}': expected $want, got $got  ($q)"
        }
        assertTrue(bad.joinToString("\n"), bad.isEmpty())
    }

    @Test fun hostileQueriesCannotCrash() {
        for (q in listOf("((((", "\\", "[a-z]+", "*".repeat(500), "\u0000", "🙂🙂", "....", "---")) {
            SmartSearch.matchBoard(SmartSearch.parse(q), "11", "A", "12", "10.0.0.1")
            SmartSearch.matchHistory(SmartSearch.parse(q), "t", "m", listOf(Triple("11", "A", "12")))
        }
    }
}
