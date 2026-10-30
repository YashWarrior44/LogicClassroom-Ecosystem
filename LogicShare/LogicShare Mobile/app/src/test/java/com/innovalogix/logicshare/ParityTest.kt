package com.innovalogix.logicshare

import com.innovalogix.logicshare.core.deriveShortName
import com.innovalogix.logicshare.core.formatClass
import com.innovalogix.logicshare.core.formatSize
import com.innovalogix.logicshare.net.pyQuote
import com.innovalogix.logicshare.update.Updater
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Every expected value here was produced by running LogicShare Windows' own
 * Python (urllib.parse.quote, _parse_version, format_size,
 * derive_short_name) — not written from expectation. The two apps talk to the
 * same boards and must produce the same bytes.
 */
class ParityTest {

    @Test fun quoteMatchesPythonUrllib() {
        val cases = mapOf(
            "My File.pdf" to "My%20File.pdf",
            "a/b c" to "a/b%20c",
            "~_.-" to "~_.-",
            "C++ notes+1.docx" to "C%2B%2B%20notes%2B1.docx",
            "नमस्ते.pdf" to "%E0%A4%A8%E0%A4%AE%E0%A4%B8%E0%A5%8D%E0%A4%A4%E0%A5%87.pdf",
            "50% off & more?.png" to "50%25%20off%20%26%20more%3F.png",
            "Yash" to "Yash",
            "O'Brien" to "O%27Brien",
            "émile" to "%C3%A9mile",
        )
        for ((input, expected) in cases) assertEquals("quote($input)", expected, pyQuote(input))
    }

    /** The reason pyQuote exists: java.net.URLEncoder writes a space as "+",
     *  and the board's unquote() keeps "+" literally. */
    @Test fun spacesAreNeverPlus() {
        assertFalse(pyQuote("a b").contains('+'))
        assertEquals("a%20b", pyQuote("a b"))
    }

    @Test fun versionParsingMatchesPython() {
        val ok = mapOf(
            "v1.2.3" to listOf(1, 2, 3), "V1.2.3" to listOf(1, 2, 3), "1.2.3" to listOf(1, 2, 3),
            "v1.2.3-beta.1" to listOf(1, 2, 3), "LogicShare v1.2.3" to listOf(1, 2, 3),
            "v1.2" to listOf(1, 2, 0), "release-1.2.3" to listOf(1, 2, 3), " 1.2.3 " to listOf(1, 2, 3),
            // The real release tag on GitHub, and the real asset name.
            "V1.1.1" to listOf(1, 1, 1), "LogicShare.V1.1.1" to listOf(1, 1, 1),
        )
        for ((t, v) in ok) assertEquals(t, v, Updater.parseVersion(t))
        assertNull(Updater.parseVersion("nope"))
        assertNull(Updater.parseVersion(""))
    }

    @Test fun versionsCompareAsNumbersNotText() {
        assertTrue(Updater.compare(listOf(1, 10, 0), listOf(1, 9, 0)) > 0)
        assertEquals(0, Updater.compare(listOf(1, 1, 1), listOf(1, 1, 1)))
        assertTrue(Updater.compare(listOf(1, 1, 1), listOf(1, 2, 0)) < 0)
    }

    @Test fun sizesMatchPythonFormatSize() {
        val cases = mapOf(0L to "0 B", 1L to "1 B", 1023L to "1023 B", 1024L to "1.0 KB",
            1536L to "1.5 KB", 1048575L to "1024.0 KB", 1048576L to "1.0 MB",
            5L * 1024 * 1024 * 1024 to "5.0 GB", 3L * 1024 * 1024 * 1024 * 1024 to "3.0 TB")
        for ((n, s) in cases) assertEquals("format_size($n)", s, formatSize(n))
    }

    @Test fun shortNamesMatchPython() {
        assertEquals("YJ", deriveShortName("Yash", "Jain"))
        assertEquals("AMDL", deriveShortName("anna maria", "de la cruz"))
        assertEquals("", deriveShortName("", ""))
    }

    @Test fun classFormatting() {
        assertEquals("Class 11-A", formatClass("11", "A"))
        assertEquals("Class 11", formatClass("11", ""))
    }
}
