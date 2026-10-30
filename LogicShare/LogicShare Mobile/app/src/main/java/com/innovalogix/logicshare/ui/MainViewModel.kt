package com.innovalogix.logicshare.ui

import android.app.Application
import android.content.Context
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.innovalogix.logicshare.BuildConfig
import com.innovalogix.logicshare.LogicShareApp
import com.innovalogix.logicshare.ack.ACK_PROTOCOL_VERSION
import com.innovalogix.logicshare.ack.TEACHER_ACK_PORT
import com.innovalogix.logicshare.core.AppLog
import com.innovalogix.logicshare.core.Profile
import com.innovalogix.logicshare.core.Recipient
import com.innovalogix.logicshare.core.formatClass
import com.innovalogix.logicshare.core.newAnnouncementId
import com.innovalogix.logicshare.net.BOARD_PORT
import com.innovalogix.logicshare.net.Discovery
import com.innovalogix.logicshare.net.FoundBoard
import com.innovalogix.logicshare.net.HttpResult
import com.innovalogix.logicshare.net.PickedFile
import com.innovalogix.logicshare.net.UploadOutcome
import com.innovalogix.logicshare.net.announcePayload
import com.innovalogix.logicshare.net.isHostReachable
import com.innovalogix.logicshare.net.parseJsonObject
import com.innovalogix.logicshare.net.postJson
import com.innovalogix.logicshare.net.sendAnnouncement
import com.innovalogix.logicshare.net.uploadFile
import com.innovalogix.logicshare.update.Updater
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.util.Locale

/** Windows' current_state values. */
enum class Phase { NONE, SCANNING, NO_BOARDS, OFFLINE, LIVE }

/** A board card. */
data class Board(val cls: String, val section: String, val room: String, val ip: String) {
    val title: String get() = "${formatClass(cls, section)}  ·  Room $room"
}

data class MainState(
    val phase: Phase = Phase.NONE,
    val boards: List<Board> = emptyList(),
    val countLabel: String = "Looking for LogicBoards…📡",
    val selectMode: Boolean = false,
    val selected: Set<String> = emptySet(),
    val disabled: Boolean = false,
    /** IPs whose ⋯ button is mid reachability check (shows "…"). */
    val checking: Set<String> = emptySet(),
)

/** A modal message — Windows' CustomMessageBox. */
data class Message(
    val kind: Kind, val title: String, val text: String, val detail: String = "",
    val onDismiss: (() -> Unit)? = null,
) { enum class Kind { INFO, WARNING } }

sealed class UploadStatus {
    object Running : UploadStatus()
    data class Done(val savedAs: String, val avgSpeed: String, val tookSeconds: Double) : UploadStatus()
    data class Failed(val message: String) : UploadStatus()
}

data class UploadState(
    val fileName: String, val total: Long, val target: String,
    val sent: Long = 0, val speed: String = "",
    val status: UploadStatus = UploadStatus.Running,
    val index: Int = 1, val count: Int = 1,
)

sealed class AnnounceStatus {
    object Sending : AnnounceStatus()
    object Sent : AnnounceStatus()
    data class Failed(val message: String) : AnnounceStatus()
}

data class BroadcastRow(val title: String, val ok: Boolean, val detail: String)

data class BroadcastState(
    val title: String, val total: Int, val done: Int = 0,
    val rows: List<BroadcastRow> = emptyList(),
    val finished: Pair<Int, Int>? = null,       // ok, failed
)

/** Search + filter on the board list. Single choice per filter dimension,
 *  the same as LogicShare Windows. */
data class BoardFilter(
    val query: String = "",
    val cls: String? = null,
    val section: String? = null,
    val searchOpen: Boolean = false,
) {
    val active: Boolean get() = query.isNotBlank() || cls != null || section != null
    val filtering: Boolean get() = cls != null || section != null
}

/** The boards that pass the search and the filters, in list order. */
fun applyBoardFilter(boards: List<Board>, f: BoardFilter): List<Board> {
    if (!f.active) return boards
    val q = com.innovalogix.logicshare.core.SmartSearch.parse(f.query)
    return boards.filter { b ->
        (f.cls == null || b.cls.equals(f.cls, ignoreCase = true)) &&
            (f.section == null || b.section.equals(f.section, ignoreCase = true)) &&
            com.innovalogix.logicshare.core.SmartSearch.matchBoard(q, b.cls, b.section, b.room, b.ip)
    }
}

/** Distinct values for the filter menu, numbers in numeric order. */
fun filterValues(values: List<String>): List<String> =
    values.filter { it.isNotBlank() }.distinct()
        .sortedWith(compareBy({ it.toIntOrNull() ?: Int.MAX_VALUE }, { it.lowercase() }))

sealed class UpdateUi {
    object Hidden : UpdateUi()
    data class Offer(val info: Updater.Info, val downloaded: Boolean,
                     val progress: Pair<Long, Long>? = null, val busy: Boolean = false,
                     val error: String? = null, val status: String? = null) : UpdateUi()
}

class MainViewModel(app: Application) : AndroidViewModel(app) {
    private val ls = app as LogicShareApp
    private val ctx: Context get() = getApplication()

    // Windows' constants.
    private val SCAN_TIMEOUT_MS = 10_000L
    private val AUTO_INTERVAL_MS = 10_000L
    private val RESCAN_WINDOW_MS = 4_000L
    private val NET_POLL_MS = 3_000L

    private val _state = MutableStateFlow(MainState())
    val state: StateFlow<MainState> = _state

    private val _profile = MutableStateFlow(ls.profiles.load())
    val profile: StateFlow<Profile?> = _profile

    private val _message = MutableStateFlow<Message?>(null)
    val message: StateFlow<Message?> = _message

    private val _upload = MutableStateFlow<UploadState?>(null)
    val upload: StateFlow<UploadState?> = _upload

    private val _announce = MutableStateFlow<AnnounceStatus?>(null)
    val announce: StateFlow<AnnounceStatus?> = _announce

    private val _broadcast = MutableStateFlow<BroadcastState?>(null)
    val broadcast: StateFlow<BroadcastState?> = _broadcast

    private val _update = MutableStateFlow<UpdateUi>(UpdateUi.Hidden)
    val update: StateFlow<UpdateUi> = _update

    private val _filter = MutableStateFlow(BoardFilter())
    val filter: StateFlow<BoardFilter> = _filter

    fun setSearchOpen(open: Boolean) =
        _filter.update { if (open) it.copy(searchOpen = true) else it.copy(searchOpen = false, query = "") }
    fun setQuery(q: String) = _filter.update { it.copy(query = q.take(80)) }
    fun setClassFilter(c: String?) = _filter.update { it.copy(cls = c) }
    fun setSectionFilter(s: String?) = _filter.update { it.copy(section = s) }
    fun clearFilters() = _filter.update { BoardFilter(searchOpen = it.searchOpen) }

    /** Selection mode: tick every board the search/filter is showing. */
    fun selectAll(ips: Collection<String>) =
        _state.update { if (it.selectMode) it.copy(selected = it.selected + ips) else it }

    /** Files shared into LogicShare from another app, waiting for a board. */
    private val _shared = MutableStateFlow<List<PickedFile>>(emptyList())
    val shared: StateFlow<List<PickedFile>> = _shared

    private val _openReport = MutableSharedFlow<String>(extraBufferCapacity = 4)
    val openReport: SharedFlow<String> = _openReport

    // Selection-mode payload (Windows' _pending_title / _pending_message).
    private var pendingTitle = ""
    private var pendingMessage = ""

    private val detected = linkedSetOf<String>()
    private val boards = LinkedHashMap<String, Board>()
    private val greeted = java.util.Collections.synchronizedSet(mutableSetOf<String>())

    private var scanTimeoutJob: Job? = null
    private var autoJob: Job? = null
    private var rescanEndJob: Job? = null
    private var pollJob: Job? = null
    private var netEventJob: Job? = null
    private var netForced = false
    private var lastNetEventAt = 0L

    private var netFp: String = ls.netWatch.fingerprint()
    private val discovery = Discovery(app) { b -> viewModelScope.launch { onBoardDetected(b) } }

    private var started = false

    /** Called once a profile exists — Windows builds LogicShareUI only
     *  after the setup dialog has been answered. */
    fun start() {
        if (started) return
        started = true
        ls.netWatch.start { kind -> viewModelScope.launch { onNetEvent(kind) } }
        pollJob = viewModelScope.launch {
            while (isActive) { delay(NET_POLL_MS); pollNetwork() }
        }
        pollNetwork()
        // Look for a new release a few seconds in, so the request never
        // competes with the first paint or with discovery.
        viewModelScope.launch { delay(5_000); checkForUpdates(silent = true) }
        // Packages that already did their job. Again two minutes in: when this
        // launch IS the installer's relaunch, the old package may still be
        // held by the package installer for a moment.
        viewModelScope.launch(Dispatchers.IO) {
            Updater.pruneSpent(ctx); delay(120_000); Updater.pruneSpent(ctx)
        }
    }

    override fun onCleared() {
        stopTimers()
        pollJob?.cancel()
        discovery.stop()
        ls.netWatch.stop()
        super.onCleared()
    }

    // ── profile ─────────────────────────────────────────────────────────
    fun saveProfile(first: String, surname: String, short: String, title: String): Boolean {
        val p = Profile(first, surname, short, title)
        val ok = ls.profiles.save(p)
        if (ok) _profile.value = ls.profiles.load() ?: p
        else showMessage(Message.Kind.WARNING, "LogicShare", "Could not save profile.")
        return ok
    }

    // ── messages ────────────────────────────────────────────────────────
    private fun showMessage(kind: Message.Kind, title: String, text: String,
                            detail: String = "", onDismiss: (() -> Unit)? = null) {
        _message.value = Message(kind, title, text, detail, onDismiss)
    }

    fun dismissMessage() {
        val m = _message.value
        _message.value = null
        m?.onDismiss?.invoke()
    }

    // ── network ─────────────────────────────────────────────────────────
    /** ConnectivityManager said something happened. Settle 250 ms (DHCP may
     *  not have finished — net_watch.SETTLE_SECONDS), coalesce bursts, then
     *  look. "lost" counts as forced: the network genuinely went, even if a
     *  fast reconnect puts the same address back before we read it. */
    private fun onNetEvent(kind: String) {
        if (kind == "lost") netForced = true
        netEventJob?.cancel()
        netEventJob = viewModelScope.launch {
            delay(250)
            val forced = netForced
            netForced = false
            val fp = ls.netWatch.fingerprint()
            if (fp == netFp && !forced) return@launch
            val now = System.currentTimeMillis()
            if (now - lastNetEventAt < 1_000 && fp == netFp) return@launch   // MIN_CALLBACK_GAP
            lastNetEventAt = now
            onNetworkChanged(if (forced) "os-event" else "link-changed")
        }
    }

    /** Windows' _poll_network. Identity FIRST — before the "boards are
     *  listed, nothing to do" return that once made this blind for good. */
    private fun pollNetwork() {
        if (_state.value.disabled) return
        if (ls.netWatch.fingerprint() != netFp) { onNetworkChanged("poll"); return }
        if (!ls.netWatch.isOnline(netFp)) {
            stopTimers(); discovery.stop()
            enterState(Phase.OFFLINE); return
        }
        if (boards.isNotEmpty()) return
        if (_state.value.phase != Phase.SCANNING && _state.value.phase != Phase.NO_BOARDS) beginScan()
    }

    /** Windows' _on_network_changed: everything learned from the old network
     *  is junk. An OS event resyncs even with an unchanged address; a poll
     *  only acts on a changed fingerprint. */
    private fun onNetworkChanged(reason: String) {
        val fp = ls.netWatch.fingerprint()
        val unchanged = fp == netFp
        netFp = fp
        if (unchanged && reason == "poll") return
        AppLog.i("Network changed ($reason): now ${if (fp.isEmpty()) "offline" else fp} — " +
                "dropping ${boards.size} board(s) and rescanning")
        // A selection made against boards that no longer exist would send
        // to stale addresses the moment Send was pressed.
        _state.update { it.copy(selected = emptySet()) }
        refresh(fp)
    }

    /** Windows' _refresh — also the Refresh button. */
    fun refresh() = refresh(ls.netWatch.fingerprint())

    /** ``fp`` is the reading that triggered this. Deciding online/offline
     *  from a SECOND read could disagree with the first during an adapter
     *  transition and send the app scanning on a network that is going. */
    private fun refresh(fp: String) {
        if (_state.value.disabled) return
        stopTimers(); discovery.stop()
        boards.clear(); detected.clear()
        _state.update { it.copy(boards = emptyList(), countLabel = "Looking for LogicBoards…📡",
            phase = Phase.NONE) }
        if (ls.netWatch.isOnline(fp)) beginScan() else enterState(Phase.OFFLINE)
    }

    // ── scanning ────────────────────────────────────────────────────────
    private fun beginScan() {
        stopTimers(); discovery.stop()
        enterState(Phase.SCANNING)
        if (!startDiscovery()) return
        scanTimeoutJob = viewModelScope.launch { delay(SCAN_TIMEOUT_MS); onScanTimeout() }
    }

    private fun onScanTimeout() {
        if (boards.isNotEmpty()) return
        enterState(Phase.NO_BOARDS)
        if (autoJob?.isActive != true) {
            autoJob = viewModelScope.launch {
                while (isActive) { delay(AUTO_INTERVAL_MS); autoRescan() }
            }
        }
    }

    private fun autoRescan() {
        if (boards.isNotEmpty()) { autoJob?.cancel(); return }
        enterState(Phase.SCANNING)
        discovery.stop()
        if (!startDiscovery()) return
        AppLog.i("Auto-rescanning for Smart Boards")
        rescanEndJob?.cancel()
        rescanEndJob = viewModelScope.launch { delay(RESCAN_WINDOW_MS); autoRescanEnd() }
    }

    private fun autoRescanEnd() {
        if (boards.isNotEmpty()) return
        enterState(Phase.NO_BOARDS)
    }

    private fun stopTimers() {
        scanTimeoutJob?.cancel(); autoJob?.cancel(); rescanEndJob?.cancel()
        scanTimeoutJob = null; autoJob = null; rescanEndJob = null
    }

    private fun startDiscovery(): Boolean {
        if (discovery.start()) return true
        enterDisabled()
        return false
    }

    private fun enterState(p: Phase) {
        if (_state.value.phase == p) return
        _state.update { it.copy(phase = p) }
    }

    /** Graceful fatal-discovery handling: say so once, stop polling, never
     *  crash. Restarting the app recovers. */
    private fun enterDisabled() {
        if (_state.value.disabled) return
        AppLog.e("LogicShare entering disabled state (discovery unavailable)")
        pollJob?.cancel(); stopTimers()
        _state.update { it.copy(disabled = true, phase = Phase.OFFLINE,
            countLabel = "LogicShare is temporarily disabled") }
        showMessage(Message.Kind.WARNING, "LogicShare",
            "LogicShare on this smart board has been temporarily disabled due to an error.",
            "Device discovery could not start. Please restart LogicShare or check the network connection.")
    }

    // ── boards ──────────────────────────────────────────────────────────
    private fun foundLabel(n: Int) = "$n LogicBoard${if (n != 1) "s" else ""} found 😎🔥"

    private fun onBoardDetected(b: FoundBoard) {
        // Greet BEFORE the early return: a re-advertise is exactly when a
        // board may be holding acknowledgments for us.
        greetBoard(b.ip)
        if (b.ip in detected) return
        detected += b.ip
        stopTimers()
        boards[b.ip] = Board(b.cls, b.section, b.room, b.ip)
        _state.update {
            it.copy(boards = boards.values.toList(), phase = Phase.LIVE,
                countLabel = if (it.selectMode) it.countLabel else foundLabel(boards.size))
        }
    }

    /** A board turned out to be unreachable — Windows' _on_card_removed. */
    fun removeBoard(ip: String) {
        detected -= ip
        boards.remove(ip)
        val st = _state.value
        _state.update { it.copy(boards = boards.values.toList(), selected = it.selected - ip) }
        if (st.selectMode) return
        if (boards.isEmpty()) {
            stopTimers(); discovery.stop()
            _state.update { it.copy(countLabel = "Looking for LogicBoards…📡", phase = Phase.NONE) }
            if (ls.netWatch.isOnline()) beginScan() else enterState(Phase.OFFLINE)
        } else {
            _state.update { it.copy(countLabel = foundLabel(boards.size)) }
        }
    }

    /** ⋯ → an action: check the board is still there first (Windows'
     *  is_host_reachable), exactly as the card menu does. */
    fun withReachableBoard(board: Board, forFile: Boolean, then: () -> Unit) {
        _state.update { it.copy(checking = it.checking + board.ip) }
        viewModelScope.launch {
            val alive = withContext(Dispatchers.IO) { isHostReachable(board.ip) }
            _state.update { it.copy(checking = it.checking - board.ip) }
            if (alive) { then(); return@launch }
            val name = formatClass(board.cls, board.section)
            if (forFile) showMessage(Message.Kind.WARNING, "Board Offline 💀",
                "Looks like $name (Room ${board.room}) just dipped offline…",
                "Maybe the board lost Wi-Fi. Hit Refresh to look again 😎") { removeBoard(board.ip) }
            else showMessage(Message.Kind.WARNING, "Board Offline 💀",
                "$name (Room ${board.room}) is offline rn.",
                "Try Refresh to look again 😎") { removeBoard(board.ip) }
        }
    }

    // ── acknowledgments: greeting ───────────────────────────────────────
    /** Tell a board where to send acknowledgments for this device, once per
     *  board per session; a failure allows a retry next time it is seen. */
    private fun greetBoard(ip: String) {
        if (ip.isEmpty() || !greeted.add(ip)) return
        val payload = JSONObject()
            .put("teacher_device_id", ls.identity.deviceId)
            .put("ack_port", ls.ackServer.port.takeIf { it > 0 } ?: TEACHER_ACK_PORT)
            .put("ack_version", ACK_PROTOCOL_VERSION)
        viewModelScope.launch(Dispatchers.IO) {
            when (val r = postJson("http://$ip:$BOARD_PORT/teacher-hello", payload, 3000, 6000)) {
                is HttpResult.Ok -> if (r.code == 200) {
                    val pending = parseJsonObject(r.body)?.optInt("pending_acks", 0) ?: 0
                    if (pending > 0) AppLog.i("Board $ip was holding $pending acknowledgment(s) for us — flushing")
                    else AppLog.d("Greeted board $ip")
                } else AppLog.d("Board $ip did not accept hello (HTTP ${r.code}) — older build")
                // The reason is logged: a bare "failed" is how the cleartext
                // block went unnoticed until it was tested on a real phone.
                else -> { AppLog.w("Hello to $ip failed: $r"); greeted.remove(ip) }
            }
        }
    }

    // ── selection mode ──────────────────────────────────────────────────
    private fun selLabel(n: Int) = "$n board${if (n != 1) "s" else ""} selected"

    fun enterSelectionMode(title: String, message: String) {
        pendingTitle = title; pendingMessage = message
        _state.update { it.copy(selected = emptySet()) }
        if (boards.isEmpty()) {
            showMessage(Message.Kind.INFO, "No Smart Boards",
                "No Smart Boards are online right now.",
                "Try Refresh, then create your announcement again.")
            return
        }
        _state.update { it.copy(selectMode = true) }
    }

    fun exitSelectionMode() {
        _state.update {
            it.copy(selectMode = false, selected = emptySet(),
                countLabel = if (boards.isNotEmpty()) foundLabel(boards.size) else it.countLabel)
        }
    }

    fun toggleSelected(ip: String) {
        _state.update { s ->
            if (!s.selectMode) s
            else s.copy(selected = if (ip in s.selected) s.selected - ip else s.selected + ip)
        }
    }

    fun selectionLabel(): String = selLabel(_state.value.selected.size)

    /** Windows' _send_to_selected: one announcement id generated here and
     *  carried by every board, every acknowledgment and the history row. */
    fun sendToSelected() {
        val sel = _state.value.selected
        val targets = boards.values.filter { it.ip in sel }
        if (targets.isEmpty()) return
        val title = pendingTitle
        val message = pendingMessage
        exitSelectionMode()
        val profile = _profile.value ?: return

        val announcementId = newAnnouncementId()
        val targetMaps = targets.map {
            mapOf("class" to it.cls, "section" to it.section, "room" to it.room, "ip" to it.ip)
        }
        ls.tracker.recordSent(announcementId, title, message, targetMaps)
        ls.history.add(title, message,
            targets.map { Recipient(it.cls, it.section, it.room, it.ip) }, announcementId)

        _broadcast.value = BroadcastState(title, targets.size)
        val ackPort = ls.ackServer.port.takeIf { it > 0 } ?: TEACHER_ACK_PORT
        viewModelScope.launch(Dispatchers.IO) {
            var ok = 0
            var fail = 0
            val payload = announcePayload(title, message, profile, announcementId,
                ls.identity.deviceId, ackPort)
            for ((i, t) in targets.withIndex()) {
                var success = false
                var detail: String
                var info: JSONObject? = null
                when (val r = postJson("http://${t.ip}:$BOARD_PORT/announce", payload, 5000, 10000)) {
                    is HttpResult.Ok -> if (r.code == 200) {
                        success = true; detail = "Delivered"; info = parseJsonObject(r.body)
                    } else { detail = "HTTP ${r.code}"; AppLog.e("Broadcast to ${t.ip} failed HTTP ${r.code}") }
                    HttpResult.ConnectFailed -> { detail = "Offline"; AppLog.w("Broadcast to ${t.ip}: offline") }
                    HttpResult.TimedOut -> { detail = "Timed out"; AppLog.w("Broadcast to ${t.ip}: timed out") }
                    is HttpResult.Failed -> { detail = "Error"; AppLog.e("Broadcast to ${t.ip}: ${r.message}") }
                }
                if (success) ok++ else fail++
                ls.tracker.recordDelivery(announcementId, t.ip, success, detail, info)
                val row = BroadcastRow("${formatClass(t.cls, t.section)} · Room ${t.room}", success, detail)
                _broadcast.update { it?.copy(done = i + 1, rows = it.rows + row) }
            }
            AppLog.i("Broadcast complete: $ok ok, $fail failed of ${targets.size}")
            _broadcast.update { it?.copy(finished = ok to fail) }
        }
    }

    fun closeBroadcast() { if (_broadcast.value?.finished != null) _broadcast.value = null }

    // ── single-board announcement ───────────────────────────────────────
    fun sendSingleAnnouncement(board: Board, title: String, message: String) {
        val profile = _profile.value ?: return
        _announce.value = AnnounceStatus.Sending
        viewModelScope.launch {
            val err = withContext(Dispatchers.IO) { sendAnnouncement(board.ip, title, message, profile) }
            _announce.value = if (err == null) AnnounceStatus.Sent else AnnounceStatus.Failed(err)
        }
    }

    fun clearAnnounce() { _announce.value = null }

    // ── uploads ─────────────────────────────────────────────────────────
    fun startUpload(board: Board, files: List<PickedFile>) {
        val profile = _profile.value ?: return
        if (files.isEmpty()) return
        viewModelScope.launch {
            for ((i, f) in files.withIndex()) {
                val target = "${formatClass(board.cls, board.section)}  ·  Room ${board.room}"
                _upload.value = UploadState(f.name, f.size.coerceAtLeast(0), target,
                    index = i + 1, count = files.size)
                val started = System.currentTimeMillis()
                var lastSent = 0L
                var lastTime = started
                val outcome = withContext(Dispatchers.IO) {
                    uploadFile(ctx, board.ip, f, profile) { sent, total ->
                        // Rolling ~300 ms speed window, and the last value is
                        // KEPT between recomputes — Windows' fix for the speed
                        // readout that used to flicker and vanish.
                        val now = System.currentTimeMillis()
                        val dt = now - lastTime
                        var speed: String? = null
                        if (dt >= 300) {
                            val bps = (sent - lastSent) * 1000.0 / dt
                            lastSent = sent; lastTime = now
                            speed = String.format(Locale.US, "%.1f MB/s", bps / (1024 * 1024))
                        }
                        _upload.update { it?.copy(sent = sent, total = total,
                            speed = speed ?: it.speed) }
                    }
                }
                when (outcome) {
                    is UploadOutcome.Success -> {
                        val elapsed = maxOf((System.currentTimeMillis() - started) / 1000.0, 0.001)
                        val bytes = _upload.value?.sent ?: f.size
                        val avg = bytes / elapsed
                        val avgStr = if (avg >= 1024 * 1024)
                            String.format(Locale.US, "%.1f MB/s", avg / (1024 * 1024))
                        else String.format(Locale.US, "%.0f KB/s", avg / 1024)
                        _upload.update { it?.copy(status = UploadStatus.Done(
                            outcome.response.optString("saved_as", ""), avgStr, elapsed)) }
                    }
                    is UploadOutcome.Failure -> {
                        _upload.update { it?.copy(status = UploadStatus.Failed(outcome.message)) }
                        return@launch       // stop the queue at the first failure
                    }
                }
                // Several shared files: let each result be seen, then move on.
                if (i < files.lastIndex) delay(900)
            }
        }
    }

    fun closeUpload() {
        if (_upload.value?.status !is UploadStatus.Running) _upload.value = null
    }

    // ── share target ────────────────────────────────────────────────────
    fun setShared(files: List<PickedFile>) { _shared.value = files }
    fun clearShared() { _shared.value = emptyList() }

    fun requestReport(announcementId: String) { _openReport.tryEmit(announcementId) }

    // ── updates ─────────────────────────────────────────────────────────
    private var checkingUpdates = false
    private var downloadJob: Job? = null
    @Volatile private var cancelDownload = false

    /** silent = the automatic check at startup (speaks only when there is
     *  genuinely a newer version); otherwise the menu item, which is owed an
     *  answer either way. */
    fun checkForUpdates(silent: Boolean) {
        if (checkingUpdates) {
            if (!silent) showMessage(Message.Kind.INFO, "Updates", "Already checking — hang on a sec 😅")
            return
        }
        checkingUpdates = true
        if (!silent) AppLog.i("Checking for updates…")
        viewModelScope.launch {
            val r = withContext(Dispatchers.IO) { Updater.check() }
            checkingUpdates = false
            when (r) {
                is Updater.Check.Failed -> {
                    AppLog.i("Update check: ${r.message}")
                    if (!silent) showMessage(Message.Kind.WARNING, "Updates", r.message)
                }
                is Updater.Check.Found -> {
                    val info = r.info
                    if (!info.isNewer()) {
                        AppLog.i("Update check: up to date (v${BuildConfig.VERSION_NAME})")
                        if (!silent) showMessage(Message.Kind.INFO, "Updates",
                            "You're on the latest version 😎\n\nLogicShare v${BuildConfig.VERSION_NAME}")
                        return@launch
                    }
                    AppLog.i("Update available: v${info.versionText} (running v${BuildConfig.VERSION_NAME})")
                    offer(info)
                }
            }
        }
    }

    /** NOTHING is downloaded before the teacher agrees. An APK already on
     *  disk (downloaded earlier, "Later" at install) opens the dialog on
     *  "install now?" instead of "download?". */
    private fun offer(info: Updater.Info) {
        val ready = Updater.existingDownload(ctx, info) != null
        _update.value = UpdateUi.Offer(info, downloaded = ready)
    }

    /** The dialog's main button: Download, or — once downloaded — Install. */
    fun updatePrimary() {
        val offer = _update.value as? UpdateUi.Offer ?: return
        if (offer.downloaded) installUpdate() else downloadUpdate()
    }

    /** Hand the downloaded package to Android's installer. Only ever from
     *  the "Install Now" button — a finished download alone never installs. */
    private fun installUpdate() {
        val offer = _update.value as? UpdateUi.Offer ?: return
        if (!Updater.canInstall(ctx)) {
            _update.value = offer.copy(status =
                "Allow LogicShare to install updates, then tap Install Now again.")
            Updater.openInstallPermission(ctx)
            return
        }
        val apk = Updater.existingDownload(ctx, offer.info)
        if (apk == null) {
            // Gone since it was offered (storage cleared): back to question 1.
            _update.value = offer.copy(downloaded = false, error = null, status = null)
            return
        }
        val err = Updater.launchInstaller(ctx, apk)
        _update.value = if (err == null) offer.copy(busy = false, error = null,
            status = "Android will ask you to confirm — LogicShare updates in place and keeps your data.")
        else offer.copy(busy = false, error = err)
    }

    /** Download with the teacher's say-so, then ASK before installing. */
    private fun downloadUpdate() {
        val offer = _update.value as? UpdateUi.Offer ?: return
        cancelDownload = false
        _update.value = offer.copy(busy = true, error = null, status = "Downloading the update…")
        downloadJob = viewModelScope.launch {
            val result = withContext(Dispatchers.IO) {
                runCatching {
                    Updater.download(ctx, offer.info, { rec, tot ->
                        _update.update { u ->
                            (u as? UpdateUi.Offer)?.copy(progress = rec to tot, status =
                                if (tot > 0) "Downloading… ${com.innovalogix.logicshare.core.formatSize(rec)} of " +
                                    com.innovalogix.logicshare.core.formatSize(tot)
                                else "Downloading… ${com.innovalogix.logicshare.core.formatSize(rec)}") ?: u
                        }
                    }, { cancelDownload })
                }
            }
            result.onSuccess {
                // Downloaded — now ASK. Installing replaces the running app,
                // which is the teacher's call, not the download's. "Later"
                // keeps the APK (pruneSpent spares a newer version).
                AppLog.i("Update ${offer.info.versionText} downloaded; asking before installing")
                _update.value = offer.copy(downloaded = true, busy = false, progress = null,
                    error = null, status = null)
            }.onFailure { e ->
                _update.value = if (e is Updater.CancelledException) UpdateUi.Hidden
                else offer.copy(busy = false, error = e.message ?: "The download failed.")
            }
        }
    }

    fun dismissUpdate() {
        val o = _update.value as? UpdateUi.Offer
        if (o?.busy == true) {
            cancelDownload = true
            _update.update { (it as? UpdateUi.Offer)?.copy(status = "Cancelling…") ?: it }
        } else {
            if (o?.downloaded == true)
                AppLog.i("Update ${o.info.versionText} kept for later (not installed)")
            _update.value = UpdateUi.Hidden
        }
    }
}
