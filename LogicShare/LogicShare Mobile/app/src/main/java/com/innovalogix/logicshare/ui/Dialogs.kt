package com.innovalogix.logicshare.ui

import androidx.compose.animation.AnimatedContent
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.innovalogix.logicshare.BuildConfig
import com.innovalogix.logicshare.core.Profile
import com.innovalogix.logicshare.core.formatSize
import com.innovalogix.logicshare.ui.theme.T
import com.innovalogix.logicshare.ui.theme.Type
import kotlinx.coroutines.delay
import java.util.Locale

/** Windows' UploadProgressDialog. No way out while the transfer runs. */
@Composable
fun UploadDialog(u: UploadState, onDone: () -> Unit) {
    val running = u.status is UploadStatus.Running
    LsDialog(onDone, dismissable = !running) {
        Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text(if (u.count > 1) "Sending File  (${u.index} of ${u.count})" else "Sending File", style = Type.title2)
            Text(buildAnnotatedString {
                withStyle(SpanStyle(color = T.TEXT_MUTED)) { append("Sending  ") }
                withStyle(SpanStyle(color = T.TEXT, fontWeight = FontWeight.Bold)) { append(u.fileName) }
                withStyle(SpanStyle(color = T.TEXT_DIM)) { append("  (${formatSize(u.total)})") }
            }, style = Type.callout)
            Text(buildAnnotatedString {
                withStyle(SpanStyle(color = T.TEXT_MUTED)) { append("→  ") }
                withStyle(SpanStyle(color = T.TEXT, fontWeight = FontWeight.Bold)) { append(u.target) }
            }, style = Type.callout)
            val pct = if (u.total > 0) (u.sent * 100 / u.total).toInt() else 0
            GradientProgress(if (u.total > 0) u.sent.toFloat() / u.total else 0f)
            when (val s = u.status) {
                is UploadStatus.Running -> Text(
                    if (u.sent == 0L) "Getting everything ready…💀"
                    else buildAnnotatedString {
                        withStyle(SpanStyle(color = T.TEXT_MUTED)) { append("Sending your file…📡  ·  ") }
                        withStyle(SpanStyle(color = T.TEXT, fontWeight = FontWeight.Bold)) { append(formatSize(u.sent)) }
                        append(" / ${formatSize(u.total)}  ·  ")
                        withStyle(SpanStyle(color = T.ACCENT)) { append("$pct%") }
                        if (u.speed.isNotEmpty()) withStyle(SpanStyle(color = T.SUCCESS)) { append("  ·  ⚡ ${u.speed}") }
                    }.text, style = Type.callout.copy(fontWeight = FontWeight.Bold))
                is UploadStatus.Done -> Column {
                    Text("✓  AYY!! File is on the board" + (if (s.savedAs.isNotEmpty()) " (${s.savedAs})" else "") + " 😎🔥",
                        style = Type.bodyBold.copy(color = T.SUCCESS))
                    Text(buildAnnotatedString {
                        append("Average speed: ")
                        withStyle(SpanStyle(color = T.ACCENT, fontWeight = FontWeight.Bold)) { append(s.avgSpeed) }
                        append(String.format(Locale.US, "  ·  Took %.1fs", s.tookSeconds))
                    }, style = Type.footnote)
                }
                is UploadStatus.Failed -> Text("✗  ${s.message}", style = Type.bodyBold.copy(color = T.DANGER))
            }
            Gap(4)
            GradientButton(if (u.status is UploadStatus.Done) "Done!!😎" else "Done",
                Modifier.fillMaxWidth(), enabled = !running, onClick = onDone)
        }
    }
}

/** Windows' AnnouncementDialog for one board: a form, then a status page;
 *  success closes itself after 2.2 s. */
@Composable
fun SingleAnnouncementScreen(board: Board, profile: Profile, status: AnnounceStatus?,
                             onSend: (String, String) -> Unit, onClose: () -> Unit) {
    var title by rememberSaveable { mutableStateOf("") }
    var msg by rememberSaveable { mutableStateOf("") }
    if (status != null) {
        LaunchedEffect(status) { if (status is AnnounceStatus.Sent) { delay(2200); onClose() } }
        LsDialog(onClose, dismissable = status !is AnnounceStatus.Sending) {
            AnimatedContent(status, label = "announce") { s ->
                Column(Modifier.fillMaxWidth(), horizontalAlignment = Alignment.CenterHorizontally,
                    verticalArrangement = Arrangement.spacedBy(12.dp)) {
                    val (icon, head, sub, colour) = when (s) {
                        AnnounceStatus.Sending -> Quad4("📡", "Sending it…📡", "Hold on a sec…😅", T.TEXT)
                        AnnounceStatus.Sent -> Quad4("🚀", "Let's go!! Sent!!😎🔥", "It's now showing on the Smart Board ✨", T.SUCCESS)
                        is AnnounceStatus.Failed -> Quad4("⚠️", "Couldn't Send…💀", s.message, T.DANGER)
                    }
                    Text(icon, fontSize = 54.sp)
                    Text(head, style = Type.title2.copy(color = colour), textAlign = TextAlign.Center)
                    Text(sub, style = Type.callout, textAlign = TextAlign.Center)
                    GradientButton(if (s is AnnounceStatus.Sent) "Done!!😎" else "Close",
                        Modifier.fillMaxWidth(), enabled = s !is AnnounceStatus.Sending, onClick = onClose)
                }
            }
        }
    }
    Column(Modifier.fillMaxSize().background(T.backdrop)) {
        ScreenTop("📢 Send Announcement", onClose)
        Column(Modifier.weight(1f).verticalScroll(rememberScrollState()).imePadding()
            .padding(horizontal = 20.dp, vertical = 14.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text(buildAnnotatedString {
                withStyle(SpanStyle(color = T.TEXT_MUTED)) { append("Going to   ") }
                withStyle(SpanStyle(color = T.TEXT, fontWeight = FontWeight.Bold)) { append(board.title) }
            }, style = Type.body)
            Hline()
            FieldLabel("TITLE")
            LsTextField(title, { title = it }, "e.g. Class Test Tomorrow", maxLength = 80)
            FieldLabel("MESSAGE")
            LsTextField(msg, { msg = it }, "Write your announcement here…", singleLine = false, minHeight = 120.dp)
            FieldLabel("PREVIEW")
            PreviewCard(profile, title, msg, 140)
        }
        Row(Modifier.fillMaxWidth().background(T.bar).navigationBarsPadding().imePadding()
            .padding(horizontal = 16.dp, vertical = 12.dp), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            SubtleButton("Cancel", Modifier.weight(0.4f), onClick = onClose)
            GradientButton("Send 🚀", Modifier.weight(0.6f), enabled = title.isNotBlank() && msg.isNotBlank()) {
                onSend(title.trim(), msg.trim())
            }
        }
    }
}

private data class Quad4(val a: String, val b: String, val c: String, val d: androidx.compose.ui.graphics.Color)

/** Windows' BroadcastProgressDialog. */
@Composable
fun BroadcastDialog(b: BroadcastState, onDone: () -> Unit) {
    val finished = b.finished
    LsDialog(onDone, dismissable = finished != null) {
        Column(Modifier.fillMaxWidth(), horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.spacedBy(10.dp)) {
            val (icon, head, colour) = when {
                finished == null -> Triple("🚀", "Sending Announcement", T.TEXT)
                finished.second == 0 -> Triple("✅", "Sent to ${finished.first} board${if (finished.first != 1) "s" else ""}!", T.SUCCESS)
                else -> Triple("⚠️", "Sent to ${finished.first}, ${finished.second} failed", T.WARNING)
            }
            Text(icon, fontSize = 44.sp)
            Text(head, style = Type.title2.copy(color = colour), textAlign = TextAlign.Center)
            Text("“${b.title}”", style = Type.callout, textAlign = TextAlign.Center)
            GradientProgress(if (b.total > 0) b.done.toFloat() / b.total else 0f)
            Text(when {
                finished == null && b.done == 0 -> "Delivering to ${b.total} board${if (b.total != 1) "s" else ""}…"
                finished == null -> "Delivering…  ${b.done} / ${b.total}"
                finished.second == 0 -> "Everything looks good 👍"
                else -> "Some boards didn't get it — check the list."
            }, style = Type.callout.copy(fontWeight = FontWeight.Bold), textAlign = TextAlign.Center)
            LazyColumn(Modifier.fillMaxWidth().heightIn(max = 260.dp), verticalArrangement = Arrangement.spacedBy(7.dp)) {
                items(b.rows) { r ->
                    Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(T.R_MD.dp)).background(T.FILL_SUBTLE)
                        .border(1.dp, if (r.ok) T.SUCCESS_BD else T.DANGER_BD, RoundedCornerShape(T.R_MD.dp))
                        .padding(horizontal = 14.dp, vertical = 10.dp), verticalAlignment = Alignment.CenterVertically) {
                        Text(if (r.ok) "✓" else "✕", color = if (r.ok) T.SUCCESS else T.DANGER,
                            fontWeight = FontWeight.Black, fontSize = 15.sp, modifier = Modifier.width(22.dp))
                        Text(r.title, style = Type.bodyBold, modifier = Modifier.weight(1f))
                        Text(r.detail, color = if (r.ok) T.SUCCESS else T.DANGER, fontWeight = FontWeight.Bold, fontSize = 12.sp)
                    }
                }
            }
            GradientButton("Done", Modifier.fillMaxWidth(), enabled = finished != null, onClick = onDone)
        }
    }
}

/** Windows' UpdateDialog: ask → download → ask again → hand off.
 *  Nothing downloads before "Download"; nothing installs before "Install
 *  Now". "Later" after downloading keeps the APK for next time. */
@Composable
fun UpdateDialog(u: UpdateUi.Offer, onPrimary: () -> Unit, onLater: () -> Unit) {
    LsDialog(onLater, dismissable = !u.busy) {
        Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text(if (u.downloaded) "Update downloaded ✅" else "A new version is available 🚀",
                style = Type.title2)
            Text(buildAnnotatedString {
                withStyle(SpanStyle(color = T.TEXT_MUTED)) { append("You're on ") }
                withStyle(SpanStyle(color = T.TEXT, fontWeight = FontWeight.Bold)) { append("v${BuildConfig.VERSION_NAME}") }
                withStyle(SpanStyle(color = T.TEXT_MUTED)) { append("  →  latest is ") }
                withStyle(SpanStyle(color = T.ACCENT, fontWeight = FontWeight.Bold)) { append("v${u.info.versionText}") }
            }, style = Type.body)
            if (u.info.notes.isNotEmpty()) {
                Text(u.info.notes, style = Type.callout, modifier = Modifier.fillMaxWidth().heightIn(max = 180.dp)
                    .clip(RoundedCornerShape(T.R_MD.dp)).background(T.FILL_SUBTLE).padding(12.dp)
                    .verticalScroll(rememberScrollState()))
            }
            u.progress?.let { (rec, tot) -> if (u.busy) GradientProgress(if (tot > 0) rec.toFloat() / tot else 0f) }
            val idle = when {
                u.downloaded -> "It's ready to install. Install it now? Android will ask you to " +
                    "confirm, and LogicShare keeps your profile and history. Choose Later to keep it for next time."
                else -> "Download it now?" +
                    (if (u.info.assetSize > 0) " It's ${com.innovalogix.logicshare.core.formatSize(u.info.assetSize)}." else "") +
                    " You'll be asked again before anything is installed."
            }
            Text(u.error ?: u.status ?: idle,
                style = Type.footnote.copy(color = if (u.error != null) T.DANGER else T.TEXT_SEC))
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                SubtleButton(if (u.busy) "Cancel" else if (u.error != null) "Close" else "Later",
                    Modifier.weight(0.4f), onClick = onLater)
                GradientButton(when {
                    u.error != null -> "Try Again"
                    u.downloaded -> "Install Now"
                    else -> "Download"
                }, Modifier.weight(0.6f), enabled = !u.busy, onClick = onPrimary)
            }
        }
    }
}
