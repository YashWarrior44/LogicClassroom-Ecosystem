package com.innovalogix.logicshare.ui

import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.spring
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInVertically
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.isImeVisible
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.scale
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsPressedAsState
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.graphics.BlendMode
import androidx.compose.ui.graphics.CompositingStrategy
import androidx.compose.ui.graphics.drawscope.rotate
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.platform.LocalSoftwareKeyboardController
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.foundation.focusable
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.innovalogix.logicshare.core.Profile
import com.innovalogix.logicshare.core.formatClass
import com.innovalogix.logicshare.net.PickedFile
import com.innovalogix.logicshare.ui.theme.T
import com.innovalogix.logicshare.ui.theme.Type

data class MainActions(
    val onRefresh: () -> Unit,
    val onCompose: () -> Unit,
    val onSendFile: (Board) -> Unit,
    val onSendAnnouncement: (Board) -> Unit,
    val onSendShared: (Board) -> Unit,
    val onCancelShared: () -> Unit,
    val onToggle: (String) -> Unit,
    val onCancelSelection: () -> Unit,
    val onSendSelected: () -> Unit,
    val onEditProfile: () -> Unit,
    val onHistory: () -> Unit,
    val onAbout: () -> Unit,
    val onHelp: () -> Unit,
    val onCheckUpdates: () -> Unit,
    val onSearchOpen: (Boolean) -> Unit = {},
    val onQuery: (String) -> Unit = {},
    val onClassFilter: (String?) -> Unit = {},
    val onSectionFilter: (String?) -> Unit = {},
    val onClearFilters: () -> Unit = {},
    val onSelectAll: (List<String>) -> Unit = {},
)

@Composable
fun MainScreen(state: MainState, profile: Profile, shared: List<PickedFile>, filter: BoardFilter,
               a: MainActions) {
    val visible = applyBoardFilter(state.boards, filter)
    Column(Modifier.fillMaxSize().background(T.backdrop)) {
        Header(state, profile, filter, a)
        AnimatedVisibility(shared.isNotEmpty()) { SharedBanner(shared, a.onCancelShared) }
        // One caption, not two side by side: at a large system font size the
        // pair wrapped "SMART BOARDS NEARBY" onto two lines (seen on a
        // Galaxy M21). While filtering, the count replaces the title.
        if (filter.active && state.boards.isNotEmpty())
            Caption("SHOWING ${visible.size} OF ${state.boards.size} BOARDS", color = T.ACCENT_HI,
                modifier = Modifier.padding(start = 22.dp, top = 14.dp, bottom = 6.dp))
        else
            Caption("SMART BOARDS NEARBY", modifier = Modifier.padding(start = 22.dp, top = 14.dp, bottom = 6.dp))
        if (filter.filtering) ActiveFilters(filter, a)
        Box(Modifier.weight(1f).fillMaxWidth()) {
            BoardList(state, visible, filter, shared.isNotEmpty(), a)
        }
        BottomBar(state, visible, a)
    }
}

@Composable
private fun ActiveFilters(f: BoardFilter, a: MainActions) {
    Row(Modifier.fillMaxWidth().padding(horizontal = 16.dp).padding(bottom = 8.dp),
        horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
        f.cls?.let { FilterChip("Class $it") { a.onClassFilter(null) } }
        f.section?.let { FilterChip("Section ${it.uppercase()}") { a.onSectionFilter(null) } }
        Spacer(Modifier.weight(1f))
        Text("Clear", style = Type.footnote.copy(color = T.ACCENT_HI, fontWeight = FontWeight.Bold),
            modifier = Modifier.clip(RoundedCornerShape(8.dp)).clickable(onClick = a.onClearFilters).padding(6.dp))
    }
}

@Composable
private fun FilterChip(label: String, onRemove: () -> Unit) {
    Row(Modifier.clip(CircleShape).background(T.accent).clickable(onClick = onRemove)
        .padding(start = 12.dp, end = 10.dp, top = 6.dp, bottom = 6.dp),
        verticalAlignment = Alignment.CenterVertically) {
        Text(label, style = Type.footnote.copy(color = Color.White, fontWeight = FontWeight.ExtraBold))
        Text("  \u2715", style = Type.footnote.copy(color = Color.White, fontWeight = FontWeight.ExtraBold))
    }
}

// ── header ──────────────────────────────────────────────────────────────

@Composable
private fun Header(state: MainState, profile: Profile, filter: BoardFilter, a: MainActions) {
    Column(
        Modifier.fillMaxWidth().background(T.bar)
            .statusBarsPadding()
            .padding(start = 16.dp, end = 16.dp, top = 10.dp, bottom = 12.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Logo(40.dp)
            Spacer(Modifier.width(11.dp))
            Column(Modifier.weight(1f)) {
                Text("LogicShare", style = Type.title.copy(fontSize = 22.sp), maxLines = 1)
                Text(profile.displayName, style = Type.footnote, maxLines = 1,
                    overflow = TextOverflow.Ellipsis)
            }
            StatusBadge(state.phase, state.disabled)
            Spacer(Modifier.width(8.dp))
            SettingsMenu(a)
        }
        Row(Modifier.fillMaxWidth().padding(top = 12.dp), verticalAlignment = Alignment.CenterVertically) {
            SearchField(filter, a, Modifier.weight(1f))
            Spacer(Modifier.width(8.dp))
            FilterMenu(state, filter, a)
        }
    }
    Box(Modifier.fillMaxWidth().size(1.dp).background(T.DIVIDER))
}

private val CONTROL_H = 42.dp
private val CONTROL_SHAPE = RoundedCornerShape(12.dp)

/**
 * The search bar, always on screen under the title \u2014 the iOS pattern. "Open"
 * in the view model now means "being used": focusing it opens the search,
 * and Back (or clearing it and leaving) closes it, which clears the query.
 */
@OptIn(androidx.compose.foundation.layout.ExperimentalLayoutApi::class)
@Composable
private fun SearchField(f: BoardFilter, a: MainActions, modifier: Modifier) {
    // Dropping focus means handing it to this sink. clearFocus() alone does
    // not stick: Android re-focuses the Compose view, which gives focus
    // straight back to the first text field — this one (Galaxy M21).
    val sink = remember { FocusRequester() }
    val keyboard = LocalSoftwareKeyboardController.current
    val release = { keyboard?.hide(); runCatching { sink.requestFocus() }; Unit }
    var focused by remember { mutableStateOf(false) }
    val query by rememberUpdatedState(f.query)
    // Back closes the search: drop the keyboard and the caret with it.
    LaunchedEffect(f.searchOpen) { if (!f.searchOpen) release() }
    // Keyboard put away (Back, or its own hide key): the caret goes with it.
    // The query and its results stay; the next Back clears them.
    val ime = WindowInsets.isImeVisible
    var imeWasUp by remember { mutableStateOf(false) }
    LaunchedEffect(ime) {
        if (ime) imeWasUp = true
        else if (imeWasUp) { imeWasUp = false; if (focused) release() }
    }
    Box(modifier) {
    Box(Modifier.size(1.dp).focusRequester(sink).focusable())
    BasicTextField(
        value = f.query,
        onValueChange = { a.onQuery(it.take(80)) },
        singleLine = true,
        textStyle = Type.bodyBold.copy(fontWeight = FontWeight.SemiBold),
        cursorBrush = androidx.compose.ui.graphics.SolidColor(T.ACCENT),
        keyboardOptions = KeyboardOptions(capitalization = KeyboardCapitalization.None,
            autoCorrectEnabled = false, imeAction = ImeAction.Search),
        keyboardActions = KeyboardActions(onSearch = { release() }),
        modifier = Modifier.fillMaxWidth().onFocusChanged {
            if (it.isFocused != focused) {
                focused = it.isFocused
                if (it.isFocused) a.onSearchOpen(true)
                else if (query.isBlank()) a.onSearchOpen(false)
            }
        },
        decorationBox = { inner ->
            Row(
                Modifier.fillMaxWidth().height(CONTROL_H).clip(CONTROL_SHAPE)
                    .background(T.FILL_HOVER)
                    .border(1.dp, if (focused) T.STROKE_ACC else T.STROKE, CONTROL_SHAPE)
                    .padding(start = 12.dp, end = 6.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                SearchGlyph(if (focused) T.ACCENT_HI else T.TEXT_MUTED, Modifier.size(17.dp))
                Spacer(Modifier.width(9.dp))
                Box(Modifier.weight(1f), contentAlignment = Alignment.CenterStart) {
                    if (f.query.isEmpty())
                        Text("Search class, section or room", style = Type.body.copy(color = T.TEXT_DIM),
                            maxLines = 1, overflow = TextOverflow.Ellipsis)
                    inner()
                }
                if (f.query.isNotEmpty()) {
                    Box(
                        Modifier.size(30.dp).clip(CircleShape).clickable { a.onQuery("") },
                        contentAlignment = Alignment.Center,
                    ) {
                        // Drawn, not a \u2715 character: the font's glyph sat low
                        // in the circle.
                        Canvas(Modifier.size(18.dp).semantics { contentDescription = "Clear search" }) {
                            drawCircle(T.TEXT_DIM)
                            val a = size.width * 0.33f; val b = size.width * 0.67f
                            val st = 1.6.dp.toPx()
                            drawLine(T.BAR_BOTTOM, Offset(a, a), Offset(b, b), st, StrokeCap.Round)
                            drawLine(T.BAR_BOTTOM, Offset(a, b), Offset(b, a), st, StrokeCap.Round)
                        }
                    }
                }
            }
        },
    )
    }
}

/** A square glass button holding a drawn icon \u2014 the filter and settings
 *  buttons. ``active`` fills it with the accent gradient. */
@Composable
private fun IconSquare(active: Boolean, label: String, onClick: () -> Unit,
                       icon: @Composable (Color) -> Unit) {
    Box(
        Modifier.size(CONTROL_H).clip(CONTROL_SHAPE)
            .background(if (active) T.accent else androidx.compose.ui.graphics.SolidColor(T.FILL_HOVER))
            .border(1.dp, if (active) Color.White.copy(alpha = 0.25f) else T.STROKE, CONTROL_SHAPE)
            .clickable(onClickLabel = label, onClick = onClick)
            .semantics { contentDescription = label },
        contentAlignment = Alignment.Center,
    ) { icon(if (active) Color.White else T.TEXT_SEC) }
}

@Composable
private fun SearchGlyph(color: Color, modifier: Modifier) {
    Canvas(modifier) {
        val w = size.width
        val stroke = 2.1.dp.toPx()
        drawCircle(color, radius = w * 0.31f, center = Offset(w * 0.43f, w * 0.43f), style = Stroke(stroke))
        drawLine(color, Offset(w * 0.66f, w * 0.66f), Offset(w * 0.92f, w * 0.92f), stroke, StrokeCap.Round)
    }
}

/** Three sliders \u2014 the usual "filter" mark. */
@Composable
private fun FilterGlyph(color: Color, modifier: Modifier) {
    Canvas(modifier.graphicsLayer(compositingStrategy = CompositingStrategy.Offscreen)) {
        val w = size.width
        val stroke = 1.6.dp.toPx()
        val r = 1.9.dp.toPx()
        for ((y, x) in listOf(0.18f to 0.7f, 0.5f to 0.3f, 0.82f to 0.62f)) {
            drawLine(color, Offset(w * 0.04f, w * y), Offset(w * 0.96f, w * y), stroke, StrokeCap.Round)
            // Punch the track out under the knob so the knob reads as a ring
            // on any background, the accent gradient included.
            drawCircle(Color.Black, r + stroke, Offset(w * x, w * y), blendMode = BlendMode.Clear)
            drawCircle(color, r, Offset(w * x, w * y), style = Stroke(stroke))
        }
    }
}

/** A gear: eight teeth round a ring. */
@Composable
private fun GearGlyph(color: Color, modifier: Modifier) {
    Canvas(modifier.graphicsLayer(compositingStrategy = CompositingStrategy.Offscreen)) {
        val w = size.width
        val c = center
        for (i in 0 until 8) rotate(i * 45f, c) {
            drawRoundRect(color, Offset(c.x - w * 0.11f, c.y - w * 0.5f),
                androidx.compose.ui.geometry.Size(w * 0.22f, w * 0.3f),
                androidx.compose.ui.geometry.CornerRadius(w * 0.05f))
        }
        drawCircle(color, w * 0.34f, c)
        drawCircle(Color.Black, w * 0.14f, c, blendMode = BlendMode.Clear)
    }
}

/** Filter by class / section: one choice each, the same as Windows. */
@Composable
private fun FilterMenu(state: MainState, f: BoardFilter, a: MainActions) {
    var open by remember { mutableStateOf(false) }
    Box {
        IconSquare(f.filtering, "Filter", { open = true }) { FilterGlyph(it, Modifier.size(20.dp)) }
        // How many filters are on, as a badge \u2014 the button alone only says "some".
        val n = listOfNotNull(f.cls, f.section).size
        if (n > 0) Box(
            Modifier.align(Alignment.TopEnd).offset(x = 5.dp, y = (-5).dp).size(18.dp)
                .clip(CircleShape).background(Color.White).border(1.5.dp, T.ACCENT, CircleShape),
            contentAlignment = Alignment.Center,
        ) {
            // Trimmed line box, or the font's padding sits the digit low.
            Text("$n", style = androidx.compose.ui.text.TextStyle(
                color = T.ACCENT, fontSize = 10.sp, fontWeight = FontWeight.Black, lineHeight = 10.sp,
                platformStyle = androidx.compose.ui.text.PlatformTextStyle(includeFontPadding = false),
                lineHeightStyle = androidx.compose.ui.text.style.LineHeightStyle(
                    androidx.compose.ui.text.style.LineHeightStyle.Alignment.Center,
                    androidx.compose.ui.text.style.LineHeightStyle.Trim.Both)))
        }
        DropdownMenu(open, { open = false }, shape = RoundedCornerShape(T.R_LG.dp),
            containerColor = Color(0xF2141D2B),
            modifier = Modifier.border(1.dp, T.STROKE, RoundedCornerShape(T.R_LG.dp))) {
            val classes = filterValues(state.boards.map { it.cls })
            val sections = filterValues(state.boards.map { it.section })
            if (classes.isEmpty() && sections.isEmpty()) {
                MenuCaption("NOTHING TO FILTER YET")
                Text("Filters appear once boards are found.", style = Type.footnote,
                    modifier = Modifier.padding(horizontal = 18.dp, vertical = 8.dp))
            }
            if (classes.isNotEmpty()) {
                MenuCaption("CLASS")
                MenuItem(if (f.cls == null) "\u2713" else "", "All classes") { open = false; a.onClassFilter(null) }
                classes.forEach { c ->
                    MenuItem(if (f.cls == c) "\u2713" else "", "Class $c") { open = false; a.onClassFilter(c) }
                }
            }
            if (sections.isNotEmpty()) {
                HorizontalDivider(color = T.DIVIDER, modifier = Modifier.padding(horizontal = 14.dp, vertical = 4.dp))
                MenuCaption("SECTION")
                MenuItem(if (f.section == null) "\u2713" else "", "All sections") { open = false; a.onSectionFilter(null) }
                sections.forEach { s ->
                    MenuItem(if (f.section.equals(s, true)) "\u2713" else "", "Section ${s.uppercase()}") {
                        open = false; a.onSectionFilter(s) }
                }
            }
        }
    }
}

/** Windows' SettingsButton popup: PROFILE / ANNOUNCEMENTS / ABOUT. */
@Composable
private fun SettingsMenu(a: MainActions) {
    var open by remember { mutableStateOf(false) }
    Box {
        IconSquare(false, "Settings", { open = true }) { GearGlyph(it, Modifier.size(19.dp)) }
        DropdownMenu(open, { open = false },
            shape = RoundedCornerShape(T.R_LG.dp),
            containerColor = Color(0xF2141D2B),
            modifier = Modifier.border(1.dp, T.STROKE, RoundedCornerShape(T.R_LG.dp))) {
            MenuCaption("PROFILE")
            MenuItem("👤", "Edit Profile") { open = false; a.onEditProfile() }
            HorizontalDivider(color = T.DIVIDER, modifier = Modifier.padding(horizontal = 14.dp, vertical = 4.dp))
            MenuCaption("ANNOUNCEMENTS")
            MenuItem("🕘", "History & Reports") { open = false; a.onHistory() }
            HorizontalDivider(color = T.DIVIDER, modifier = Modifier.padding(horizontal = 14.dp, vertical = 4.dp))
            MenuCaption("ABOUT")
            MenuItem("ℹ️", "About LogicShare") { open = false; a.onAbout() }
            MenuItem("🛠", "Help & Tips") { open = false; a.onHelp() }
            MenuItem("🔄", "Check for Updates") { open = false; a.onCheckUpdates() }
        }
    }
}

@Composable
private fun MenuCaption(text: String) {
    Caption(text, modifier = Modifier.padding(start = 18.dp, end = 18.dp, top = 8.dp, bottom = 2.dp))
}

@Composable
private fun MenuItem(icon: String, label: String, onClick: () -> Unit) {
    DropdownMenuItem(
        text = {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(icon, fontSize = 17.sp, modifier = Modifier.width(30.dp))
                Text(label, style = Type.bodyBold)
            }
        },
        onClick = onClick,
    )
}

// ── share banner ────────────────────────────────────────────────────────

@Composable
private fun SharedBanner(files: List<PickedFile>, onCancel: () -> Unit) {
    Row(
        Modifier.padding(start = 16.dp, end = 16.dp, top = 12.dp).fillMaxWidth()
            .glassCard(selected = true, radius = T.R_MD).padding(14.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text("📎", fontSize = 22.sp)
        Spacer(Modifier.width(10.dp))
        Column(Modifier.weight(1f)) {
            Text(if (files.size == 1) files[0].name else "${files.size} files ready to send",
                style = Type.bodyBold, maxLines = 1, overflow = TextOverflow.Ellipsis)
            Text("Tap Send on the board it should go to", style = Type.footnote)
        }
        Text("Cancel", style = Type.bodyBold.copy(color = T.ACCENT_HI),
            modifier = Modifier.clip(RoundedCornerShape(8.dp)).clickable(onClick = onCancel).padding(8.dp))
    }
}

// ── the list ────────────────────────────────────────────────────────────

@Composable
private fun BoardList(state: MainState, visible: List<Board>, filter: BoardFilter,
                      sharing: Boolean, a: MainActions) {
    val mode = when {
        state.boards.isNotEmpty() && visible.isEmpty() -> "nomatch"
        state.boards.isNotEmpty() -> "cards"
        state.disabled || state.phase == Phase.OFFLINE -> "offline"
        state.phase == Phase.NO_BOARDS -> "none"
        else -> "scanning"
    }
    AnimatedContent(mode, transitionSpec = { fadeIn() togetherWith fadeOut() }, label = "list") { m ->
        when (m) {
            "cards" -> LazyColumn(
                Modifier.fillMaxSize(),
                contentPadding = PaddingValues(start = 16.dp, end = 16.dp, top = 4.dp, bottom = 16.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                items(visible, key = { it.ip }) { b ->
                    BoardCard(b, state, sharing, a, Modifier.animateItem())
                }
            }
            "nomatch" -> NoMatchPanel(filter, a)
            "offline" -> OfflinePanel(state.disabled)
            "none" -> NoBoardPanel(a.onHelp)
            else -> ScanningPanel()
        }
    }
}

/** Windows' BoardCard. */
@Composable
private fun BoardCard(b: Board, state: MainState, sharing: Boolean, a: MainActions, modifier: Modifier) {
    val selecting = state.selectMode
    val selected = b.ip in state.selected
    var menu by remember { mutableStateOf(false) }
    Row(
        modifier.fillMaxWidth()
            .glassCard(selected = selecting && selected)
            .clickable(enabled = selecting) { a.onToggle(b.ip) }
            .padding(start = 16.dp, end = 10.dp, top = 14.dp, bottom = 14.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        PulseDot(T.SUCCESS)
        Spacer(Modifier.width(10.dp))
        Box(
            Modifier.size(50.dp).clip(RoundedCornerShape(T.R_MD.dp))
                .background(Brush.linearGradient(listOf(T.ACCENT_HI.copy(alpha = 0.38f), T.ACCENT.copy(alpha = 0.12f))))
                .border(1.dp, T.STROKE_BRIGHT, RoundedCornerShape(T.R_MD.dp)),
            contentAlignment = Alignment.Center,
        ) { Text("🖥", fontSize = 24.sp) }
        Spacer(Modifier.width(14.dp))
        Column(Modifier.weight(1f)) {
            // ONE line, always. It used to wrap to two at large system font
            // sizes ("Class 11-A · Room 12"), which made some cards taller
            // than others. Now it starts a touch smaller and shrinks further
            // only as far as a long name needs; the ellipsis is a last resort
            // for a name that will not fit even at the minimum.
            // A tighter separator than the "  ·  " used elsewhere: on a
            // phone-width card those two spaces each side were what pushed a
            // long room name into the ellipsis.
            androidx.compose.foundation.text.BasicText(
                "${formatClass(b.cls, b.section)} · Room ${b.room}",
                style = Type.headline.copy(fontSize = 16.sp),
                maxLines = 1,
                softWrap = false,
                overflow = TextOverflow.Ellipsis,
                autoSize = androidx.compose.foundation.text.TextAutoSize.StepBased(
                    minFontSize = 9.5.sp, maxFontSize = 16.sp, stepSize = 0.5.sp),
            )
            Spacer(Modifier.size(3.dp))
            Text(b.ip, style = Type.mono, maxLines = 1, softWrap = false)
        }
        when {
            selecting -> CheckBadge(selected)
            sharing -> GradientButton("Send", Modifier.padding(start = 6.dp)) { a.onSendShared(b) }
            else -> Box {
                val busy = b.ip in state.checking
                Box(
                    Modifier.size(44.dp).clip(RoundedCornerShape(12.dp)).background(T.FILL)
                        .border(1.dp, T.STROKE, RoundedCornerShape(12.dp))
                        .clickable(enabled = !busy) { menu = true },
                    contentAlignment = Alignment.Center,
                ) { Text(if (busy) "…" else "⋯", fontSize = 22.sp, fontWeight = FontWeight.ExtraBold, color = T.TEXT) }
                DropdownMenu(menu, { menu = false },
                    shape = RoundedCornerShape(T.R_LG.dp), containerColor = Color(0xF2141D2B),
                    modifier = Modifier.border(1.dp, T.STROKE, RoundedCornerShape(T.R_LG.dp))) {
                    MenuCaption("ACTIONS")
                    MenuItem("📤", "Send File") { menu = false; a.onSendFile(b) }
                    MenuItem("📢", "Send Announcement") { menu = false; a.onSendAnnouncement(b) }
                }
            }
        }
    }
}

/** The animated selection check — Windows' paintEvent badge. */
@Composable
private fun CheckBadge(selected: Boolean) {
    val s by animateFloatAsState(if (selected) 1f else 0f, spring(dampingRatio = 0.55f), label = "check")
    Box(Modifier.padding(end = 6.dp).size(30.dp), contentAlignment = Alignment.Center) {
        Box(Modifier.size(26.dp).clip(CircleShape).border(1.5.dp, T.STROKE_BRIGHT, CircleShape))
        if (s > 0.01f) Canvas(Modifier.size(28.dp).scale(s)) {
            drawCircle(Brush.linearGradient(listOf(Color(124, 208, 255), Color(37, 150, 227)),
                Offset.Zero, Offset(size.width, size.height)))
            drawCircle(Color.White.copy(alpha = 0.7f), style = Stroke(1.2.dp.toPx()))
            if (s > 0.45f) {
                val w = 2.4.dp.toPx()
                val cx = size.width / 2; val cy = size.height / 2; val u = size.width / 26f
                drawLine(Color.White, Offset(cx - 5 * u, cy + 0.5f * u), Offset(cx - 1.5f * u, cy + 4 * u), w, StrokeCap.Round)
                drawLine(Color.White, Offset(cx - 1.5f * u, cy + 4 * u), Offset(cx + 5 * u, cy - 4 * u), w, StrokeCap.Round)
            }
        }
    }
}

// ── panels ──────────────────────────────────────────────────────────────

@Composable
private fun ScanningPanel() {
    Column(Modifier.fillMaxSize().padding(24.dp), horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center) {
        ScanningRadar()
        Gap(18)
        Text("Looking for Smart Boards…📡", style = Type.title2, textAlign = TextAlign.Center)
        Gap(6)
        Text("Make sure your board and phone are on the same Wi-Fi 😎", style = Type.callout,
            textAlign = TextAlign.Center)
    }
}

@Composable
private fun NoBoardPanel(onHelp: () -> Unit) {
    Column(Modifier.fillMaxSize().padding(horizontal = 16.dp, vertical = 8.dp)) {
        Column(
            Modifier.fillMaxWidth().glassCard().padding(22.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("📡", fontSize = 32.sp)
                Spacer(Modifier.width(12.dp))
                Column {
                    Text("No Smart Boards yet…💀", style = Type.title2)
                    Text("Let's get things connected 😎", style = Type.footnote)
                }
            }
            Hline()
            Caption("QUICK CHECKS")
            for ((em, txt) in listOf(
                "🔌" to "LogicBoard is running on the Smart Board",
                "📶" to "The Smart Board is on Wi-Fi",
                "📱" to "Your phone is on the same Wi-Fi",
            )) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(em, fontSize = 17.sp, modifier = Modifier.width(32.dp))
                    Text(txt, style = Type.callout.copy(fontWeight = FontWeight.Bold))
                }
            }
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                SubtleButton("Need help?🛠", onClick = onHelp)
            }
        }
    }
}

@Composable
private fun NoMatchPanel(f: BoardFilter, a: MainActions) {
    Column(Modifier.fillMaxSize().padding(32.dp), horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center) {
        Text("\uD83D\uDD0D", fontSize = 52.sp)
        Gap(12)
        Text(if (f.query.isNotBlank()) "No boards match \u201C${f.query.trim()}\u201D"
             else "No boards match these filters",
            style = Type.title2, textAlign = TextAlign.Center)
        Gap(6)
        Text("Try \u201C11a\u201D, \u201CXI A\u201D, \u201Croom 12\u201D or part of an IP address.",
            style = Type.callout, textAlign = TextAlign.Center)
        Gap(16)
        SubtleButton("Clear search & filters") { a.onClearFilters(); a.onQuery("") }
    }
}

/** Windows shows an empty list under a red "No Wi-Fi" badge. A phone screen
 *  that is simply blank reads as broken, so this says what the badge says. */
@Composable
private fun OfflinePanel(disabled: Boolean) {
    Column(Modifier.fillMaxSize().padding(32.dp), horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center) {
        Text(if (disabled) "⚠️" else "📶", fontSize = 56.sp)
        Gap(14)
        Text(if (disabled) "LogicShare is temporarily disabled" else "No Wi-Fi 💀",
            style = Type.title2, textAlign = TextAlign.Center)
        Gap(6)
        Text(if (disabled) "Device discovery could not start. Restart LogicShare to try again."
             else "Connect to the same Wi-Fi as the Smart Board — LogicShare will pick it up instantly 😎",
            style = Type.callout, textAlign = TextAlign.Center)
    }
}

// ── bottom bar ──────────────────────────────────────────────────────────

/** Windows' toolbar, and the selection bar that replaces it in place. */
@Composable
private fun BottomBar(state: MainState, visible: List<Board>, a: MainActions) {
    Box(Modifier.fillMaxWidth().size(1.dp).background(if (state.selectMode) T.STROKE_ACC else T.DIVIDER))
    AnimatedContent(state.selectMode,
        transitionSpec = { (fadeIn() + slideInVertically { it / 3 }) togetherWith fadeOut() },
        label = "toolbar") { selecting ->
        Column(
            Modifier.fillMaxWidth().background(T.bar).navigationBarsPadding()
                .padding(start = 16.dp, end = 16.dp, top = 9.dp, bottom = 10.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            if (selecting) {
                val n = state.selected.size
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text("$n board${if (n != 1) "s" else ""} selected",
                        style = Type.footnote.copy(color = T.ACCENT_HI, fontWeight = FontWeight.ExtraBold),
                        modifier = Modifier.weight(1f))
                    val unselected = visible.map { it.ip }.filter { it !in state.selected }
                    if (unselected.isNotEmpty())
                        Text(if (visible.size == state.boards.size) "Select all" else "Select all shown",
                            style = Type.footnote.copy(color = T.ACCENT_HI, fontWeight = FontWeight.ExtraBold),
                            modifier = Modifier.clip(RoundedCornerShape(8.dp))
                                .clickable { a.onSelectAll(visible.map { it.ip }) }
                                .padding(horizontal = 6.dp, vertical = 2.dp))
                }
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    BarButton("Cancel", primary = false, Modifier.weight(0.38f), onClick = a.onCancelSelection)
                    BarButton("🚀  Send Announcement", primary = true, Modifier.weight(0.62f), enabled = n > 0,
                        onClick = a.onSendSelected)
                }
            } else {
                Text(state.countLabel, style = Type.footnote.copy(color = T.TEXT_SEC, fontWeight = FontWeight.Bold),
                    maxLines = 1, overflow = TextOverflow.Ellipsis)
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    BarButton("📢  Create Announcement", primary = true, Modifier.weight(1f), onClick = a.onCompose)
                    BarButton("🔄  Refresh", primary = false, onClick = a.onRefresh)
                }
            }
        }
    }
}

/** The toolbar's buttons: one line, 44 dp — a toolbar, not a hero. */
@Composable
private fun BarButton(text: String, primary: Boolean, modifier: Modifier = Modifier,
                      enabled: Boolean = true, onClick: () -> Unit) {
    val src = remember { MutableInteractionSource() }
    val pressed by src.collectIsPressedAsState()
    val s by animateFloatAsState(if (pressed) 0.97f else 1f, label = "press")
    Box(
        modifier.scale(s).alpha(if (enabled) 1f else 0.42f)
            .height(44.dp).clip(CONTROL_SHAPE)
            .background(if (primary) T.accent else androidx.compose.ui.graphics.SolidColor(T.FILL_HOVER))
            .border(1.dp, if (primary) Color.White.copy(alpha = 0.22f) else T.STROKE, CONTROL_SHAPE)
            .clickable(src, null, enabled = enabled, onClick = onClick)
            .padding(horizontal = 16.dp),
        contentAlignment = Alignment.Center,
    ) {
        Text(text, style = Type.button.copy(fontSize = 14.sp, color = if (primary) Color.White else T.TEXT),
            maxLines = 1, softWrap = false, overflow = TextOverflow.Ellipsis)
    }
}
