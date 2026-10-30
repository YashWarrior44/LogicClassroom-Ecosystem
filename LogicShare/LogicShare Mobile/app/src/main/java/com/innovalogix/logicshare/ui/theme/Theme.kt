package com.innovalogix.logicshare.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp

/**
 * LogicShare Windows' design tokens (class T), carried over value for value,
 * so the two apps read as one product. The Windows UI is dark glass over an
 * acrylic backdrop; here the backdrop is a deep gradient and the surfaces
 * sit on it the same way.
 */
object T {
    val TEXT = Color(0xFFF5F8FC)
    val TEXT_SEC = Color(0xFFBFCAD8)
    val TEXT_MUTED = Color(0xFF92A0B0)
    val TEXT_DIM = Color(0xFF5E6A78)

    val ACCENT = Color(0xFF4CC2FF)
    val ACCENT_HI = Color(0xFF7CD0FF)
    val ACCENT_LO = Color(0xFF2596E3)

    val SUCCESS = Color(0xFF6CCF80)
    val WARNING = Color(0xFFF0C14B)
    val DANGER = Color(0xFFFF6B6B)
    val SUCCESS_BG = Color(108, 207, 128, 40)
    val WARNING_BG = Color(240, 193, 75, 40)
    val DANGER_BG = Color(255, 107, 107, 40)
    val SUCCESS_BD = Color(108, 207, 128, 120)
    val WARNING_BD = Color(240, 193, 75, 120)
    val DANGER_BD = Color(255, 107, 107, 120)

    val STROKE_SOFT = Color(0xFF2B3138)
    val STROKE = Color(0xFF3A3F46)
    val STROKE_HI = Color(0xFF555A5F)
    val STROKE_BRIGHT = Color(0xFF696D72)
    val STROKE_ACC = Color(0xFF62A2C7)
    val DIVIDER = Color(0xFF2D333A)

    val FILL_SUBTLE = Color(255, 255, 255, 12)
    val FILL = Color(255, 255, 255, 16)
    val FILL_HOVER = Color(255, 255, 255, 26)
    val FILL_ACTIVE = Color(255, 255, 255, 38)

    // Surfaces (top, bottom) — Windows' SURF_CARD / SURF_TILE / SURF_BAR.
    val CARD_TOP = Color(38, 48, 66, 224)
    val CARD_BOTTOM = Color(24, 32, 46, 206)
    val CARD_SEL_TOP = Color(40, 70, 100, 175)
    val CARD_SEL_BOTTOM = Color(26, 46, 70, 165)
    val TILE = Color(27, 33, 42, 200)
    val BAR_TOP = Color(24, 31, 43, 236)
    val BAR_BOTTOM = Color(17, 22, 32, 240)

    /** The window backdrop: Windows' acrylic tint (24, 28, 38) deepened to a
     *  gradient, with the accent bleeding in from the top. */
    val backdrop = Brush.verticalGradient(
        0f to Color(0xFF14233A), 0.35f to Color(0xFF0D1522), 1f to Color(0xFF080C14))

    val accent = Brush.horizontalGradient(listOf(ACCENT_LO, ACCENT_HI))
    val accentDiagonal = Brush.linearGradient(listOf(ACCENT_HI, ACCENT_LO))
    val card = Brush.verticalGradient(listOf(CARD_TOP, CARD_BOTTOM))
    val cardSelected = Brush.verticalGradient(listOf(CARD_SEL_TOP, CARD_SEL_BOTTOM))
    val bar = Brush.verticalGradient(listOf(BAR_TOP, BAR_BOTTOM))

    /** Windows' AVATAR_PALETTES, in the same order, picked the same way. */
    val AVATARS = listOf(
        Color(0xFF7CD0FF) to Color(0xFF2596E3),
        Color(0xFFF56DE2) to Color(0xFF8B3FD3),
        Color(0xFF6CCF80) to Color(0xFF2E8A4A),
        Color(0xFFF0C14B) to Color(0xFFD28722),
        Color(0xFFFF8A80) to Color(0xFFD04848),
        Color(0xFF67E8F9) to Color(0xFF0891B2),
        Color(0xFFA78BFA) to Color(0xFF6D28D9),
    )

    fun avatarPalette(seed: String): Pair<Color, Color> =
        if (seed.isEmpty()) AVATARS[0]
        else AVATARS[seed.uppercase().sumOf { it.code } % AVATARS.size]

    // Corner radii — iOS cards run larger than Windows' R_LG (16).
    const val R_SM = 10
    const val R_MD = 14
    const val R_LG = 20
    const val R_XL = 26
}

/**
 * iOS-style type: heavy weights and slightly tightened tracking on the
 * display sizes, which is most of what makes SF Pro headings read as they
 * do. SF itself is Apple-licensed for Apple platforms only, so the system
 * sans carries it here.
 */
object Type {
    private val f = FontFamily.Default
    val largeTitle = TextStyle(fontFamily = f, fontWeight = FontWeight.Black, fontSize = 32.sp,
        letterSpacing = (-0.8).sp, color = T.TEXT)
    val title = TextStyle(fontFamily = f, fontWeight = FontWeight.ExtraBold, fontSize = 24.sp,
        letterSpacing = (-0.5).sp, color = T.TEXT)
    val title2 = TextStyle(fontFamily = f, fontWeight = FontWeight.ExtraBold, fontSize = 20.sp,
        letterSpacing = (-0.3).sp, color = T.TEXT)
    val headline = TextStyle(fontFamily = f, fontWeight = FontWeight.Bold, fontSize = 17.sp,
        letterSpacing = (-0.2).sp, color = T.TEXT)
    val body = TextStyle(fontFamily = f, fontWeight = FontWeight.Medium, fontSize = 15.sp, color = T.TEXT_SEC)
    val bodyBold = TextStyle(fontFamily = f, fontWeight = FontWeight.Bold, fontSize = 15.sp, color = T.TEXT)
    val callout = TextStyle(fontFamily = f, fontWeight = FontWeight.SemiBold, fontSize = 14.sp, color = T.TEXT_SEC)
    val footnote = TextStyle(fontFamily = f, fontWeight = FontWeight.SemiBold, fontSize = 13.sp, color = T.TEXT_MUTED)
    val caption = TextStyle(fontFamily = f, fontWeight = FontWeight.ExtraBold, fontSize = 11.sp,
        letterSpacing = 1.6.sp, color = T.TEXT_MUTED)
    val mono = TextStyle(fontFamily = FontFamily.Monospace, fontWeight = FontWeight.SemiBold,
        fontSize = 12.sp, color = T.TEXT_MUTED)
    val button = TextStyle(fontFamily = f, fontWeight = FontWeight.Bold, fontSize = 15.sp, color = Color.White)
}

@Composable
fun LogicShareTheme(content: @Composable () -> Unit) {
    // Dark always, as on Windows — the glass look is the product's identity.
    @Suppress("UNUSED_VARIABLE") val dark = isSystemInDarkTheme()
    MaterialTheme(
        colorScheme = darkColorScheme(
            primary = T.ACCENT, onPrimary = Color.White,
            background = Color(0xFF0D1522), surface = Color(0xFF151D2A),
            onBackground = T.TEXT, onSurface = T.TEXT,
            surfaceVariant = Color(0xFF1B2433), onSurfaceVariant = T.TEXT_SEC,
            error = T.DANGER,
        ),
        content = content,
    )
}
