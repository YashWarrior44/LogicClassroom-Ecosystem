package com.innovalogix.logicshare.ui

import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsPressedAsState
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.scale
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.rotate
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import androidx.compose.material3.Text
import com.innovalogix.logicshare.R
import com.innovalogix.logicshare.ui.theme.T
import com.innovalogix.logicshare.ui.theme.Type

// ── buttons ─────────────────────────────────────────────────────────────

/** Shrinks slightly under the finger, the way iOS controls do. */
@Composable
private fun pressScale(source: MutableInteractionSource): Float {
    val pressed by source.collectIsPressedAsState()
    val s by animateFloatAsState(if (pressed) 0.96f else 1f, tween(120), label = "press")
    return s
}

/** Windows' ACCENT_BTN_CSS: the accent gradient, white bold text. */
@Composable
fun GradientButton(text: String, modifier: Modifier = Modifier, enabled: Boolean = true,
                   onClick: () -> Unit) {
    val src = remember { MutableInteractionSource() }
    Box(
        modifier
            .scale(pressScale(src))
            .alpha(if (enabled) 1f else 0.42f)
            .clip(RoundedCornerShape(T.R_MD.dp))
            .background(T.accent)
            .border(1.dp, Color.White.copy(alpha = 0.22f), RoundedCornerShape(T.R_MD.dp))
            .clickable(src, null, enabled = enabled, onClick = onClick)
            .heightIn(min = 48.dp)
            .padding(horizontal = 18.dp, vertical = 12.dp),
        contentAlignment = Alignment.Center,
    ) { Text(text, style = Type.button, textAlign = TextAlign.Center, maxLines = 2) }
}

/** Windows' SUBTLE_BTN_CSS: quiet glass. */
@Composable
fun SubtleButton(text: String, modifier: Modifier = Modifier, enabled: Boolean = true,
                 onClick: () -> Unit) {
    val src = remember { MutableInteractionSource() }
    Box(
        modifier
            .scale(pressScale(src))
            .alpha(if (enabled) 1f else 0.42f)
            .clip(RoundedCornerShape(T.R_MD.dp))
            .background(T.FILL_HOVER)
            .border(1.dp, T.STROKE, RoundedCornerShape(T.R_MD.dp))
            .clickable(src, null, enabled = enabled, onClick = onClick)
            .heightIn(min = 48.dp)
            .padding(horizontal = 16.dp, vertical = 12.dp),
        contentAlignment = Alignment.Center,
    ) { Text(text, style = Type.button.copy(color = T.TEXT), textAlign = TextAlign.Center, maxLines = 2) }
}

@Composable
fun DangerButton(text: String, modifier: Modifier = Modifier, onClick: () -> Unit) {
    val src = remember { MutableInteractionSource() }
    Box(
        modifier
            .scale(pressScale(src))
            .clip(RoundedCornerShape(T.R_MD.dp))
            .background(T.DANGER_BG)
            .border(1.dp, T.DANGER_BD, RoundedCornerShape(T.R_MD.dp))
            .clickable(src, null, onClick = onClick)
            .heightIn(min = 44.dp)
            .padding(horizontal = 14.dp, vertical = 10.dp),
        contentAlignment = Alignment.Center,
    ) { Text(text, style = Type.button.copy(color = T.DANGER)) }
}

// ── surfaces ────────────────────────────────────────────────────────────

/** A raised glass card — Windows' SURF_CARD with its top-lit bevel. */
fun Modifier.glassCard(selected: Boolean = false, radius: Int = T.R_LG): Modifier {
    val shape = RoundedCornerShape(radius.dp)
    return this
        .clip(shape)
        .background(if (selected) T.cardSelected else T.card)
        .border(if (selected) 1.6.dp else 1.dp,
            if (selected) Brush.verticalGradient(listOf(T.ACCENT_HI, T.STROKE_ACC))
            else Brush.verticalGradient(listOf(T.STROKE_BRIGHT.copy(alpha = 0.8f), T.STROKE_SOFT)),
            shape)
}

@Composable
fun Hline(modifier: Modifier = Modifier) {
    Box(modifier.fillMaxWidth().height(1.dp).background(T.DIVIDER))
}

/** Windows' caption_label: small caps, wide tracking. */
@Composable
fun Caption(text: String, color: Color = T.TEXT_MUTED, modifier: Modifier = Modifier) {
    Text(text, style = Type.caption.copy(color = color), modifier = modifier)
}

@Composable
fun FieldLabel(text: String) { Caption(text, modifier = Modifier.padding(top = 6.dp, bottom = 2.dp)) }

@Composable
fun Logo(size: Dp, modifier: Modifier = Modifier) {
    Image(painterResource(R.drawable.ic_logo), contentDescription = "LogicShare",
        modifier = modifier.size(size))
}

/** Windows' AvatarCircle: palette gradient chosen from the initials, a soft
 *  top highlight, bold white initials. */
@Composable
fun Avatar(initials: String, size: Dp, modifier: Modifier = Modifier) {
    val ini = initials.ifBlank { "?" }.uppercase().take(3)
    val (c1, c2) = T.avatarPalette(ini)
    Box(modifier.size(size), contentAlignment = Alignment.Center) {
        Canvas(Modifier.matchParentSize()) {
            drawCircle(Brush.linearGradient(listOf(c1, c2), Offset.Zero, Offset(this.size.width, this.size.height)))
            drawCircle(Color.White.copy(alpha = 0.28f), style = Stroke(1.5.dp.toPx()))
            drawOval(Brush.verticalGradient(listOf(Color.White.copy(alpha = 0.2f), Color.Transparent),
                0f, this.size.height / 2),
                topLeft = Offset(3f, 3f),
                size = androidx.compose.ui.geometry.Size(this.size.width - 6f, (this.size.height - 6f) / 2))
        }
        Text(ini, color = Color.White, fontWeight = FontWeight.ExtraBold,
            fontSize = (size.value * 0.34f).sp, letterSpacing = (-0.3).sp)
    }
}

/** The pulsing status dot used on board cards and the status badge. */
@Composable
fun PulseDot(color: Color, modifier: Modifier = Modifier) {
    val t = rememberInfiniteTransition(label = "pulse")
    val p by t.animateFloat(0f, 1f, infiniteRepeatable(tween(1600, easing = LinearEasing)), label = "p")
    Canvas(modifier.size(14.dp)) {
        val r = size.minDimension / 2
        drawCircle(color.copy(alpha = (1f - p) * 0.45f), radius = r * (0.45f + 0.55f * p))
        drawCircle(color, radius = r * 0.42f)
    }
}

/** Windows' StatusBadge — same four states, same words. */
@Composable
fun StatusBadge(phase: Phase, disabled: Boolean) {
    val (fg, bg, bd, label) = when {
        disabled || phase == Phase.OFFLINE -> Quad(T.DANGER, T.DANGER_BG, T.DANGER_BD, "No Wi-Fi")
        phase == Phase.NO_BOARDS -> Quad(T.DANGER, T.DANGER_BG, T.DANGER_BD, "No Boards")
        phase == Phase.LIVE -> Quad(T.SUCCESS, T.SUCCESS_BG, T.SUCCESS_BD, "Ready!!")
        else -> Quad(T.WARNING, T.WARNING_BG, T.WARNING_BD, "Looking…")
    }
    Row(
        Modifier.clip(CircleShape).background(bg).border(1.dp, bd, CircleShape)
            .padding(start = 8.dp, end = 12.dp, top = 5.dp, bottom = 5.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        PulseDot(fg)
        Text(label, color = fg, fontWeight = FontWeight.ExtraBold, fontSize = 12.sp, letterSpacing = 0.4.sp)
    }
}

private data class Quad(val a: Color, val b: Color, val c: Color, val d: String)

/** A board's acknowledgment state as a coloured pill — Windows' ACK_STATE_STYLE. */
@Composable
fun StateChip(colour: Color, bg: Color, label: String, dot: Boolean = true) {
    Row(
        Modifier.clip(RoundedCornerShape(10.dp)).background(bg)
            .border(1.dp, colour, RoundedCornerShape(10.dp))
            .padding(horizontal = 9.dp, vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(5.dp),
    ) {
        if (dot) Box(Modifier.size(7.dp).clip(CircleShape).background(colour))
        Text(label, color = colour, fontWeight = FontWeight.ExtraBold, fontSize = 11.sp)
    }
}

// ── input ───────────────────────────────────────────────────────────────

@Composable
fun LsTextField(value: String, onChange: (String) -> Unit, placeholder: String,
                modifier: Modifier = Modifier, maxLength: Int = Int.MAX_VALUE,
                singleLine: Boolean = true, minHeight: Dp = 50.dp,
                caps: KeyboardCapitalization = KeyboardCapitalization.Sentences) {
    BasicTextField(
        value = value,
        onValueChange = { onChange(it.take(maxLength)) },
        singleLine = singleLine,
        textStyle = Type.bodyBold.copy(fontWeight = FontWeight.SemiBold),
        cursorBrush = SolidColor(T.ACCENT),
        keyboardOptions = KeyboardOptions(capitalization = caps),
        modifier = modifier,
        decorationBox = { inner ->
            Box(
                Modifier.fillMaxWidth().heightIn(min = minHeight)
                    .clip(RoundedCornerShape(T.R_MD.dp))
                    .background(T.FILL_HOVER)
                    .border(1.dp, if (value.isNotEmpty()) T.STROKE_ACC.copy(alpha = 0.6f) else T.STROKE,
                        RoundedCornerShape(T.R_MD.dp))
                    .padding(horizontal = 14.dp, vertical = 14.dp),
            ) {
                if (value.isEmpty()) Text(placeholder, style = Type.body.copy(color = T.TEXT_DIM))
                inner()
            }
        },
    )
}

/** iOS segmented control — for Sir / Ma'am. */
@Composable
fun Segmented(options: List<String>, selected: String, onSelect: (String) -> Unit) {
    Row(
        Modifier.clip(RoundedCornerShape(T.R_MD.dp)).background(T.FILL_HOVER)
            .border(1.dp, T.STROKE, RoundedCornerShape(T.R_MD.dp)).padding(3.dp),
    ) {
        options.forEach { o ->
            val on = o == selected
            Box(
                Modifier.clip(RoundedCornerShape((T.R_MD - 3).dp))
                    .background(if (on) T.accent else SolidColor(Color.Transparent))
                    .clickable { onSelect(o) }
                    .padding(horizontal = 26.dp, vertical = 10.dp),
                contentAlignment = Alignment.Center,
            ) { Text(o, style = Type.bodyBold.copy(color = if (on) Color.White else T.TEXT_SEC)) }
        }
    }
}

/** A gradient progress bar — Windows' QProgressBar chunk. */
@Composable
fun GradientProgress(fraction: Float, modifier: Modifier = Modifier) {
    val f by animateFloatAsState(fraction.coerceIn(0f, 1f), tween(160), label = "progress")
    Box(modifier.fillMaxWidth().height(10.dp).clip(CircleShape).background(T.FILL)
        .border(1.dp, T.STROKE, CircleShape)) {
        Box(Modifier.fillMaxWidth(f).height(10.dp).clip(CircleShape).background(T.accent))
    }
}

// ── dialogs ─────────────────────────────────────────────────────────────

/** A centred glass sheet. ``dismissable = false`` is Windows' "Escape is
 *  blocked while the transfer is running". */
@Composable
fun LsDialog(onDismiss: () -> Unit, dismissable: Boolean = true,
             content: @Composable () -> Unit) {
    Dialog(onDismissRequest = { if (dismissable) onDismiss() },
        properties = DialogProperties(dismissOnBackPress = dismissable,
            dismissOnClickOutside = dismissable, usePlatformDefaultWidth = false)) {
        Box(
            Modifier.padding(horizontal = 18.dp).fillMaxWidth()
                .glassCard(radius = T.R_XL)
                .background(Color(0xF00F1724))
                .padding(22.dp),
        ) { content() }
    }
}

/** Windows' CustomMessageBox: centred icon, title, message, optional
 *  detail, one OK button. */
@Composable
fun MessageDialog(m: Message, onDismiss: () -> Unit) {
    LsDialog(onDismiss) {
        Column(Modifier.fillMaxWidth(), horizontalAlignment = Alignment.CenterHorizontally) {
            Text(if (m.kind == Message.Kind.INFO) "ℹ️" else "⚠️", fontSize = 40.sp)
            Spacer(Modifier.height(10.dp))
            Text(m.title, style = Type.title2, textAlign = TextAlign.Center)
            Spacer(Modifier.height(8.dp))
            Text(m.text, style = Type.body.copy(color = T.TEXT), textAlign = TextAlign.Center)
            if (m.detail.isNotEmpty()) {
                Spacer(Modifier.height(6.dp))
                Text(m.detail, style = Type.footnote, textAlign = TextAlign.Center)
            }
            Spacer(Modifier.height(20.dp))
            GradientButton("OK", Modifier.fillMaxWidth(), onClick = onDismiss)
        }
    }
}

// ── the radar ───────────────────────────────────────────────────────────

/**
 * Windows' ScanningRadar, layer for layer and value for value:
 *   1. ring grid + crosshair
 *   2. three pulses born at the antenna, fading as they grow
 *   3. the sweep's TRAIL — a conical gradient fading out over 130° behind
 *      the line (the part the first port left out: it drew a 60° wedge with
 *      a flat edge, which read as a pie slice, not a radar)
 *   4. the sweep line as three stacked glows, with a glowing tip
 *   5. the antenna plate: a glow, a dark disc, a lit rim — 📡 on top
 * Speeds are Windows': 80°/s sweep, 0.42 pulse cycles/s, both linear and
 * time-based, so dropped frames never slow it down. Every size is scaled
 * from Windows' 260 px widget so the proportions match too.
 */
@Composable
fun ScanningRadar(size: Dp = 240.dp) {
    val t = rememberInfiniteTransition(label = "radar")
    val sweep by t.animateFloat(0f, 360f, infiniteRepeatable(tween(4500, easing = LinearEasing)), label = "sweep")
    val phase by t.animateFloat(0f, 1f, infiniteRepeatable(tween(2381, easing = LinearEasing),
        RepeatMode.Restart), label = "pulse")
    Box(Modifier.size(size), contentAlignment = Alignment.Center) {
        Canvas(Modifier.matchParentSize()) {
            val c = center
            val radius = this.size.minDimension / 2 - 8.dp.toPx()
            // Windows' constants are pixels on a ~122 px radius; k maps them.
            val k = radius / 122f
            fun rgba(r: Int, g: Int, b: Int, a: Int) = Color(r, g, b, a)

            // 1. rings + crosshair
            for ((m, a) in listOf(1f to 40, 0.66f to 30, 0.33f to 22))
                drawCircle(rgba(124, 208, 255, a), radius * m, c, style = Stroke(1.dp.toPx()))
            val cross = rgba(124, 208, 255, 18)
            drawLine(cross, Offset(c.x - radius, c.y), Offset(c.x + radius, c.y), 1.dp.toPx())
            drawLine(cross, Offset(c.x, c.y - radius), Offset(c.x, c.y + radius), 1.dp.toPx())

            // 2. pulses
            for (i in 0 until 3) {
                val p = (phase + i / 3f) % 1f
                val alpha = (150 * minOf(p / 0.10f, 1f) * maxOf(0f, 1f - p)).toInt()
                if (alpha > 0) drawCircle(rgba(140, 215, 255, alpha), radius * p, c, style = Stroke(1.8f * k))
            }

            // 3. trail. Compose's sweep gradient runs clockwise from 3
            // o'clock; rotated so the line sits at 0°, "behind the line" is
            // the END of the gradient. Windows' stops, mirrored to match.
            rotate(sweep, c) {
                drawCircle(
                    Brush.sweepGradient(
                        0.00f to rgba(76, 194, 255, 0),
                        0.64f to rgba(76, 194, 255, 0),
                        0.70f to rgba(80, 170, 230, 10),
                        0.80f to rgba(96, 180, 240, 32),
                        0.90f to rgba(110, 195, 250, 80),
                        0.96f to rgba(124, 208, 255, 135),
                        1.00f to rgba(140, 220, 255, 195),
                        center = c,
                    ),
                    radius, c,
                )
                // 4. the line — wide soft glow, mid glow, sharp core
                val end = Offset(c.x + radius, c.y)
                for ((w, col) in listOf(7.5f to rgba(180, 230, 255, 55), 3.8f to rgba(200, 235, 255, 115),
                                        1.6f to rgba(235, 248, 255, 235)))
                    drawLine(col, c, end, w * k, StrokeCap.Round)
                drawCircle(Brush.radialGradient(
                    0f to rgba(220, 240, 255, 230), 0.5f to rgba(140, 215, 255, 120),
                    1f to rgba(140, 215, 255, 0), center = end, radius = 9f * k), 10f * k, end)
            }

            // 5. antenna plate
            val plate = 34f * k
            drawCircle(Brush.radialGradient(
                0f to rgba(76, 194, 255, 130), 0.6f to rgba(76, 194, 255, 55), 1f to rgba(76, 194, 255, 0),
                center = c, radius = plate + 8f * k), plate + 8f * k, c)
            drawCircle(Brush.radialGradient(listOf(rgba(28, 38, 56, 245), rgba(16, 22, 34, 230)),
                center = c, radius = plate), plate, c)
            drawCircle(rgba(140, 215, 255, 170), plate, c, style = Stroke(1.4f * k))
        }
        Text("📡", fontSize = 30.sp)
    }
}

@Composable
fun Gap(h: Int) = Spacer(Modifier.height(h.dp))

@Composable
fun HGap(w: Int) = Spacer(Modifier.width(w.dp))
