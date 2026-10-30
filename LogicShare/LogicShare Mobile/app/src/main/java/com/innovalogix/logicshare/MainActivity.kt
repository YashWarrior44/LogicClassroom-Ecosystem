package com.innovalogix.logicshare

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.res.Configuration
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInHorizontally
import androidx.compose.animation.slideOutHorizontally
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.toArgb
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.innovalogix.logicshare.ack.AckService
import com.innovalogix.logicshare.core.AppLog
import com.innovalogix.logicshare.net.describeUri
import com.innovalogix.logicshare.ui.AboutDialog
import com.innovalogix.logicshare.ui.Board
import com.innovalogix.logicshare.ui.BroadcastDialog
import com.innovalogix.logicshare.ui.ComposerScreen
import com.innovalogix.logicshare.ui.HelpDialog
import com.innovalogix.logicshare.ui.HistoryScreen
import com.innovalogix.logicshare.ui.MainActions
import com.innovalogix.logicshare.ui.MainScreen
import com.innovalogix.logicshare.ui.MainViewModel
import com.innovalogix.logicshare.ui.Message
import com.innovalogix.logicshare.ui.MessageDialog
import com.innovalogix.logicshare.ui.ProfileScreen
import com.innovalogix.logicshare.ui.ReportScreen
import com.innovalogix.logicshare.ui.SingleAnnouncementScreen
import com.innovalogix.logicshare.ui.UpdateDialog
import com.innovalogix.logicshare.ui.UpdateUi
import com.innovalogix.logicshare.ui.UploadDialog
import com.innovalogix.logicshare.ui.theme.LogicShareTheme
import com.innovalogix.logicshare.ui.theme.T
import kotlinx.coroutines.launch

const val MAX_FONT_SCALE = 1.15f

/** Where the app is. The board list is always underneath; the rest push. */
sealed class Screen {
    object Main : Screen()
    object Composer : Screen()
    object History : Screen()
    object EditProfile : Screen()
    data class Report(val id: String) : Screen()
    data class Single(val board: Board) : Screen()
}

/**
 * The one window. launchMode="singleTop" plus the task model give Android's
 * version of LogicShare Windows' single-instance rule for free: launching
 * LogicShare again brings this back rather than starting a second copy.
 */
class MainActivity : ComponentActivity() {
    private val vm: MainViewModel by viewModels()
    private val app get() = application as LogicShareApp

    /** Follow the system font size, but only so far. At Samsung's 150% every
     *  button and bar grew by half and the layout stopped looking designed
     *  (Galaxy M21). Up to +15% still reads larger for anyone who asked for
     *  it. Done on the configuration, not in Compose, so dialogs and menus
     *  (their own windows) get the same cap. A font-size change recreates
     *  the activity, which runs this again. */
    override fun attachBaseContext(base: Context) {
        val cfg = base.resources.configuration
        if (cfg.fontScale <= MAX_FONT_SCALE) return super.attachBaseContext(base)
        val capped = Configuration(cfg).apply { fontScale = MAX_FONT_SCALE }
        super.attachBaseContext(base.createConfigurationContext(capped))
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        enableEdgeToEdge(
            statusBarStyle = SystemBarStyle.dark(android.graphics.Color.TRANSPARENT),
            navigationBarStyle = SystemBarStyle.dark(T.BAR_BOTTOM.toArgb()),
        )
        super.onCreate(savedInstanceState)
        lifecycleScope.launch { app.quitRequested.collect { finishAndRemoveTask() } }
        handleIntent(intent)
        setContent { LogicShareTheme { Root() } }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleIntent(intent)
    }

    /** Share-sheet files, and the "open this report" a timeout notification
     *  carries. */
    private fun handleIntent(intent: Intent?) {
        intent ?: return
        intent.getStringExtra(AckService.EXTRA_REPORT)?.let {
            vm.requestReport(it)
            intent.removeExtra(AckService.EXTRA_REPORT)
        }
        val uris: List<Uri> = when (intent.action) {
            Intent.ACTION_SEND -> listOfNotNull(
                if (Build.VERSION.SDK_INT >= 33) intent.getParcelableExtra(Intent.EXTRA_STREAM, Uri::class.java)
                else @Suppress("DEPRECATION") intent.getParcelableExtra(Intent.EXTRA_STREAM))
            Intent.ACTION_SEND_MULTIPLE ->
                (if (Build.VERSION.SDK_INT >= 33) intent.getParcelableArrayListExtra(Intent.EXTRA_STREAM, Uri::class.java)
                else @Suppress("DEPRECATION") intent.getParcelableArrayListExtra(Intent.EXTRA_STREAM)).orEmpty()
            else -> emptyList()
        }
        if (uris.isNotEmpty()) {
            AppLog.i("Received ${uris.size} shared file(s)")
            vm.setShared(uris.map { describeUri(this, it) })
            intent.action = null      // don't re-share on rotation
        }
    }

    @Composable
    private fun Root() {
        val profile by vm.profile.collectAsState()
        val state by vm.state.collectAsState()
        val message by vm.message.collectAsState()
        val upload by vm.upload.collectAsState()
        val announce by vm.announce.collectAsState()
        val broadcast by vm.broadcast.collectAsState()
        val update by vm.update.collectAsState()
        val shared by vm.shared.collectAsState()
        val filter by vm.filter.collectAsState()

        var screen by remember { mutableStateOf<Screen>(Screen.Main) }
        var about by remember { mutableStateOf(false) }
        var help by remember { mutableStateOf(false) }
        var fileTarget by remember { mutableStateOf<Board?>(null) }

        val picker = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
            val b = fileTarget
            fileTarget = null
            if (uri != null && b != null) vm.startUpload(b, listOf(describeUri(this, uri)))
        }
        val notifPerm = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) {}

        LaunchedEffect(Unit) { vm.openReport.collect { screen = Screen.Report(it) } }
        // Files shared in from another app: go to the board list, where the
        // "tap Send on a board" banner lives. Without this, sharing while
        // LogicShare was last left on History or a report did visibly
        // nothing — found on a real phone, not in a test.
        LaunchedEffect(shared) {
            if (shared.isNotEmpty()) {
                screen = Screen.Main
                if (state.selectMode) vm.exitSelectionMode()
            }
        }

        Box(Modifier.fillMaxSize().background(T.backdrop)) {
            val p = profile
            if (p == null) {
                // First run — Windows shows the setup dialog before anything else.
                ProfileScreen(null, onSave = { f, s, sh, t -> vm.saveProfile(f, s, sh, t) }, onBack = null)
            } else {
                LaunchedEffect(Unit) {
                    vm.start()
                    AckService.start(this@MainActivity)
                    if (Build.VERSION.SDK_INT >= 33 && ContextCompat.checkSelfPermission(this@MainActivity,
                            Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
                        // Needed for the five-minute "no reply yet" warning.
                        notifPerm.launch(Manifest.permission.POST_NOTIFICATIONS)
                    }
                }
                BackHandler(enabled = screen != Screen.Main || state.selectMode || filter.searchOpen) {
                    when {
                        screen != Screen.Main -> screen = Screen.Main
                        filter.searchOpen -> vm.setSearchOpen(false)
                        else -> vm.exitSelectionMode()
                    }
                }
                AnimatedContent(screen, transitionSpec = {
                    if (targetState == Screen.Main) (fadeIn() + slideInHorizontally { -it / 4 }) togetherWith
                            (fadeOut() + slideOutHorizontally { it / 3 })
                    else (fadeIn() + slideInHorizontally { it / 3 }) togetherWith fadeOut()
                }, label = "nav") { s ->
                    when (s) {
                        Screen.Main -> MainScreen(state, p, shared, filter, MainActions(
                            onRefresh = vm::refresh,
                            onCompose = { screen = Screen.Composer },
                            onSendFile = { b -> vm.withReachableBoard(b, forFile = true) {
                                fileTarget = b; picker.launch(arrayOf("*/*")) } },
                            onSendAnnouncement = { b -> vm.withReachableBoard(b, forFile = false) {
                                vm.clearAnnounce(); screen = Screen.Single(b) } },
                            onSendShared = { b -> vm.withReachableBoard(b, forFile = true) {
                                vm.startUpload(b, shared); vm.clearShared() } },
                            onCancelShared = vm::clearShared,
                            onToggle = vm::toggleSelected,
                            onCancelSelection = vm::exitSelectionMode,
                            onSendSelected = vm::sendToSelected,
                            onEditProfile = { screen = Screen.EditProfile },
                            onHistory = { screen = Screen.History },
                            onAbout = { about = true },
                            onHelp = { help = true },
                            onCheckUpdates = { vm.checkForUpdates(silent = false) },
                            onSearchOpen = vm::setSearchOpen,
                            onQuery = vm::setQuery,
                            onClassFilter = vm::setClassFilter,
                            onSectionFilter = vm::setSectionFilter,
                            onClearFilters = vm::clearFilters,
                            onSelectAll = vm::selectAll,
                        ))
                        Screen.Composer -> ComposerScreen(p, onBack = { screen = Screen.Main },
                            onHistory = { screen = Screen.History },
                            onSelect = { t, m -> screen = Screen.Main; vm.enterSelectionMode(t, m) })
                        Screen.History -> HistoryScreen(app.history, app.tracker, onBack = { screen = Screen.Main },
                            onReport = { screen = Screen.Report(it) },
                            onNoReport = {
                                showInfo("No report for this one",
                                    "LogicShare has no delivery record for this announcement.",
                                    "It was sent before acknowledgment tracking existed, or its record has since " +
                                            "been cleared. Everything you send from now on gets a full board-by-board report.")
                            })
                        Screen.EditProfile -> ProfileScreen(p, onSave = { f, sh2, sh, t ->
                            if (vm.saveProfile(f, sh2, sh, t)) screen = Screen.Main }, onBack = { screen = Screen.Main })
                        is Screen.Report -> ReportScreen(app.tracker, s.id, onBack = { screen = Screen.Main })
                        is Screen.Single -> SingleAnnouncementScreen(s.board, p, announce,
                            onSend = { t, m -> vm.sendSingleAnnouncement(s.board, t, m) },
                            onClose = { vm.clearAnnounce(); screen = Screen.Main })
                    }
                }
            }

            upload?.let { UploadDialog(it, vm::closeUpload) }
            broadcast?.let { BroadcastDialog(it, vm::closeBroadcast) }
            (update as? UpdateUi.Offer)?.let { UpdateDialog(it, vm::updatePrimary, vm::dismissUpdate) }
            if (about) AboutDialog(BuildConfig.VERSION_NAME) { about = false }
            if (help) HelpDialog { help = false }
            message?.let { MessageDialog(it, vm::dismissMessage) }
            extraInfo?.let { MessageDialog(it) { extraInfo = null } }
        }
    }

    // A message raised from navigation code rather than the view model.
    private var extraInfo by mutableStateOf<Message?>(null)
    private fun showInfo(title: String, text: String, detail: String) {
        extraInfo = Message(Message.Kind.INFO, title, text, detail)
    }
}
