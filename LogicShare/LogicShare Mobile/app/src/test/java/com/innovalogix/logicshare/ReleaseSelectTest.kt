package com.innovalogix.logicshare

import com.innovalogix.logicshare.update.Updater
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/** The ecosystem shares ONE GitHub repo. The phone must take LogicShare's
 *  newest release from it — never LogicBoard's, whichever shipped last. */
class ReleaseSelectTest {
    private fun rel(tag: String, vararg assets: String, pre: Boolean = false) =
        """{"tag_name":"$tag","prerelease":$pre,"assets":[""" +
            assets.joinToString(",") {
                """{"name":"$it","browser_download_url":"https://x/$it","size":5}"""
            } + "]}"

    @Test fun logicBoardsReleaseIsNeverOffered() {
        val body = "[" + rel("LogicBoard-v9.0.0", "LogicBoard.Installer.exe") + "," +
            rel("LogicShare-v1.1.2", "LogicShare.Installer.exe", "LogicShare.apk") + "]"
        assertEquals("LogicShare-v1.1.2", Updater.selectRelease(body)!!.getString("tag_name"))
    }

    @Test fun theHighestVersionWinsWhateverTheOrder() {
        val body = "[" + rel("LogicShare-v1.1.2", "a.apk") + "," +
            rel("LogicShare-v1.2.0", "b.apk") + "," + rel("LogicShare-v1.1.9", "c.apk") + "]"
        assertEquals("LogicShare-v1.2.0", Updater.selectRelease(body)!!.getString("tag_name"))
    }

    @Test fun aReleaseWithoutAnApkIsPassedOver() {
        val body = "[" + rel("LogicShare-v1.3.0", "LogicShare.Installer.exe") + "," +
            rel("LogicShare-v1.2.0", "LogicShare.apk") + "]"
        assertEquals("LogicShare-v1.2.0", Updater.selectRelease(body)!!.getString("tag_name"))
    }

    @Test fun prereleasesAreSkipped() {
        val body = "[" + rel("LogicShare-v2.0.0", "x.apk", pre = true) + "," +
            rel("LogicShare-v1.2.0", "y.apk") + "]"
        assertEquals("LogicShare-v1.2.0", Updater.selectRelease(body)!!.getString("tag_name"))
    }

    @Test fun nothingTaggedForUsMeansNothing() {
        assertNull(Updater.selectRelease("[" + rel("LogicBoard-v3.1.3", "LB.exe") + "]"))
        assertNull(Updater.selectRelease("[]"))
    }
}
