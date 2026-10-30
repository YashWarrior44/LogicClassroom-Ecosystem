package com.innovalogix.logicshare.core

import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** Windows' format_size. Locale.US because String.format otherwise follows
 *  the phone's language, and "1,5 MB" is not what the Windows app prints. */
fun formatSize(n: Long): String {
    if (n < 1024) return "$n B"
    var v = n.toDouble()
    for (unit in listOf("KB", "MB", "GB")) {
        v /= 1024
        if (v < 1024) return String.format(Locale.US, "%.1f %s", v, unit)
    }
    return String.format(Locale.US, "%.1f TB", v / 1024)
}

/** Windows' format_class: "Class 11-A", or "Class 11" with no section. */
fun formatClass(cls: String, section: String = ""): String =
    if (section.isNotEmpty()) "Class $cls-$section" else "Class $cls"

/** Windows' time_ago_short. */
fun timeAgoShort(ts: Double): String {
    if (ts <= 0) return "just now"
    val delta = maxOf(0.0, nowSeconds() - ts)
    return when {
        delta < 60 -> "just now"
        delta < 3600 -> "${(delta / 60).toInt()}m ago"
        delta < 86400 -> "${(delta / 3600).toInt()}h ago"
        else -> {
            val days = (delta / 86400).toInt()
            when {
                days == 1 -> "yesterday"
                days < 7 -> "${days}d ago"
                else -> SimpleDateFormat("dd MMM", Locale.getDefault())
                    .format(Date((ts * 1000).toLong()))
            }
        }
    }
}

/** Windows' _ack_clock: HH:MM. */
fun ackClock(ts: Double?): String =
    if (ts == null || ts <= 0) "" else
        SimpleDateFormat("HH:mm", Locale.getDefault()).format(Date((ts * 1000).toLong()))
