package com.innovalogix.logicshare.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.innovalogix.logicshare.ack.Announcement
import com.innovalogix.logicshare.ack.AnnouncementTracker
import com.innovalogix.logicshare.ack.BoardState
import com.innovalogix.logicshare.ack.BoardStatus
import com.innovalogix.logicshare.core.HistoryEntry
import com.innovalogix.logicshare.core.HistoryStore
import com.innovalogix.logicshare.core.Profile
import com.innovalogix.logicshare.core.ackClock
import com.innovalogix.logicshare.core.deriveShortName
import com.innovalogix.logicshare.core.timeAgoShort
import com.innovalogix.logicshare.ui.theme.T
import com.innovalogix.logicshare.ui.theme.Type

/** Top bar for a pushed screen: back chevron and a large bold title. */
@Composable
fun ScreenTop(title: String, onBack: (() -> Unit)?, trailing: @Composable () -> Unit = {}) {
    Column(Modifier.fillMaxWidth().background(T.bar).statusBarsPadding()
        .padding(start = 8.dp, end = 16.dp, top = 6.dp, bottom = 12.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            if (onBack != null) Text("‹  Back", style = Type.headline.copy(color = T.ACCENT_HI),
                modifier = Modifier.clip(RoundedCornerShape(10.dp)).clickable(onClick = onBack)
                    .padding(horizontal = 10.dp, vertical = 8.dp))
            Spacer(Modifier.weight(1f))
            trailing()
        }
        Text(title, style = Type.largeTitle, modifier = Modifier.padding(start = 12.dp, top = 2.dp))
    }
    Box(Modifier.fillMaxWidth().size(1.dp).background(T.DIVIDER))
}

// ── profile ─────────────────────────────────────────────────────────────

/** Windows' TeacherProfileDialog — first run and Edit Profile. */
@Composable
fun ProfileScreen(existing: Profile?, onSave: (String, String, String, String) -> Unit,
                  onBack: (() -> Unit)?) {
    var first by rememberSaveable { mutableStateOf(existing?.firstName.orEmpty()) }
    var sur by rememberSaveable { mutableStateOf(existing?.surname.orEmpty()) }
    var short by rememberSaveable { mutableStateOf(existing?.initials.orEmpty()) }
    var title by rememberSaveable { mutableStateOf(existing?.title ?: "Sir") }
    // A custom short name only while the field actually has content —
    // clearing it brings the auto-suggestion back (Windows' fix for a Save
    // button that otherwise stayed disabled forever).
    var userEditedShort by rememberSaveable { mutableStateOf(existing != null && existing.shortName.isNotBlank()) }

    fun suggest(f: String, s: String) { if (!userEditedShort) short = deriveShortName(f.trim(), s.trim()) }
    val canSave = first.isNotBlank() && sur.isNotBlank() && short.isNotBlank()
    val preview = short.trim().uppercase().ifEmpty { deriveShortName(first.trim(), sur.trim()) }.ifEmpty { "?" }

    Column(Modifier.fillMaxSize().background(T.backdrop)) {
        if (existing != null) ScreenTop("Edit Profile", onBack)
        Column(
            Modifier.weight(1f).verticalScroll(rememberScrollState()).imePadding()
                .then(if (existing == null) Modifier.statusBarsPadding() else Modifier)
                .padding(horizontal = 22.dp, vertical = 20.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            if (existing == null) {
                Gap(18)
                Logo(76.dp)
                Gap(6)
                Text("LogicShare", style = Type.largeTitle)
                Text("Quick setup — tell me who you are 👋", style = Type.body)
            } else {
                Text("Update your name, short name, or title.", style = Type.body)
            }
            Gap(4); Hline(); Gap(4)
            FieldLabel("FIRST NAME")
            LsTextField(first, { first = it; suggest(it, sur) }, "e.g. Priya",
                caps = KeyboardCapitalization.Words)
            FieldLabel("SURNAME")
            LsTextField(sur, { sur = it; suggest(first, it) }, "e.g. Sharma",
                caps = KeyboardCapitalization.Words)
            FieldLabel("SHORT NAME  (YOUR INITIALS)")
            Row(verticalAlignment = Alignment.CenterVertically) {
                LsTextField(short, { short = it; userEditedShort = it.isNotBlank()
                    if (it.isBlank()) suggest(first, sur) }, "e.g. PS",
                    Modifier.weight(1f), maxLength = 4, caps = KeyboardCapitalization.Characters)
                Spacer(Modifier.width(14.dp))
                Avatar(preview, 52.dp)
            }
            FieldLabel("TITLE")
            Segmented(listOf("Sir", "Ma'am"), title) { title = it }
        }
        Box(Modifier.navigationBarsPadding().imePadding().padding(horizontal = 22.dp, vertical = 14.dp)) {
            GradientButton(if (existing == null) "Let's go!! 😎" else "Save", Modifier.fillMaxWidth(),
                enabled = canSave) {
                val f = first.trim(); val s = sur.trim()
                val sh = short.trim().uppercase().ifEmpty { deriveShortName(f, s) }.take(4)
                onSave(f, s, sh, title)
            }
        }
    }
}

// ── composer ────────────────────────────────────────────────────────────

/** Windows' AnnouncementComposer: write once, then pick boards. */
@Composable
fun ComposerScreen(profile: Profile, onBack: () -> Unit, onHistory: () -> Unit,
                   onSelect: (String, String) -> Unit) {
    var title by rememberSaveable { mutableStateOf("") }
    var msg by rememberSaveable { mutableStateOf("") }
    Column(Modifier.fillMaxSize().background(T.backdrop)) {
        ScreenTop("📢 Create Announcement", onBack)
        Column(
            Modifier.weight(1f).verticalScroll(rememberScrollState()).imePadding()
                .padding(horizontal = 20.dp, vertical = 14.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            Text("Write it once — you'll pick which boards get it next 😎", style = Type.body)
            Row(Modifier.fillMaxWidth().glassCard(radius = T.R_MD).padding(12.dp),
                verticalAlignment = Alignment.CenterVertically) {
                Avatar(profile.initials, 42.dp)
                Spacer(Modifier.width(12.dp))
                Column {
                    Text(profile.displayName, style = Type.bodyBold)
                    Text("Short name: ${profile.initials}", style = Type.footnote)
                }
            }
            FieldLabel("TITLE")
            LsTextField(title, { title = it }, "e.g. Exam Schedule", maxLength = 80)
            FieldLabel("MESSAGE")
            LsTextField(msg, { msg = it },
                "Type your announcement here…\nLine breaks and paragraphs are supported.",
                singleLine = false, minHeight = 130.dp)
            FieldLabel("PREVIEW")
            PreviewCard(profile, title, msg, 160)
        }
        Row(Modifier.fillMaxWidth().background(T.bar).navigationBarsPadding().imePadding()
            .padding(horizontal = 16.dp, vertical = 12.dp),
            horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            SubtleButton("🕘  History", Modifier.weight(0.38f), onClick = onHistory)
            GradientButton("Select Recipients  →", Modifier.weight(0.62f),
                enabled = title.isNotBlank() && msg.isNotBlank()) { onSelect(title.trim(), msg.trim()) }
        }
    }
}

/** The announcement as the board will show it. */
@Composable
fun PreviewCard(profile: Profile, title: String, msg: String, limit: Int) {
    Row(Modifier.fillMaxWidth().glassCard().padding(16.dp)) {
        Avatar(profile.initials, 52.dp)
        Spacer(Modifier.width(14.dp))
        Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
            Text(profile.displayName, style = Type.footnote)
            Text(title.trim().ifEmpty { "Title goes here" }, style = Type.headline.copy(fontWeight = FontWeight.ExtraBold))
            val m = msg.trim().ifEmpty { "Your message will appear here…" }
            Text(if (m.length > limit) m.take(limit) + "…" else m, style = Type.callout)
        }
    }
}

// ── history ─────────────────────────────────────────────────────────────

/** Windows' ACK_STATE_STYLE. */
fun ackStyle(state: String): Triple<Color, Color, String> = when (state) {
    BoardState.READ -> Triple(T.SUCCESS, T.SUCCESS_BG, "Read")
    BoardState.DELIVERED -> Triple(T.WARNING, T.WARNING_BG, "Pending")
    BoardState.PENDING -> Triple(T.WARNING, T.WARNING_BG, "Sending…")
    BoardState.TIMEOUT -> Triple(T.DANGER, T.DANGER_BG, "No response")
    BoardState.EXPIRED -> Triple(T.DANGER, T.DANGER_BG, "Expired")
    BoardState.OFFLINE -> Triple(T.TEXT_MUTED, T.FILL, "Offline")
    else -> Triple(T.TEXT_MUTED, T.FILL, state.ifEmpty { "—" })
}

/** History filters — the same choices as LogicShare Windows. */
enum class HistoryStatus(val label: String) {
    ALL("All"), AWAITING("Awaiting replies"), ALL_READ("Read by everyone"), PROBLEMS("No response / offline")
}
enum class HistoryPeriod(val label: String) { ALL("All time"), TODAY("Today"), WEEK("Last 7 days") }

fun historyStatusMatches(ann: Announcement?, s: HistoryStatus): Boolean {
    if (s == HistoryStatus.ALL) return true
    ann ?: return false
    val states = ann.boards.values.map { it.state }
    return when (s) {
        HistoryStatus.AWAITING -> states.any { it == BoardState.PENDING || it == BoardState.DELIVERED }
        HistoryStatus.ALL_READ -> ann.allRead()
        HistoryStatus.PROBLEMS -> states.any { it == BoardState.TIMEOUT || it == BoardState.EXPIRED || it == BoardState.OFFLINE }
        HistoryStatus.ALL -> true
    }
}

fun historyPeriodMatches(sentAt: Double, p: HistoryPeriod, now: Long = System.currentTimeMillis()): Boolean = when (p) {
    HistoryPeriod.ALL -> true
    HistoryPeriod.WEEK -> sentAt * 1000 >= now - 7L * 86_400_000
    HistoryPeriod.TODAY -> {
        val fmt = java.text.SimpleDateFormat("yyyyMMdd", java.util.Locale.US)
        fmt.format(java.util.Date((sentAt * 1000).toLong())) == fmt.format(java.util.Date(now))
    }
}

@Composable
fun HistoryScreen(history: HistoryStore, tracker: AnnouncementTracker, onBack: () -> Unit,
                  onReport: (String) -> Unit, onNoReport: () -> Unit) {
    var version by remember { mutableIntStateOf(0) }
    // Repaint when an acknowledgment lands so the chips stay honest while
    // the screen is open.
    LaunchedEffect(Unit) { tracker.statusChanged.collect { version++ } }
    val entries = remember(version) { history.load() }
    var query by rememberSaveable { mutableStateOf("") }
    var status by rememberSaveable { mutableStateOf(HistoryStatus.ALL) }
    var period by rememberSaveable { mutableStateOf(HistoryPeriod.ALL) }
    var filterOpen by remember { mutableStateOf(false) }
    val parsed = remember(query) { com.innovalogix.logicshare.core.SmartSearch.parse(query) }
    val shown = remember(entries, parsed, status, period, version) {
        entries.filter { e ->
            val ann = if (e.announcementId.isNotEmpty()) tracker.get(e.announcementId) else null
            historyStatusMatches(ann, status) && historyPeriodMatches(e.sentAt, period) &&
                com.innovalogix.logicshare.core.SmartSearch.matchHistory(parsed, e.title, e.message,
                    e.recipients.map { Triple(it.cls, it.section, it.room) })
        }
    }
    val filtering = status != HistoryStatus.ALL || period != HistoryPeriod.ALL
    Column(Modifier.fillMaxSize().background(T.backdrop)) {
        ScreenTop("\uD83D\uDD58 Recent Announcements", onBack) {
            if (entries.isNotEmpty()) SubtleButton("Clear All") { history.clear(); version++ }
        }
        if (entries.isNotEmpty()) Row(Modifier.fillMaxWidth().padding(start = 16.dp, end = 16.dp, top = 12.dp),
            verticalAlignment = Alignment.CenterVertically) {
            LsTextField(query, { query = it }, "Search titles, messages, classes",
                Modifier.weight(1f), maxLength = 80,
                caps = KeyboardCapitalization.None)
            Spacer(Modifier.width(8.dp))
            Box {
                Box(Modifier.clip(RoundedCornerShape(T.R_MD.dp))
                    .background(if (filtering) T.accent else androidx.compose.ui.graphics.SolidColor(T.FILL_HOVER))
                    .clickable { filterOpen = true }.padding(horizontal = 14.dp, vertical = 15.dp)) {
                    Text(if (filtering) "\u26A1 \u2022" else "\u26A1", fontSize = 16.sp,
                        color = Color.White, fontWeight = FontWeight.ExtraBold)
                }
                androidx.compose.material3.DropdownMenu(filterOpen, { filterOpen = false },
                    shape = RoundedCornerShape(T.R_LG.dp), containerColor = Color(0xF2141D2B)) {
                    Caption("STATUS", modifier = Modifier.padding(start = 18.dp, top = 8.dp, bottom = 2.dp))
                    HistoryStatus.entries.forEach { st ->
                        androidx.compose.material3.DropdownMenuItem(
                            text = { Text((if (status == st) "\u2713  " else "      ") + st.label, style = Type.bodyBold) },
                            onClick = { status = st; filterOpen = false })
                    }
                    Caption("WHEN", modifier = Modifier.padding(start = 18.dp, top = 8.dp, bottom = 2.dp))
                    HistoryPeriod.entries.forEach { pe ->
                        androidx.compose.material3.DropdownMenuItem(
                            text = { Text((if (period == pe) "\u2713  " else "      ") + pe.label, style = Type.bodyBold) },
                            onClick = { period = pe; filterOpen = false })
                    }
                }
            }
        }
        if (entries.isNotEmpty() && (filtering || query.isNotBlank())) Row(
            Modifier.fillMaxWidth().padding(start = 22.dp, end = 16.dp, top = 8.dp),
            verticalAlignment = Alignment.CenterVertically) {
            Caption("SHOWING ${shown.size} OF ${entries.size}", color = T.ACCENT_HI, modifier = Modifier.weight(1f))
            Text("Clear", style = Type.footnote.copy(color = T.ACCENT_HI, fontWeight = FontWeight.Bold),
                modifier = Modifier.clip(RoundedCornerShape(8.dp)).clickable {
                    query = ""; status = HistoryStatus.ALL; period = HistoryPeriod.ALL }.padding(6.dp))
        }
        if (entries.isEmpty()) {
            Box(Modifier.fillMaxSize().padding(40.dp), contentAlignment = Alignment.Center) {
                Text("No announcements yet.\nSend one and it'll show up here \uD83D\uDE0E",
                    style = Type.body.copy(color = T.TEXT_DIM), textAlign = TextAlign.Center)
            }
        } else if (shown.isEmpty()) {
            Box(Modifier.fillMaxSize().padding(40.dp), contentAlignment = Alignment.Center) {
                Text("Nothing matches.\nTry fewer words, or clear the filters.",
                    style = Type.body.copy(color = T.TEXT_DIM), textAlign = TextAlign.Center)
            }
        } else LazyColumn(
            contentPadding = PaddingValues(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
            modifier = Modifier.navigationBarsPadding(),
        ) {
            items(shown, key = { it.id }) { e ->
                HistoryCard(e, if (e.announcementId.isNotEmpty()) tracker.get(e.announcementId) else null,
                    onReport = { if (tracker.get(e.announcementId) != null) onReport(e.announcementId) else onNoReport() },
                    onDelete = { history.delete(e.id); version++ })
            }
        }
    }
}

@Composable
private fun HistoryCard(e: HistoryEntry, ann: Announcement?, onReport: () -> Unit, onDelete: () -> Unit) {
    Column(Modifier.fillMaxWidth().glassCard().padding(16.dp), verticalArrangement = Arrangement.spacedBy(7.dp)) {
        Text(e.title.ifEmpty { "—" }, style = Type.headline.copy(fontWeight = FontWeight.ExtraBold))
        Text(if (e.message.length > 110) e.message.take(110) + "…" else e.message, style = Type.callout)
        val n = e.recipients.size
        Text("📡 $n board${if (n != 1) "s" else ""}   ·   ${timeAgoShort(e.sentAt)}", style = Type.footnote)
        if (ann != null && ann.boards.isNotEmpty()) {
            val counts = ann.counts()
            Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                for (s in listOf(BoardState.READ, BoardState.DELIVERED, BoardState.PENDING,
                    BoardState.TIMEOUT, BoardState.EXPIRED, BoardState.OFFLINE)) {
                    val c = counts[s] ?: 0
                    if (c == 0) continue
                    val (col, bg, label) = ackStyle(s)
                    StateChip(col, bg, "$c  $label", dot = false)
                }
            }
        }
        Gap(2)
        Row(verticalAlignment = Alignment.CenterVertically) {
            // One action, explained on tap rather than greyed out: a disabled
            // button on a touch screen has no tooltip and reads as broken.
            GradientButton("📋   Show Full Report", Modifier.weight(1f), onClick = onReport)
            Spacer(Modifier.width(10.dp))
            DangerButton("🗑", onClick = onDelete)
        }
    }
}

// ── report ──────────────────────────────────────────────────────────────

/** Windows' AnnouncementReportDialog: outstanding boards first, live. */
@Composable
fun ReportScreen(tracker: AnnouncementTracker, announcementId: String, onBack: () -> Unit) {
    var version by remember { mutableIntStateOf(0) }
    LaunchedEffect(announcementId) {
        tracker.statusChanged.collect { if (it == announcementId) version++ }
    }
    val ann = remember(version) { tracker.get(announcementId) }
    Column(Modifier.fillMaxSize().background(T.backdrop)) {
        ScreenTop("Report", onBack)
        if (ann == null) {
            Box(Modifier.fillMaxSize().padding(40.dp), contentAlignment = Alignment.Center) {
                Text("This announcement is no longer in your history.",
                    style = Type.body.copy(color = T.TEXT_DIM), textAlign = TextAlign.Center)
            }
            return@Column
        }
        LazyColumn(contentPadding = PaddingValues(18.dp), verticalArrangement = Arrangement.spacedBy(9.dp),
            modifier = Modifier.navigationBarsPadding()) {
            item {
                Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    Text(ann.title.ifEmpty { "—" }, style = Type.title2)
                    Text(ann.message, style = Type.callout)
                    val read = ann.counts()[BoardState.READ] ?: 0
                    val total = ann.boards.size
                    Text("$read of $total board${if (total != 1) "s" else ""} acknowledged   ·   sent ${timeAgoShort(ann.sentAt)}",
                        style = Type.footnote.copy(fontWeight = FontWeight.Bold))
                    Gap(4); Hline(); Gap(4)
                }
            }
            val outstanding = ann.unacknowledged()
            if (outstanding.isNotEmpty()) {
                item {
                    Caption("NO ACKNOWLEDGMENT FROM ${outstanding.size} BOARD${if (outstanding.size != 1) "S" else ""}",
                        color = T.DANGER)
                    Gap(4)
                    Text("These boards have not confirmed yet. They may be offline, or the class may not " +
                            "be in the room right now — it does not mean the announcement was never received.",
                        style = Type.footnote.copy(fontWeight = FontWeight.Medium))
                }
                items(outstanding, key = { "o_" + it.key }) { AckRow(it) }
                item { Gap(4); Hline(); Gap(4) }
            }
            item { Caption("ALL BOARDS") }
            val all = ann.boards.values.sortedWith(compareBy({ BoardState.SEVERITY[it.state] ?: 9 }, { it.label }))
            items(all, key = { "a_" + it.key }) { AckRow(it) }
        }
    }
}

@Composable
private fun AckRow(b: BoardStatus) {
    Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(T.R_MD.dp)).background(T.FILL_SUBTLE)
        .padding(horizontal = 14.dp, vertical = 11.dp), verticalAlignment = Alignment.CenterVertically) {
        Column(Modifier.weight(1f)) {
            Text(b.label, style = Type.bodyBold)
            // The moment it last moved — "Read 14:03" answers the follow-up
            // question before it is asked.
            val stamp = when {
                b.state == BoardState.READ && b.readAt != null -> "Read at ${ackClock(b.readAt)}"
                (b.state == BoardState.TIMEOUT || b.state == BoardState.EXPIRED) && b.deliveredAt != null ->
                    "Delivered ${ackClock(b.deliveredAt)} · no reply"
                b.deliveredAt != null -> "Delivered ${ackClock(b.deliveredAt)}"
                else -> b.detail
            }
            if (stamp.isNotEmpty()) Text(stamp, style = Type.footnote)
        }
        val (c, bg, label) = ackStyle(b.state)
        StateChip(c, bg, label)
    }
}

// ── about / help ────────────────────────────────────────────────────────

@Composable
fun AboutDialog(version: String, onDismiss: () -> Unit) {
    LsDialog(onDismiss) {
        Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Logo(52.dp); Spacer(Modifier.width(12.dp))
                Text("LogicShare", style = Type.title)
            }
            Text("Version $version  •  Beta", style = Type.footnote.copy(color = T.TEXT_DIM))
            Hline()
            Text("Send files and announcements straight to the Smart Board in your classroom — " +
                    "no cables, no USB sticks. Just hit Send and go 😎🔥", style = Type.body)
            Text("Free and open source  •  GPL-3.0", style = Type.footnote)
            Gap(4)
            GradientButton("Got it 😎", Modifier.fillMaxWidth(), onClick = onDismiss)
        }
    }
}

@Composable
fun HelpDialog(onDismiss: () -> Unit) {
    LsDialog(onDismiss) {
        Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text("🛠  Help & Tips", style = Type.title2)
            Hline()
            for ((em, q, ans) in listOf(
                Triple("📡", "Don't see your board?💀",
                    "Make sure LogicBoard is open on the Smart Board and both devices are on the same Wi-Fi."),
                Triple("📄", "File won't send?💀",
                    "Tap ⋯ → Send File, pick your file and confirm. Works with PDFs, Word, PowerPoint, images and videos."),
                Triple("📢", "How do I send an announcement?",
                    "Tap ⋯ on a board card → Send Announcement. Type a title and message, then hit Send 🚀"),
                Triple("🔄", "Still stuck?😅",
                    "Hit Refresh to look again. If nothing shows up, try restarting LogicBoard on the Smart Board."),
            )) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(em, fontSize = 17.sp, modifier = Modifier.width(30.dp))
                    Text(q, style = Type.bodyBold)
                }
                Text(ans, style = Type.callout, modifier = Modifier.padding(start = 30.dp))
            }
            Gap(4)
            GradientButton("Got it 😎", Modifier.fillMaxWidth(), onClick = onDismiss)
        }
    }
}
