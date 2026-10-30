package com.innovalogix.logicshare.update

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.core.content.FileProvider
import com.innovalogix.logicshare.BuildConfig
import com.innovalogix.logicshare.core.AppLog
import com.innovalogix.logicshare.core.formatSize
import com.innovalogix.logicshare.core.optStr
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL

/**
 * Updates, the way LogicShare Windows does them: ask GitHub for the newest
 * release, compare tags, download the package asset, hand it to the system
 * installer. Here the installer is Android's own; the package is signed with
 * the same key as the installed app, so Android updates in place and keeps
 * the teacher's data.
 */
object Updater {
    /** ONE repo for the whole ecosystem. Releases are told apart by tag:
     *  "LogicShare-v1.1.2" carries this APK (and the Windows installer);
     *  "LogicBoard-v3.1.3" is LogicBoard's and is never offered here. So
     *  the check lists releases rather than asking for /latest, which is
     *  whichever app happened to ship last. */
    const val REPO = "YashWarrior44/LogicClassroom-Ecosystem"
    const val TAG_PREFIX = "LogicShare-"
    private const val LATEST = "https://api.github.com/repos/$REPO/releases?per_page=50"
    private val UA = "LogicShare-Updater/${BuildConfig.VERSION_NAME}"
    private const val TIMEOUT_MS = 20_000

    data class Info(
        val tag: String,
        val version: List<Int>,
        val url: String,
        val assetName: String,
        val assetSize: Long,
        val notes: String,
    ) {
        val versionText: String get() = version.joinToString(".")
        fun isNewer(): Boolean {
            val cur = parseVersion(BuildConfig.VERSION_NAME) ?: return false
            return compare(version, cur) > 0
        }
    }

    sealed class Check {
        data class Found(val info: Info) : Check()
        data class Failed(val message: String) : Check()
    }

    /** Windows' _parse_version: the first dotted version anywhere in the
     *  text, so v1.2.3, V1.2.3, 1.2.3-beta, "LogicShare v1.2.3" all work. A
     *  missing patch is zero. Null when there is no version at all — the safe
     *  direction to fail: a missed update, never a bogus one. */
    fun parseVersion(text: String?): List<Int>? {
        val m = Regex("(\\d+)\\.(\\d+)(?:\\.(\\d+))?").find(text ?: return null) ?: return null
        return listOf(m.groupValues[1].toInt(), m.groupValues[2].toInt(),
            m.groupValues[3].ifEmpty { "0" }.toInt())
    }

    fun compare(a: List<Int>, b: List<Int>): Int {
        for (i in 0 until maxOf(a.size, b.size)) {
            val d = (a.getOrElse(i) { 0 }).compareTo(b.getOrElse(i) { 0 })
            if (d != 0) return d
        }
        return 0
    }

    /** Ranked like Windows' _pick_installer_asset, for .apk: an
     *  installable package is the only thing worth downloading. */
    private fun pickAsset(assets: JSONArray?): JSONObject? {
        var best: JSONObject? = null
        var bestRank = -1
        for (i in 0 until (assets?.length() ?: 0)) {
            val a = assets!!.optJSONObject(i) ?: continue
            val name = a.optStr("name").lowercase()
            if (a.optStr("browser_download_url").isEmpty() || !name.endsWith(".apk")) continue
            val rank = when {
                "release" in name -> 2
                "logicshare" in name -> 1
                else -> 0
            }
            if (rank > bestRank) { best = a; bestRank = rank }
        }
        return best
    }

    /** LogicShare's newest usable release in the shared repo's list, or
     *  null when there is none tagged for us. A single release object (the
     *  old /latest shape) counts as a list of one. Prereleases are skipped
     *  as /latest skipped them; among releases tagged [TAG_PREFIX] that
     *  carry an APK, the highest version wins, whatever the list order. */
    fun selectRelease(body: String): JSONObject? {
        val trimmed = body.trimStart()
        val list = trimmed.startsWith("[")
        val items = if (list) JSONArray(trimmed) else JSONArray().put(JSONObject(trimmed))
        var best: JSONObject? = null
        var bestV: List<Int>? = null
        var firstOurs: JSONObject? = null
        for (i in 0 until items.length()) {
            val r = items.optJSONObject(i) ?: continue
            if (r.optBoolean("draft") || r.optBoolean("prerelease")) continue
            val tag = r.optStr("tag_name")
            if (list && !tag.lowercase().startsWith(TAG_PREFIX.lowercase())) continue
            if (firstOurs == null) firstOurs = r
            val v = parseVersion(tag) ?: continue
            if (pickAsset(r.optJSONArray("assets")) == null) continue
            if (bestV == null || compare(v, bestV) > 0) { best = r; bestV = v }
        }
        return best ?: firstOurs
    }

    /** Blocking. Every failure message is the Windows wording. */
    fun check(): Check {
        val conn = try {
            (URL(LATEST).openConnection() as HttpURLConnection).apply {
                connectTimeout = TIMEOUT_MS; readTimeout = TIMEOUT_MS
                setRequestProperty("User-Agent", UA)
                setRequestProperty("Accept", "application/vnd.github+json")
            }
        } catch (e: Exception) {
            return Check.Failed("Couldn't reach GitHub — check your internet connection and try again.")
        }
        try {
            val code = try { conn.responseCode } catch (e: Exception) {
                AppLog.w("Update check could not reach GitHub: ${e.message}")
                return Check.Failed("Couldn't reach GitHub — check your internet connection and try again.")
            }
            when (code) {
                404 -> return Check.Failed("No releases have been published yet.")
                403 -> return Check.Failed("GitHub is rate-limiting update checks right now. Try again in a little while.")
                200 -> {}
                else -> return Check.Failed("GitHub responded with HTTP $code.")
            }
            val data = try {
                selectRelease(conn.inputStream.use { it.readBytes().toString(Charsets.UTF_8) })
            } catch (e: Exception) {
                return Check.Failed("GitHub sent back something unreadable.")
            } ?: return Check.Failed("No LogicShare release has been published yet.")
            val tag = data.optStr("tag_name").ifEmpty { data.optStr("name") }.trim()
            val version = parseVersion(tag) ?: run {
                AppLog.w("Latest release tag is unparseable: $tag")
                return Check.Failed("The latest release is tagged “$tag”, which isn't a version number LogicShare understands.")
            }
            val asset = pickAsset(data.optJSONArray("assets"))
                ?: return Check.Failed("The latest release doesn't have an app package attached to it yet.")
            return Check.Found(Info(tag, version, asset.optStr("browser_download_url"),
                asset.optStr("name"), asset.optLong("size", 0), data.optStr("body").trim()))
        } finally {
            conn.disconnect()
        }
    }

    fun updatesDir(ctx: Context) = File(ctx.filesDir, "Updates")

    /** Named by version, so two releases can never be confused. */
    fun packagePath(ctx: Context, info: Info) =
        File(updatesDir(ctx), "LogicShare_v${info.versionText}.apk")

    /** A previously downloaded package for this release, verified against
     *  the size GitHub published; a mismatch is a cut-short download and is
     *  deleted rather than kept to fool the next attempt. */
    fun existingDownload(ctx: Context, info: Info): File? {
        val f = packagePath(ctx, info)
        if (!f.isFile) return null
        val ok = if (info.assetSize > 0) f.length() == info.assetSize else f.length() >= 1024
        if (!ok) {
            AppLog.i("Discarding ${f.name}: ${formatSize(f.length())} on disk, " +
                    "release says ${formatSize(info.assetSize)}")
            f.delete()
            return null
        }
        AppLog.i("Reusing the package already downloaded for ${info.versionText} (${formatSize(f.length())})")
        return f
    }

    /** Blocking download with progress. Returns the file, or throws with a
     *  user-facing message. ``cancelled`` is polled between chunks. */
    fun download(ctx: Context, info: Info, onProgress: (Long, Long) -> Unit,
                 cancelled: () -> Boolean): File {
        existingDownload(ctx, info)?.let { onProgress(1, 1); return it }
        val dir = updatesDir(ctx).apply { mkdirs() }
        // Clear out anything an earlier attempt left — never the target.
        val dest = packagePath(ctx, info)
        dir.listFiles()?.filter { it.name.endsWith(".apk") && it != dest }?.forEach { it.delete() }
        val part = File(dir, dest.name + ".part")
        try {
            val conn = (URL(info.url).openConnection() as HttpURLConnection).apply {
                connectTimeout = TIMEOUT_MS; readTimeout = TIMEOUT_MS
                setRequestProperty("User-Agent", UA)
                instanceFollowRedirects = true
            }
            try {
                if (conn.responseCode != 200) throw IllegalStateException("HTTP ${conn.responseCode}")
                val total = conn.contentLengthLong.takeIf { it > 0 } ?: info.assetSize
                var received = 0L
                conn.inputStream.use { input ->
                    part.outputStream().use { out ->
                        val buf = ByteArray(256 * 1024)
                        while (true) {
                            if (cancelled()) throw CancelledException()
                            val n = input.read(buf)
                            if (n < 0) break
                            out.write(buf, 0, n)
                            received += n
                            onProgress(received, total)
                        }
                    }
                }
                if (info.assetSize > 0 && received != info.assetSize)
                    throw IllegalStateException("short download")
            } finally {
                conn.disconnect()
            }
            if (!part.renameTo(dest)) throw IllegalStateException("could not finalise")
            return dest
        } catch (e: CancelledException) {
            part.delete()
            throw e
        } catch (e: Exception) {
            AppLog.w("Update download failed: ${e.message}")
            part.delete()
            throw IllegalStateException("The download failed. Check your internet connection and try again.")
        }
    }

    class CancelledException : Exception("cancelled")

    /** Android needs the user's say-so before an app may install packages. */
    fun canInstall(ctx: Context): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.O || ctx.packageManager.canRequestPackageInstalls()

    fun openInstallPermission(ctx: Context) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            ctx.startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                Uri.parse("package:${ctx.packageName}")).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        }
    }

    /** Hand the package to the system installer. */
    fun launchInstaller(ctx: Context, apk: File): String? = try {
        val uri = FileProvider.getUriForFile(ctx, "${ctx.packageName}.fileprovider", apk)
        ctx.startActivity(Intent(Intent.ACTION_VIEW)
            .setDataAndType(uri, "application/vnd.android.package-archive")
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK))
        AppLog.i("Update package handed to the installer: ${apk.name}")
        null
    } catch (e: Exception) {
        AppLog.e("Could not start the installer", e)
        "Couldn't start the installer: ${e.message}"
    }

    /** The Windows rule (prune_installed_updates): packages for a version
     *  <= the one running are spent; a newer one is a waiting update and is
     *  kept; anything without a version can never be reused. */
    fun pruneSpent(ctx: Context): List<String> {
        val cur = parseVersion(BuildConfig.VERSION_NAME) ?: return emptyList()
        val removed = mutableListOf<String>()
        updatesDir(ctx).listFiles()?.forEach { f ->
            // Finished packages only. A ".part" may be a download in progress
            // right now — this also runs two minutes after launch — and
            // download() clears stale partials itself before it starts.
            if (!f.name.endsWith(".apk")) return@forEach
            val v = parseVersion(f.nameWithoutExtension)
            if (v != null && compare(v, cur) > 0) return@forEach
            if (f.delete()) removed += f.name
        }
        if (removed.isNotEmpty()) AppLog.i("Removed spent update package(s): ${removed.joinToString()}")
        return removed
    }
}
