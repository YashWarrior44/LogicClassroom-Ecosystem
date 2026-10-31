// =============================================================================
// LogicBoardInstaller.cpp
//
// Single-file Windows installer for LogicBoard, with an Aero-glass modern UI.
//
//   * Frameless translucent window using DWM's system-backdrop APIs
//     (Windows 11 acrylic on 22H2+, Aero blur fallback on 10 / early 11).
//   * Three screens: Welcome -> Installing -> Done, plus an Error surface
//     shown if anything goes wrong along the way. All painted with GDI+
//     for smooth rounded panels + anti-aliased text.
//   * ZIP payload + icon are BAKED INTO THE EXE via a .rc resource file
//     (see resources.rc), so the shipped installer is truly a single file.
//   * Runs asInvoker — NO elevation for the install itself. The one
//     operation that needs administrator rights (deleting a previous
//     machine-wide install out of Program Files) is handed to a short-lived
//     elevated re-launch of this same exe; see RemoveLegacyInstall.
//   * Installs to %USERPROFILE%\LogicBoard, fixed — no path picker.
//   * Creates Desktop + Start Menu shortcuts (IShellLink / IPersistFile),
//     idempotently: an existing correct shortcut is left alone, an existing
//     stale one is repointed in place. Neither case ever adds a second icon.
//   * Registers under HKCU Uninstall so LogicBoard shows up in
//     Apps & Features and is reachable via Windows Search.
//   * Self-copies to the install dir as ``Uninstaller.exe``; invoking it
//     with ``/uninstall`` reverses everything. The UNINSTALL does elevate:
//     it re-launches itself through "runas" before painting a frame, so
//     the machine-wide leftovers a long-lived install accumulates (HKLM
//     keys, all-users shortcuts, firewall rules) are actually removable
//     rather than best-effort no-ops. See ElevateForUninstall.
//
// Build: see build.md in this folder. In short, `cl` the .cpp + miniz.c +
// resources.res together with the manifest linked in.
// =============================================================================

#define _WIN32_WINNT 0x0A00        // Windows 10 baseline (works on 11 too)
#define WINVER       0x0A00
#define NOMINMAX
#include <windows.h>
#include <windowsx.h>
#include <shlobj.h>
#include <shlwapi.h>
#include <shobjidl.h>
#include <objidl.h>
#include <dwmapi.h>
#include <uxtheme.h>
#include <gdiplus.h>
#include <commctrl.h>
#include <tlhelp32.h>   // process enumeration for uninstaller kill-first
#include <propkey.h>    // PKEY_AppUserModel_ID for shortcut tagging
#include <knownfolders.h>  // FOLDERID_Profile — the per-user install root
#include <netfw.h>      // INetFwPolicy2 — firewall rules for the app path
#include <sddl.h>       // ConvertSidToStringSidW — who asked for the uninstall

#include <string>
#include <vector>
#include <atomic>
#include <thread>
#include <mutex>
#include <fstream>
#include <filesystem>
#include <chrono>

#include "miniz.h"

#pragma comment(lib, "user32.lib")
#pragma comment(lib, "gdi32.lib")
#pragma comment(lib, "gdiplus.lib")
#pragma comment(lib, "shell32.lib")
#pragma comment(lib, "shlwapi.lib")
#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "oleaut32.lib")
#pragma comment(lib, "dwmapi.lib")
#pragma comment(lib, "uxtheme.lib")
#pragma comment(lib, "advapi32.lib")
#pragma comment(lib, "comctl32.lib")
#pragma comment(lib, "propsys.lib")   // PKEY_AppUserModel_ID (shortcut tag)
#pragma comment(lib, "uuid.lib")      // IID_IShellLinkW / IID_IPersistFile / CLSID_ShellLink
#pragma comment(lib, "msimg32.lib")   // (legacy; no longer strictly needed)
// NOTE: common-controls v6 dependency is declared ONCE, in manifest.xml
// (fed to the linker via /MANIFESTINPUT). Declaring it a second time here
// via #pragma comment(linker, "/manifestdependency:...") caused mt.exe to
// merge two manifest fragments with conflicting attributes and fail with
// "c1010001: Values of attribute level not equal in different manifest
// snippets." One source of truth for the manifest — manifest.xml — avoids
// the whole class of merge conflicts.

namespace fs = std::filesystem;
using namespace Gdiplus;

// -----------------------------------------------------------------------------
// Constants — one place to tweak product identity
// -----------------------------------------------------------------------------
constexpr wchar_t APP_NAME[]       = L"LogicBoard";
constexpr wchar_t APP_PUBLISHER[]  = L"LogicClassroom";       // shown in Apps & Features
// Version lives in a macro, not just the constexpr array, because some
// UI strings paste it into an adjacent-literal concatenation
// (L"...v" APP_VERSION_TEXT L"...") and a constexpr array cannot be used
// that way. The array is derived from the macro so they cannot drift.
#define APP_VERSION_TEXT L"3.1.4"
constexpr wchar_t APP_VERSION[]    = APP_VERSION_TEXT;
constexpr wchar_t APP_EXE[]        = L"LogicBoard.exe";
// AppUserModelID — an internal, stable Windows grouping id (taskbar
// grouping, pinned-icon identity). NOT a user-visible company name.
// Renamed off the old "InnovaLogiX" token per the branding change.
constexpr wchar_t APP_AUMID[]      = L"YashJain.LogicBoard";
// Resource names — must match resources.rc.
constexpr wchar_t RC_ZIP_NAME[]    = L"LOGICBOARD_ZIP";
// (The ZIP's resource TYPE is RT_RCDATA; we pass MAKEINTRESOURCEW(10)
// inline at the FindResource call sites rather than storing a string,
// since FindResourceW wants the integer resource-type atom there.)
// Fixed install destination — no path picker.
constexpr wchar_t INSTALL_SUBDIR[] = L"LogicBoard";

// -----------------------------------------------------------------------------
// Where LogicBoard lives
//
//     %USERPROFILE%\LogicBoard\
//         LogicBoard.exe
//         _internal\
//         logo.ico
//         Uninstaller.exe
//
// PER-USER, and FLAT. Both halves are deliberate, and both are changes from
// the previous layout (C:\Program Files\LogicBoard\LogicBoard_V<ver>\).
//
// PER-USER, because nothing here needs to be machine-wide. Program Files
// forced the ENTIRE installer to run elevated, which meant every routine
// update asked a teacher for administrator credentials they may well not
// have on a school OPS panel. A folder inside the user's own profile needs
// no rights at all, so an update becomes a double-click.
//
// FLAT — no version folder — because to Windows Firewall the exe's PATH is
// its identity. A firewall rule records the program path it was created for,
// so moving the exe into a new folder each release made every release look
// like a program that had never been allowed through the firewall. That is
// the whole explanation for the "allow network access?" prompt on the
// 3.0.0 -> 3.1.0 update, and for its absence across the many 3.0.0 -> 3.0.0
// reinstalls before it: those landed on the same path, so the existing rule
// still matched. A stable path keeps one approval valid for every future
// update. It costs nothing now: the version folder existed for the old
// side-by-side ZIP updater, and that updater is gone — an update runs THIS
// installer, which stops LogicBoard and replaces the folder in place.
//
// %LOCALAPPDATA%\LogicBoard is a DIFFERENT directory and nothing here
// touches it. It holds saved boards, settings and the LogicShare config, and
// it deliberately outlives every install, including an uninstall.
// -----------------------------------------------------------------------------
static fs::path UserProfileDir() {
    PWSTR raw = nullptr;
    // FOLDERID_Profile rather than %USERPROFILE%: the environment variable
    // can be missing or stale (a service context, a runas, a roamed profile),
    // while the known-folder API asks the shell where this token's profile
    // actually is. Resolves per user, with no account name written down
    // anywhere in this binary.
    if (SUCCEEDED(SHGetKnownFolderPath(FOLDERID_Profile, 0, nullptr, &raw))
            && raw) {
        fs::path p = raw;
        CoTaskMemFree(raw);
        return p;
    }
    if (raw) CoTaskMemFree(raw);
    wchar_t buf[MAX_PATH] = {};
    DWORD n = GetEnvironmentVariableW(L"USERPROFILE", buf, MAX_PATH);
    return (n > 0 && n < MAX_PATH) ? fs::path(buf) : fs::path();
}

static fs::path InstallDir() {
    fs::path home = UserProfileDir();
    return home.empty() ? fs::path() : home / INSTALL_SUBDIR;
}

// The machine-wide location previous versions used. Only ever read from and
// deleted — never installed into again.
static fs::path LegacyProgramFilesDir() {
    wchar_t progFiles[MAX_PATH] = {};
    if (FAILED(SHGetFolderPathW(nullptr, CSIDL_PROGRAM_FILES, nullptr, 0,
                                 progFiles)))
        return fs::path();
    return fs::path(progFiles) / INSTALL_SUBDIR;
}

// Uninstall registry key path. Written under HKCU now (a per-user install
// has no business in the machine-wide list); the same subpath under HKLM is
// what a legacy install left behind, and is read + deleted, never written.
constexpr wchar_t UNINSTALL_KEY[]  =
    L"Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\LogicBoard";

constexpr wchar_t APP_PATHS_KEY[]  =
    L"Software\\Microsoft\\Windows\\CurrentVersion\\App Paths\\LogicBoard.exe";

// Startup registry value — HKCU Run, so LogicBoard launches at login for
// THIS user without needing rights it would not otherwise ask for. Value
// name is the product name; value data is the quoted exe path plus
// --background.
constexpr wchar_t RUN_KEY[]        =
    L"Software\\Microsoft\\Windows\\CurrentVersion\\Run";
constexpr wchar_t RUN_VALUE_NAME[] = L"LogicBoard";

// Argument that boots LogicBoard headless (LogicShare service only).
constexpr wchar_t STARTUP_ARGS[]   = L"--background";

// Internal switch: re-launch of ourselves, elevated, to remove a previous
// machine-wide install. Never typed by a user.
constexpr wchar_t REMOVE_LEGACY_SWITCH[] = L"--remove-legacy";

// Internal switches for the elevated uninstall re-launch. ELEVATED_SWITCH is
// the re-entry guard: without it, a machine where the consent prompt somehow
// returns an unelevated token would relaunch itself forever.
//
// USER_SID_SWITCH carries the SID of the account that ASKED for the
// uninstall. It matters because "elevated" and "the same user" are not the
// same thing: when a standard user runs this and types an administrator's
// credentials into the prompt, the elevated copy is that ADMINISTRATOR —
// different HKCU, different Desktop, different Start Menu. Cleaning up
// "the current user" there would wipe the wrong profile's shortcuts and
// leave the real ones listed in Apps & Features forever.
constexpr wchar_t ELEVATED_SWITCH[] = L"--elevated";
constexpr wchar_t USER_SID_SWITCH[] = L"--user-sid:";

// Exit codes the unelevated launcher returns when it never got as far as
// starting an uninstall. 1223 is ERROR_CANCELLED, which is what a caller
// inspecting the process would expect for "the user said no".
constexpr int UNINST_EXIT_DECLINED = 1223;
constexpr int UNINST_EXIT_NO_ELEV  = 1;

// Set from the command line in wWinMain, read by the uninstall worker.
static std::wstring g_invokingUserSid;

// Window sizing (design units — DPI-scaled at runtime).
constexpr int WIN_W = 720;
constexpr int WIN_H = 500;

// -----------------------------------------------------------------------------
// Existing-install detection
//
// The same binary is BOTH the first-time installer and the updater — the
// install pipeline is identical either way (wipe the old version folders,
// lay the new one down, re-point every shortcut and registry value). What
// changes is only what we TELL the user: showing "Install LogicBoard" to
// someone who already has 3.0.0 on the machine reads as "this will give you
// a second copy", which is the one thing it will not do.
//
// The probe is deliberately two-tiered:
//   1. HKLM Uninstall\LogicBoard — the authoritative record, and the only
//      place the previously-installed VERSION is written down.
//   2. A LogicBoard.exe actually present under %ProgramFiles%\LogicBoard.
//      Registry keys survive a hand-deleted install folder and vice versa,
//      so trusting either one alone gets the label wrong in exactly the
//      situations where a user is most likely to be reinstalling.
// Either signal alone counts as "installed"; the version string is
// best-effort and may come back empty, which the UI handles.
// -----------------------------------------------------------------------------
struct ExistingInstall {
    bool         present = false;
    std::wstring version;        // may be empty even when present
    std::wstring location;       // may be empty even when present
};

static ExistingInstall g_existing;   // per-user install (%USERPROFILE%)
static ExistingInstall g_legacy;     // machine-wide leftover (Program Files)

// Read a REG_SZ value into a wstring. Returns empty on any failure — every
// caller here treats "couldn't read it" and "wasn't there" identically.
static std::wstring RegReadString(HKEY root, const wchar_t* subkey,
                                    const wchar_t* value) {
    HKEY hKey = nullptr;
    // KEY_WOW64_64KEY: the installer is a 64-bit binary so this is already
    // the natural view, but being explicit costs nothing and makes the
    // lookup immune to ever being built 32-bit (where it would silently
    // read the WOW6432Node mirror and find nothing).
    if (RegOpenKeyExW(root, subkey, 0, KEY_READ | KEY_WOW64_64KEY, &hKey)
            != ERROR_SUCCESS)
        return L"";
    wchar_t buf[512] = {};
    DWORD cb = sizeof(buf);
    DWORD type = 0;
    LONG r = RegQueryValueExW(hKey, value, nullptr, &type,
                               reinterpret_cast<BYTE*>(buf), &cb);
    RegCloseKey(hKey);
    if (r != ERROR_SUCCESS || (type != REG_SZ && type != REG_EXPAND_SZ))
        return L"";
    // RegQueryValueExW does not guarantee a terminator; force one.
    buf[(sizeof(buf) / sizeof(buf[0])) - 1] = L'\0';
    return std::wstring(buf);
}

// Does ``root`` hold a LogicBoard install — at its root, or one folder
// deep in a ``LogicBoard_V*`` version folder from the old layout?
// Fills ``version`` from the folder name when the registry had none.
static bool FindExeUnder(const fs::path& root, std::wstring& version) {
    std::error_code ec;
    if (root.empty() || !fs::exists(root, ec)) return false;
    if (fs::exists(root / APP_EXE, ec)) return true;
    for (auto& entry : fs::directory_iterator(root, ec)) {
        if (ec) break;
        if (!entry.is_directory(ec)) continue;
        if (!fs::exists(entry.path() / APP_EXE, ec)) continue;
        // "LogicBoard_V3.0.0" — recover the version from the folder name
        // when the registry didn't have one (hand-cleaned registry, or an
        // install predating the Uninstall key).
        if (version.empty()) {
            std::wstring leaf = entry.path().filename().wstring();
            std::wstring prefix = std::wstring(APP_NAME) + L"_V";
            if (leaf.size() > prefix.size() &&
                leaf.compare(0, prefix.size(), prefix) == 0)
                version = leaf.substr(prefix.size());
        }
        return true;
    }
    return false;
}

// The per-user install this installer owns: %USERPROFILE%\LogicBoard.
static ExistingInstall DetectPerUserInstall() {
    ExistingInstall e;
    e.version  = RegReadString(HKEY_CURRENT_USER, UNINSTALL_KEY,
                                L"DisplayVersion");
    e.location = RegReadString(HKEY_CURRENT_USER, UNINSTALL_KEY,
                                L"InstallLocation");
    fs::path dir = InstallDir();
    bool onDisk = FindExeUnder(dir, e.version);
    // The registry alone is not proof: a hand-deleted folder leaves the key
    // behind. The FILES are what an update actually replaces, so they decide,
    // and the registry only supplies the version string.
    if (onDisk) {
        e.present = true;
        if (e.location.empty()) e.location = dir.wstring();
    }
    return e;
}

// A previous machine-wide install in Program Files. This is the one that
// needs administrator rights to remove, and the only reason this installer
// ever elevates.
static ExistingInstall DetectLegacyInstall() {
    ExistingInstall e;
    e.version  = RegReadString(HKEY_LOCAL_MACHINE, UNINSTALL_KEY,
                                L"DisplayVersion");
    e.location = RegReadString(HKEY_LOCAL_MACHINE, UNINSTALL_KEY,
                                L"InstallLocation");
    fs::path dir = LegacyProgramFilesDir();
    bool onDisk = FindExeUnder(dir, e.version);
    // Either signal counts here, unlike the per-user check above: a stray
    // HKLM Uninstall key pointing into Program Files is itself something
    // that has to be cleaned up, files or no files, because Apps & Features
    // would otherwise keep listing a LogicBoard that cannot be uninstalled.
    if (onDisk || !e.version.empty() || !e.location.empty()) {
        e.present = true;
        if (e.location.empty()) e.location = dir.wstring();
    }
    return e;
}

// True when this run is an upgrade/reinstall rather than a first install.
// Every piece of user-facing copy branches on exactly this. A machine-wide
// leftover counts: from the user's side that IS "LogicBoard is already here".
static inline bool IsUpdateMode() {
    return g_existing.present || g_legacy.present;
}

// "Install LogicBoard" / "Update LogicBoard" — the primary button, and the
// single most important word on the Welcome screen.
static std::wstring PrimaryActionLabel() {
    return IsUpdateMode() ? L"Update LogicBoard" : L"Install LogicBoard";
}

// -----------------------------------------------------------------------------
// Screen state
// -----------------------------------------------------------------------------
enum class Screen {
    Welcome, Installing, Done, Error,
    // Uninstaller screens — reuse the same paint pipeline / Aero glass
    // window instead of the plain MessageBox we had before, so the
    // uninstall UX matches the install UX.
    UninstConfirm, UninstWorking, UninstDone
};

// Shared installer state — read from paint thread, mutated by install thread.
struct InstallerCtx {
    HWND         hwnd            = nullptr;
    // ``screen`` is written by worker threads (install / uninstall) and
    // read by the UI thread in WM_PAINT / WM_LBUTTONDOWN, so it must be
    // atomic to avoid a data race. Screen is a simple enum so it's
    // trivially atomic-friendly.
    std::atomic<Screen> screen   { Screen::Welcome };
    std::atomic<int> progress    { 0 };    // 0..100
    std::mutex   textLock;
    std::wstring statusText      = L"Preparing...";
    std::wstring errorText;
    Bitmap*      logo            = nullptr;

    // Hit-box rectangles filled at paint time so WM_LBUTTONDOWN can
    // reverse-lookup which button was tapped without a control tree.
    RECT rcNext        = {};
    RECT rcLaunch      = {};
    RECT rcClose       = {};
    RECT rcCloseBtn    = {};    // the [Close] button on Done / Error screen
    // Uninstaller confirm screen — Yes / No buttons.
    RECT rcUninstYes   = {};
    RECT rcUninstNo    = {};
    // Uninstall-done "Close" button.
    RECT rcUninstDone  = {};
};

static InstallerCtx g_ctx;
static ULONG_PTR    g_gdiplusToken = 0;
static int          g_dpi          = 96;

static void OnPaint(HWND hwnd);   // fwd decl

// Convenience accessors for the atomic screen — keeps call sites readable
// and guarantees every read/write goes through the atomic.
static inline Screen CurScreen()            { return g_ctx.screen.load(); }
static inline void   SetScreen(Screen s)    { g_ctx.screen.store(s); }

// Trigger a repaint from anywhere. InvalidateRect is thread-safe (it just
// marks the window dirty; WM_PAINT then fires on the UI thread), so worker
// threads can call this directly.
static void RequestRepaint() {
    if (g_ctx.hwnd) InvalidateRect(g_ctx.hwnd, nullptr, FALSE);
}

// Thread-safe status text setter.
static void SetStatus(const std::wstring& s) {
    std::lock_guard<std::mutex> lk(g_ctx.textLock);
    g_ctx.statusText = s;
    RequestRepaint();
}

// -----------------------------------------------------------------------------
// DPI helpers
// -----------------------------------------------------------------------------
static int Scale(int n) { return MulDiv(n, g_dpi, 96); }

// Font sizes need the same DPI treatment as everything else, and this is
// the function that was missing.
//
// Every Font in this file is constructed with UnitPixel, which means the
// size is in DEVICE pixels and Windows applies no scaling of its own. The
// layout, meanwhile, goes through Scale(). So on a 4K board at 200% the
// panels, buttons and spacing all doubled while the text stayed at its
// literal pixel size — the installer looked correctly proportioned with
// unreadably small captions inside it. That is exactly the "UI scales but
// text doesn't" report.
//
// Kept as a separate float overload rather than reusing Scale(int): font
// sizes are fractional (12.5f, 13.5f) and rounding them to whole pixels
// before scaling visibly coarsens the type at 100%.
static REAL ScaleF(REAL v) {
    return v * static_cast<REAL>(g_dpi) / 96.0f;
}

// -----------------------------------------------------------------------------
// Aero glass — set up the translucent window using DWM composition
// -----------------------------------------------------------------------------
// DWMWA constants that the Win10 SDK headers may not define. We define
// them defensively so the installer compiles against older SDKs and still
// calls the modern backdrop APIs at runtime (which no-op on OSes that
// don't support them).
#ifndef DWMWA_SYSTEMBACKDROP_TYPE
#define DWMWA_SYSTEMBACKDROP_TYPE 38
#endif
#ifndef DWMWA_WINDOW_CORNER_PREFERENCE
#define DWMWA_WINDOW_CORNER_PREFERENCE 33
#endif
#ifndef DWMWA_USE_IMMERSIVE_DARK_MODE
#define DWMWA_USE_IMMERSIVE_DARK_MODE 20
#endif

static void EnableWindowShadow(HWND hwnd) {
    // Plain opaque window — no transparency, no Aero glass. We just add a
    // standard drop shadow so the frameless window has a subtle edge
    // against the desktop instead of looking flat. Extending the frame by
    // a hair on all sides is the lightweight way to make DWM render the
    // window's drop shadow for a WS_POPUP window.
    MARGINS m = { 1, 1, 1, 1 };
    DwmExtendFrameIntoClientArea(hwnd, &m);
}

// -----------------------------------------------------------------------------
// Load the embedded logo resource as a GDI+ Bitmap
// -----------------------------------------------------------------------------
static Bitmap* LoadEmbeddedIconAsBitmap() {
    // Load from RCDATA id 102 — the RAW .ico file bytes we deliberately
    // embedded for exactly this purpose. GDI+ can decode a full .ico
    // container straight from a memory stream and auto-selects the best
    // frame, giving us a clean premultiplied-alpha bitmap for drawing.
    //
    // Why not the "100 ICON" resource? Because the RC compiler does NOT
    // store `100 ICON "logo.ico"` as a single RT_ICON under id 100 — it
    // splits it into an RT_GROUP_ICON directory (id 100) plus one RT_ICON
    // per image at compiler-assigned ids. So FindResource(101, RT_ICON)
    // never matched anything, and the whole old code path silently fell
    // through to a LoadImage that could also miss. Going straight to the
    // raw RCDATA bytes sidesteps all of that.
    HMODULE self = GetModuleHandleW(nullptr);
    HRSRC hRes = FindResourceW(self, MAKEINTRESOURCEW(102),
                                MAKEINTRESOURCEW(10) /*RT_RCDATA*/);
    if (!hRes) return nullptr;
    HGLOBAL hMem = LoadResource(self, hRes);
    DWORD size = SizeofResource(self, hRes);
    void* data = LockResource(hMem);
    if (!data || size == 0) return nullptr;

    // Copy the resource bytes into a movable HGLOBAL that we hand to an
    // IStream. GDI+ requires a real stream; it doesn't take a raw ptr.
    HGLOBAL hCopy = GlobalAlloc(GMEM_MOVEABLE, size);
    if (!hCopy) return nullptr;
    void* dst = GlobalLock(hCopy);
    if (!dst) { GlobalFree(hCopy); return nullptr; }
    memcpy(dst, data, size);
    GlobalUnlock(hCopy);

    IStream* stream = nullptr;
    // TRUE = the stream owns hCopy and frees it on release.
    if (CreateStreamOnHGlobal(hCopy, TRUE, &stream) != S_OK) {
        GlobalFree(hCopy);
        return nullptr;
    }

    Bitmap* bmp = Bitmap::FromStream(stream);
    stream->Release();   // also frees hCopy (we passed TRUE)

    if (bmp && bmp->GetLastStatus() != Ok) {
        delete bmp;
        return nullptr;
    }
    return bmp;
}

// -----------------------------------------------------------------------------
// Drawing primitives
// -----------------------------------------------------------------------------
static GraphicsPath* RoundedRectPath(const Rect& r, int radius) {
    auto* p = new GraphicsPath();
    int d = radius * 2;
    p->AddArc(r.X,           r.Y,          d, d, 180, 90);
    p->AddArc(r.X+r.Width-d, r.Y,          d, d, 270, 90);
    p->AddArc(r.X+r.Width-d, r.Y+r.Height-d, d, d,   0, 90);
    p->AddArc(r.X,           r.Y+r.Height-d, d, d,  90, 90);
    p->CloseFigure();
    return p;
}

static void FillRoundedRect(Graphics& g, const Rect& r, int radius, Color color) {
    std::unique_ptr<GraphicsPath> p(RoundedRectPath(r, radius));
    SolidBrush brush(color);
    g.FillPath(&brush, p.get());
}

static void DrawRoundedBorder(Graphics& g, const Rect& r, int radius,
                                Color color, float width) {
    std::unique_ptr<GraphicsPath> p(RoundedRectPath(r, radius));
    Pen pen(color, width);
    g.DrawPath(&pen, p.get());
}

// Text draw helper — the surface uses AntiAliasGridFit (set in OnPaint)
// because ClearType can't render onto a transparent alpha surface without
// black fringing. This helper just draws; the hint is set on the Graphics.
static void DrawTextGdi(Graphics& g, const std::wstring& text,
                      const RectF& box, const Font& font, Color color,
                      StringAlignment horiz = StringAlignmentNear,
                      StringAlignment vert  = StringAlignmentNear) {
    SolidBrush brush(color);
    StringFormat fmt;
    fmt.SetAlignment(horiz);
    fmt.SetLineAlignment(vert);
    fmt.SetTrimming(StringTrimmingEllipsisCharacter);
    g.DrawString(text.c_str(), -1, &font, box, &fmt, &brush);
}

// Rounded button — filled variant (primary action).
static void DrawButtonFilled(Graphics& g, const RECT& rc,
                              const std::wstring& label,
                              const Font& font, Color fill, Color textColor) {
    Rect r(rc.left, rc.top, rc.right - rc.left, rc.bottom - rc.top);
    FillRoundedRect(g, r, Scale(12), fill);
    RectF tbox(static_cast<REAL>(rc.left), static_cast<REAL>(rc.top),
                 static_cast<REAL>(rc.right - rc.left),
                 static_cast<REAL>(rc.bottom - rc.top));
    DrawTextGdi(g, label, tbox, font, textColor,
              StringAlignmentCenter, StringAlignmentCenter);
}

// Rounded button — outlined variant (secondary action).
static void DrawButtonOutlined(Graphics& g, const RECT& rc,
                                const std::wstring& label,
                                const Font& font, Color border, Color textColor) {
    Rect r(rc.left, rc.top, rc.right - rc.left, rc.bottom - rc.top);
    // Very faint fill so the button reads as a solid target even when
    // it's transparent-ish against the acrylic backdrop.
    FillRoundedRect(g, r, Scale(12), Color(30, 255, 255, 255));
    DrawRoundedBorder(g, r, Scale(12), border, 1.2f);
    RectF tbox(static_cast<REAL>(rc.left), static_cast<REAL>(rc.top),
                 static_cast<REAL>(rc.right - rc.left),
                 static_cast<REAL>(rc.bottom - rc.top));
    DrawTextGdi(g, label, tbox, font, textColor,
              StringAlignmentCenter, StringAlignmentCenter);
}

// -----------------------------------------------------------------------------
// Screen painters
// -----------------------------------------------------------------------------
// Fully-opaque palette — the window is a plain solid window now, so every
// colour has alpha 255. One place to tweak the whole visual identity.
static const Color kBgRight  (255,  24,  26,  32); // right pane background
static const Color kBgLeft   (255,  16,  18,  24); // left pane (logo) bg
static const Color kBgBar    (255,  20,  22,  28); // bottom button bar
static const Color kStroke   (255,  44,  48,  58); // hairline separators
static const Color kTextPri  (255, 245, 246, 250); // primary text
static const Color kTextSec  (255, 165, 172, 186); // secondary text
static const Color kTextDim  (255, 120, 126, 140); // dim / footnote text
static const Color kAccent   (255,  64, 176, 255); // vibrant sky-blue
static const Color kAccentHi (255,  92, 190, 255); // hover / press
static const Color kOnAccent (255,  12,  20,  30); // text on accent
static const Color kWarn     (255, 224, 168,  72); // warning amber
static const Color kCardFill (255,  30,  33,  40); // sub-card fill (progress)


// Fills the entire window with the solid base background + draws the small
// close [X] in the top-right. Every screen calls this first.
static void PaintBackground(Graphics& g, InstallerCtx* ctx, int w, int h) {
    // Solid fill — no transparency. This is the whole window background.
    SolidBrush bg(kBgRight);
    g.FillRectangle(&bg, 0, 0, w, h);

    // Close [X] top-right, 40x32 hit area.
    ctx->rcClose.right  = w - Scale(6);
    ctx->rcClose.left   = ctx->rcClose.right - Scale(40);
    ctx->rcClose.top    = Scale(6);
    ctx->rcClose.bottom = ctx->rcClose.top + Scale(32);
    Font fontClose(L"Segoe UI", ScaleF(13.0f), FontStyleRegular, UnitPixel);
    RectF cbox(static_cast<REAL>(ctx->rcClose.left),
                 static_cast<REAL>(ctx->rcClose.top),
                 static_cast<REAL>(ctx->rcClose.right - ctx->rcClose.left),
                 static_cast<REAL>(ctx->rcClose.bottom - ctx->rcClose.top));
    DrawTextGdi(g, L"\u00D7", cbox, fontClose, kTextSec,
              StringAlignmentCenter, StringAlignmentCenter);
}

// Small helper: draws a feature line with an accent bullet dot.
static void DrawFeatureLine(Graphics& g, const std::wstring& text,
                             int x, int y, int textW, const Font& font) {
    int dot = Scale(6);
    SolidBrush accent(kAccent);
    g.FillEllipse(&accent, x, y + Scale(6), dot, dot);
    RectF tbox(static_cast<REAL>(x + Scale(16)), static_cast<REAL>(y),
                static_cast<REAL>(textW - Scale(16)), static_cast<REAL>(Scale(24)));
    DrawTextGdi(g, text, tbox, font, kTextPri,
              StringAlignmentNear, StringAlignmentNear);
}

static void PaintWelcome(Graphics& g, InstallerCtx* ctx, int w, int h) {
    PaintBackground(g, ctx, w, h);

    // ── Two-pane split ────────────────────────────────────────────────
    int barH   = Scale(84);            // bottom button bar height
    int leftW  = Scale(300);           // left (logo) pane width
    int bodyH  = h - barH;             // height of the two panes

    // LEFT PANE — darker background, centred logo + wordmark.
    {
        SolidBrush leftBg(kBgLeft);
        g.FillRectangle(&leftBg, 0, 0, leftW, bodyH);
        // Thin separator between panes.
        SolidBrush sep(kStroke);
        g.FillRectangle(&sep, leftW, 0, 1, bodyH);

        int logoSz = Scale(150);
        if (ctx->logo) {
            int lx = (leftW - logoSz) / 2;
            int ly = (bodyH - logoSz) / 2 - Scale(30);
            g.DrawImage(ctx->logo, lx, ly, logoSz, logoSz);
        }
        // Wordmark under the logo.
        Font fontName(L"Segoe UI Semibold", ScaleF(26.0f), FontStyleRegular, UnitPixel);
        RectF nbox(0.0f, static_cast<REAL>((bodyH) / 2 + Scale(60)),
                     static_cast<REAL>(leftW), static_cast<REAL>(Scale(36)));
        DrawTextGdi(g, L"LogicBoard", nbox, fontName, kTextPri,
                  StringAlignmentCenter, StringAlignmentCenter);
        Font fontTag(L"Segoe UI", ScaleF(11.5f), FontStyleRegular, UnitPixel);
        RectF tgbox(0.0f, static_cast<REAL>((bodyH) / 2 + Scale(98)),
                      static_cast<REAL>(leftW), static_cast<REAL>(Scale(20)));
        DrawTextGdi(g, L"The smart classroom whiteboard", tgbox, fontTag,
                  kTextDim, StringAlignmentCenter, StringAlignmentCenter);
    }

    // RIGHT PANE — creator info + features + about.
    {
        int rx = leftW + Scale(36);     // right pane content x
        int rw = w - rx - Scale(36);    // right pane content width
        int y  = Scale(40);

        // Heading — the update path says so up front. Someone who already
        // has LogicBoard on the board should not have to infer from a
        // progress bar that this is an upgrade rather than a second copy.
        Font fontH1(L"Segoe UI Semibold", ScaleF(24.0f), FontStyleRegular, UnitPixel);
        RectF h1(static_cast<REAL>(rx), static_cast<REAL>(y),
                   static_cast<REAL>(rw), static_cast<REAL>(Scale(34)));
        DrawTextGdi(g,
                  IsUpdateMode() ? L"Update LogicBoard"
                                 : L"Welcome to LogicBoard",
                  h1, fontH1, kTextPri,
                  StringAlignmentNear, StringAlignmentNear);
        y += Scale(42);

        // Second line. On a first install this says what the app is; on
        // an update it is the far more useful "where you are -> where you are
        // going", which is the first question an update prompt has to answer.
        Font fontSub(L"Segoe UI", ScaleF(13.0f), FontStyleRegular, UnitPixel);
        RectF sb(static_cast<REAL>(rx), static_cast<REAL>(y),
                   static_cast<REAL>(rw), static_cast<REAL>(Scale(22)));
        std::wstring subLine;
        if (IsUpdateMode()) {
            if (g_existing.version.empty()) {
                subLine = std::wstring(L"Upgrading to v") + APP_VERSION;
            } else if (g_existing.version == APP_VERSION) {
                // The same version is already on disk, so this is a repair.
                // Calling it an upgrade would be a claim the user can check.
                subLine = std::wstring(L"Reinstalling v") + APP_VERSION;
            } else {
                subLine = L"v" + g_existing.version
                        + L"  \u2192  v" + APP_VERSION;
            }
        } else {
            subLine = L"A free, open-source whiteboard for classrooms";
        }
        DrawTextGdi(g, subLine,
                  sb, fontSub, kAccent, StringAlignmentNear, StringAlignmentNear);
        y += Scale(34);

        // About blurb.
        Font fontBody(L"Segoe UI", ScaleF(12.5f), FontStyleRegular, UnitPixel);
        RectF ab(static_cast<REAL>(rx), static_cast<REAL>(y),
                   static_cast<REAL>(rw), static_cast<REAL>(Scale(60)));
        DrawTextGdi(g,
            IsUpdateMode()
              ? L"This replaces the copy already on this PC with the latest "
                L"build. Your saved boards, settings and shortcuts stay "
                L"exactly where they are - only the app itself is swapped."
              : L"LogicBoard helps teachers run a modern classroom - drawing, "
                L"presenting, and sharing notes, all in one place and fully "
                L"offline. Free to use, share and improve.",
            ab, fontBody, kTextSec, StringAlignmentNear, StringAlignmentNear);
        y += Scale(72);

        // Features heading.
        Font fontFH(L"Segoe UI Semibold", ScaleF(13.5f), FontStyleRegular, UnitPixel);
        RectF fh(static_cast<REAL>(rx), static_cast<REAL>(y),
                   static_cast<REAL>(rw), static_cast<REAL>(Scale(22)));
        DrawTextGdi(g,
                  IsUpdateMode() ? L"What you get" : L"What's inside",
                  fh, fontFH, kTextPri,
                  StringAlignmentNear, StringAlignmentNear);
        y += Scale(30);

        // Feature list.
        Font fontFeat(L"Segoe UI", ScaleF(12.5f), FontStyleRegular, UnitPixel);
        const wchar_t* feats[] = {
            L"Smooth pen, highlighter & shape tools",
            L"Import PowerPoint & PDF slides as backgrounds",
            L"Multi-page boards with unlimited undo",
            L"LogicShare - send files across the classroom LAN",
            L"Palm rejection & stylus support for smart boards",
        };
        for (auto* f : feats) {
            DrawFeatureLine(g, f, rx, y, rw, fontFeat);
            y += Scale(28);
        }
    }

    // ── BOTTOM BAR — licence + Install button ─────────────────────────
    {
        SolidBrush barBg(kBgBar);
        g.FillRectangle(&barBg, 0, bodyH, w, barH);
        SolidBrush sep(kStroke);
        g.FillRectangle(&sep, 0, bodyH, w, 1);

        // The licence on the left.
        Font fontCredit(L"Segoe UI Semibold", ScaleF(12.0f), FontStyleRegular, UnitPixel);
        RectF cr(static_cast<REAL>(Scale(28)), static_cast<REAL>(bodyH),
                   static_cast<REAL>(Scale(300)), static_cast<REAL>(barH));
        DrawTextGdi(g, L"Open source  \u2022  GPL-3.0", cr, fontCredit, kTextSec,
                  StringAlignmentNear, StringAlignmentCenter);

        // Install button on the right.
        int btnW = Scale(200), btnH = Scale(48);
        ctx->rcNext.right  = w - Scale(28);
        ctx->rcNext.left   = ctx->rcNext.right - btnW;
        ctx->rcNext.top    = bodyH + (barH - btnH) / 2;
        ctx->rcNext.bottom = ctx->rcNext.top + btnH;
        Font fontBtn(L"Segoe UI Semibold", ScaleF(14.0f), FontStyleRegular, UnitPixel);
        DrawButtonFilled(g, ctx->rcNext, PrimaryActionLabel(),
                           fontBtn, kAccent, kOnAccent);
    }
}

static void PaintInstalling(Graphics& g, InstallerCtx* ctx, int w, int h) {
    PaintBackground(g, ctx, w, h);
    int logoSz = Scale(80);
    if (ctx->logo) {
        int x = (w - logoSz) / 2;
        int y = Scale(90);
        g.DrawImage(ctx->logo, x, y, logoSz, logoSz);
    }

    Font fontHero(L"Segoe UI Semibold", ScaleF(22.0f), FontStyleRegular, UnitPixel);
    RectF hbox(0.0f, static_cast<REAL>(Scale(190)),
                 static_cast<REAL>(w), static_cast<REAL>(Scale(40)));
    DrawTextGdi(g,
              IsUpdateMode() ? L"Updating LogicBoard..."
                             : L"Installing LogicBoard...",
              hbox, fontHero,
              kTextPri, StringAlignmentCenter, StringAlignmentCenter);

    // Status text (updated from the extract thread).
    std::wstring status;
    {
        std::lock_guard<std::mutex> lk(ctx->textLock);
        status = ctx->statusText;
    }
    Font fontStatus(L"Segoe UI", ScaleF(12.0f), FontStyleRegular, UnitPixel);
    RectF sbox(static_cast<REAL>(Scale(60)),
                 static_cast<REAL>(Scale(240)),
                 static_cast<REAL>(w - Scale(120)),
                 static_cast<REAL>(Scale(24)));
    DrawTextGdi(g, status, sbox, fontStatus, kTextSec,
              StringAlignmentCenter, StringAlignmentCenter);

    // Progress bar. Track + filled portion, both rounded.
    int barW = w - Scale(120);
    int barH = Scale(8);
    int barX = Scale(60);
    int barY = Scale(290);
    Rect track(barX, barY, barW, barH);
    FillRoundedRect(g, track, Scale(4), Color(80, 255, 255, 255));

    int pct = std::max(0, std::min(100, ctx->progress.load()));
    int filledW = (barW * pct) / 100;
    if (filledW > 0) {
        Rect filled(barX, barY, filledW, barH);
        FillRoundedRect(g, filled, Scale(4), kAccent);
    }

    // Percent readout.
    Font fontPct(L"Segoe UI Semibold", ScaleF(11.0f), FontStyleRegular, UnitPixel);
    wchar_t pctBuf[16];
    swprintf_s(pctBuf, L"%d%%", pct);
    RectF pbox(0.0f, static_cast<REAL>(barY + Scale(16)),
                 static_cast<REAL>(w), static_cast<REAL>(Scale(20)));
    DrawTextGdi(g, pctBuf, pbox, fontPct, kTextSec,
              StringAlignmentCenter, StringAlignmentCenter);

    // Footer hint.
    Font fontHint(L"Segoe UI", ScaleF(10.0f), FontStyleItalic, UnitPixel);
    RectF fbox(0.0f, static_cast<REAL>(h - Scale(50)),
                 static_cast<REAL>(w), static_cast<REAL>(Scale(20)));
    DrawTextGdi(g, L"Hang tight - this is quick",
              fbox, fontHint, kTextSec,
              StringAlignmentCenter, StringAlignmentCenter);
}

static void PaintDone(Graphics& g, InstallerCtx* ctx, int w, int h) {
    PaintBackground(g, ctx, w, h);
    int logoSz = Scale(120);
    if (ctx->logo) {
        int x = (w - logoSz) / 2;
        int y = Scale(70);
        g.DrawImage(ctx->logo, x, y, logoSz, logoSz);
    }

    // Check mark badge on the corner of the logo.
    Font fontCheck(L"Segoe UI Semibold", ScaleF(34.0f), FontStyleRegular, UnitPixel);
    int cx = (w + logoSz) / 2 - Scale(28);
    int cy = Scale(70) + logoSz - Scale(46);
    Rect badge(cx, cy, Scale(48), Scale(48));
    FillRoundedRect(g, badge, Scale(24), kAccent);
    RectF cbox(static_cast<REAL>(cx), static_cast<REAL>(cy),
                 static_cast<REAL>(Scale(48)),
                 static_cast<REAL>(Scale(48)));
    Font fontCheckSmall(L"Segoe UI Semibold", ScaleF(20.0f), FontStyleRegular, UnitPixel);
    DrawTextGdi(g, L"\u2713", cbox, fontCheckSmall, kOnAccent,
              StringAlignmentCenter, StringAlignmentCenter);

    Font fontHero(L"Segoe UI Semibold", ScaleF(26.0f), FontStyleRegular, UnitPixel);
    RectF hbox(0.0f, static_cast<REAL>(Scale(220)),
                 static_cast<REAL>(w), static_cast<REAL>(Scale(44)));
    DrawTextGdi(g, IsUpdateMode() ? L"Updated" : L"All done",
              hbox, fontHero, kTextPri,
              StringAlignmentCenter, StringAlignmentCenter);

    Font fontSub(L"Segoe UI", ScaleF(13.0f), FontStyleRegular, UnitPixel);
    RectF sbox(static_cast<REAL>(Scale(60)),
                 static_cast<REAL>(Scale(270)),
                 static_cast<REAL>(w - Scale(120)),
                 static_cast<REAL>(Scale(44)));
    DrawTextGdi(g,
              IsUpdateMode()
                ? L"LogicBoard is now on v" APP_VERSION_TEXT L".\nHit launch to open it now."
                : L"LogicBoard is installed and ready to go.\nHit launch to open it now.",
              sbox, fontSub, kTextSec,
              StringAlignmentCenter, StringAlignmentNear);

    // Buttons: [Close]  [Launch LogicBoard]
    int btnW = Scale(180), btnH = Scale(46);
    int gap = Scale(14);
    int totalW = btnW * 2 + gap;
    int startX = (w - totalW) / 2;
    int by = h - Scale(72);

    ctx->rcCloseBtn.left   = startX;
    ctx->rcCloseBtn.top    = by;
    ctx->rcCloseBtn.right  = startX + btnW;
    ctx->rcCloseBtn.bottom = by + btnH;

    ctx->rcLaunch.left   = startX + btnW + gap;
    ctx->rcLaunch.top    = by;
    ctx->rcLaunch.right  = ctx->rcLaunch.left + btnW;
    ctx->rcLaunch.bottom = by + btnH;

    Font fontBtn(L"Segoe UI Semibold", ScaleF(14.0f), FontStyleRegular, UnitPixel);
    DrawButtonOutlined(g, ctx->rcCloseBtn, L"Close",
                         fontBtn, kStroke, kTextPri);
    DrawButtonFilled(g, ctx->rcLaunch, L"Launch LogicBoard",
                       fontBtn, kAccent, kOnAccent);
}

static void PaintError(Graphics& g, InstallerCtx* ctx, int w, int h) {
    PaintBackground(g, ctx, w, h);
    Font fontHero(L"Segoe UI Semibold", ScaleF(24.0f), FontStyleRegular, UnitPixel);
    RectF hbox(0.0f, static_cast<REAL>(Scale(120)),
                 static_cast<REAL>(w), static_cast<REAL>(Scale(40)));
    DrawTextGdi(g, IsUpdateMode() ? L"Update failed" : L"Install failed",
              hbox, fontHero, kTextPri,
              StringAlignmentCenter, StringAlignmentCenter);

    std::wstring err;
    {
        std::lock_guard<std::mutex> lk(ctx->textLock);
        err = ctx->errorText;
    }
    Font fontSub(L"Segoe UI", ScaleF(12.0f), FontStyleRegular, UnitPixel);
    RectF sbox(static_cast<REAL>(Scale(60)),
                 static_cast<REAL>(Scale(180)),
                 static_cast<REAL>(w - Scale(120)),
                 static_cast<REAL>(Scale(140)));
    DrawTextGdi(g, err, sbox, fontSub, kWarn,
              StringAlignmentCenter, StringAlignmentNear);

    // Single Close button.
    int btnW = Scale(180), btnH = Scale(46);
    ctx->rcCloseBtn.left   = (w - btnW) / 2;
    ctx->rcCloseBtn.top    = h - Scale(72);
    ctx->rcCloseBtn.right  = ctx->rcCloseBtn.left + btnW;
    ctx->rcCloseBtn.bottom = ctx->rcCloseBtn.top  + btnH;
    Font fontBtn(L"Segoe UI Semibold", ScaleF(14.0f), FontStyleRegular, UnitPixel);
    DrawButtonFilled(g, ctx->rcCloseBtn, L"Close",
                       fontBtn, kAccent, kOnAccent);
}

// ---- Uninstaller screens — reuse the same content panel + card style. ----

static void PaintUninstConfirm(Graphics& g, InstallerCtx* ctx, int w, int h) {
    PaintBackground(g, ctx, w, h);
    int logoSz = Scale(96);
    if (ctx->logo) {
        int x = (w - logoSz) / 2;
        int y = Scale(90);
        g.DrawImage(ctx->logo, x, y, logoSz, logoSz);
    }
    Font fontHero(L"Segoe UI Semibold", ScaleF(24.0f), FontStyleRegular, UnitPixel);
    RectF hbox(0.0f, static_cast<REAL>(Scale(210)),
                 static_cast<REAL>(w), static_cast<REAL>(Scale(40)));
    DrawTextGdi(g, L"Uninstall LogicBoard?", hbox, fontHero,
              kTextPri, StringAlignmentCenter, StringAlignmentCenter);
    Font fontSub(L"Segoe UI", ScaleF(13.0f), FontStyleRegular, UnitPixel);
    RectF sbox(static_cast<REAL>(Scale(60)),
                 static_cast<REAL>(Scale(260)),
                 static_cast<REAL>(w - Scale(120)),
                 static_cast<REAL>(Scale(70)));
    DrawTextGdi(g,
        L"This will completely remove LogicBoard from your PC.\n"
        L"Your saved boards and personal settings will stay untouched.",
        sbox, fontSub, kTextSec,
        StringAlignmentCenter, StringAlignmentNear);

    // Buttons: [No, keep it]  [Yes, uninstall]
    int btnW = Scale(190), btnH = Scale(48);
    int gap = Scale(14);
    int totalW = btnW * 2 + gap;
    int startX = (w - totalW) / 2;
    int by = h - Scale(76);
    ctx->rcUninstNo.left   = startX;
    ctx->rcUninstNo.top    = by;
    ctx->rcUninstNo.right  = startX + btnW;
    ctx->rcUninstNo.bottom = by + btnH;
    ctx->rcUninstYes.left   = startX + btnW + gap;
    ctx->rcUninstYes.top    = by;
    ctx->rcUninstYes.right  = ctx->rcUninstYes.left + btnW;
    ctx->rcUninstYes.bottom = by + btnH;
    Font fontBtn(L"Segoe UI Semibold", ScaleF(14.0f), FontStyleRegular, UnitPixel);
    DrawButtonOutlined(g, ctx->rcUninstNo, L"No, keep it",
                         fontBtn, kStroke, kTextPri);
    DrawButtonFilled  (g, ctx->rcUninstYes, L"Yes, uninstall",
                         fontBtn, kAccent, kOnAccent);
}

static void PaintUninstWorking(Graphics& g, InstallerCtx* ctx, int w, int h) {
    PaintBackground(g, ctx, w, h);
    int logoSz = Scale(80);
    if (ctx->logo) {
        int x = (w - logoSz) / 2;
        int y = Scale(110);
        g.DrawImage(ctx->logo, x, y, logoSz, logoSz);
    }
    Font fontHero(L"Segoe UI Semibold", ScaleF(22.0f), FontStyleRegular, UnitPixel);
    RectF hbox(0.0f, static_cast<REAL>(Scale(210)),
                 static_cast<REAL>(w), static_cast<REAL>(Scale(40)));
    DrawTextGdi(g, L"Removing LogicBoard...", hbox, fontHero,
              kTextPri, StringAlignmentCenter, StringAlignmentCenter);
    std::wstring status;
    {
        std::lock_guard<std::mutex> lk(ctx->textLock);
        status = ctx->statusText;
    }
    Font fontStatus(L"Segoe UI", ScaleF(12.0f), FontStyleRegular, UnitPixel);
    RectF sbox(static_cast<REAL>(Scale(60)),
                 static_cast<REAL>(Scale(260)),
                 static_cast<REAL>(w - Scale(120)),
                 static_cast<REAL>(Scale(24)));
    DrawTextGdi(g, status, sbox, fontStatus, kTextSec,
              StringAlignmentCenter, StringAlignmentCenter);

    // Indeterminate-style progress bar — visual only, no percent readout
    // (steps are fast and not proportionally measurable).
    int barW = w - Scale(160);
    int barH = Scale(6);
    int barX = Scale(80);
    int barY = Scale(310);
    Rect track(barX, barY, barW, barH);
    FillRoundedRect(g, track, Scale(3), Color(60, 255, 255, 255));
    int pct = std::max(0, std::min(100, ctx->progress.load()));
    if (pct > 0) {
        Rect filled(barX, barY, (barW * pct) / 100, barH);
        FillRoundedRect(g, filled, Scale(3), kAccent);
    }
}

static void PaintUninstDone(Graphics& g, InstallerCtx* ctx, int w, int h) {
    PaintBackground(g, ctx, w, h);
    Font fontHero(L"Segoe UI Semibold", ScaleF(26.0f), FontStyleRegular, UnitPixel);
    RectF hbox(0.0f, static_cast<REAL>(Scale(150)),
                 static_cast<REAL>(w), static_cast<REAL>(Scale(44)));
    DrawTextGdi(g, L"Uninstalled", hbox, fontHero, kTextPri,
              StringAlignmentCenter, StringAlignmentCenter);
    Font fontSub(L"Segoe UI", ScaleF(13.0f), FontStyleRegular, UnitPixel);
    RectF sbox(static_cast<REAL>(Scale(60)),
                 static_cast<REAL>(Scale(210)),
                 static_cast<REAL>(w - Scale(120)),
                 static_cast<REAL>(Scale(50)));
    DrawTextGdi(g,
        L"LogicBoard has been removed from your PC.\nSee you next time.",
        sbox, fontSub, kTextSec,
        StringAlignmentCenter, StringAlignmentNear);

    // Optional warning if some files were locked.
    std::wstring extra;
    {
        std::lock_guard<std::mutex> lk(ctx->textLock);
        extra = ctx->errorText;
    }
    if (!extra.empty()) {
        Font fontWarn(L"Segoe UI", ScaleF(11.5f), FontStyleRegular, UnitPixel);
        RectF wbox(static_cast<REAL>(Scale(60)),
                     static_cast<REAL>(Scale(280)),
                     static_cast<REAL>(w - Scale(120)),
                     static_cast<REAL>(Scale(80)));
        DrawTextGdi(g, extra, wbox, fontWarn, kWarn,
                  StringAlignmentCenter, StringAlignmentNear);
    }

    int btnW = Scale(180), btnH = Scale(46);
    ctx->rcUninstDone.left   = (w - btnW) / 2;
    ctx->rcUninstDone.top    = h - Scale(72);
    ctx->rcUninstDone.right  = ctx->rcUninstDone.left + btnW;
    ctx->rcUninstDone.bottom = ctx->rcUninstDone.top  + btnH;
    Font fontBtn(L"Segoe UI Semibold", ScaleF(14.0f), FontStyleRegular, UnitPixel);
    DrawButtonFilled(g, ctx->rcUninstDone, L"Close",
                       fontBtn, kAccent, kOnAccent);
}

// -----------------------------------------------------------------------------
// OnPaint — simple opaque double-buffered painting.
//
// The window is a plain solid window (no transparency, no layered window,
// no Aero glass). We draw the current screen into an off-screen memory
// bitmap and BitBlt it to the window in one shot, which is flicker-free and
// completely free of the alpha/backing-rectangle problems the transparent
// approaches kept running into.
// -----------------------------------------------------------------------------
static void OnPaint(HWND hwnd) {
    PAINTSTRUCT ps;
    HDC hdc = BeginPaint(hwnd, &ps);
    RECT rc; GetClientRect(hwnd, &rc);
    int w = rc.right, h = rc.bottom;
    if (w <= 0 || h <= 0) { EndPaint(hwnd, &ps); return; }

    // Standard double-buffered opaque paint: draw everything into an
    // off-screen bitmap, then BitBlt it to the window in one shot (no
    // flicker). No alpha, no transparency, no layered-window trickery —
    // a plain solid window. This is bulletproof and looks clean.
    HDC memDC = CreateCompatibleDC(hdc);
    HBITMAP memBmp = CreateCompatibleBitmap(hdc, w, h);
    HGDIOBJ oldBmp = SelectObject(memDC, memBmp);

    {
        Graphics g(memDC);
        g.SetSmoothingMode(SmoothingModeAntiAlias);
        g.SetTextRenderingHint(TextRenderingHintClearTypeGridFit);
        g.SetInterpolationMode(InterpolationModeHighQualityBicubic);

        switch (CurScreen()) {
            case Screen::Welcome:       PaintWelcome        (g, &g_ctx, w, h); break;
            case Screen::Installing:    PaintInstalling     (g, &g_ctx, w, h); break;
            case Screen::Done:          PaintDone           (g, &g_ctx, w, h); break;
            case Screen::Error:         PaintError          (g, &g_ctx, w, h); break;
            case Screen::UninstConfirm: PaintUninstConfirm  (g, &g_ctx, w, h); break;
            case Screen::UninstWorking: PaintUninstWorking  (g, &g_ctx, w, h); break;
            case Screen::UninstDone:    PaintUninstDone     (g, &g_ctx, w, h); break;
        }
    }

    BitBlt(hdc, 0, 0, w, h, memDC, 0, 0, SRCCOPY);
    SelectObject(memDC, oldBmp);
    DeleteObject(memBmp);
    DeleteDC(memDC);
    EndPaint(hwnd, &ps);
}

// -----------------------------------------------------------------------------
// Installation logic — runs on a background thread, posts progress via
// the shared context. Marshals status text updates through RequestRepaint.
// -----------------------------------------------------------------------------

static bool ExtractEmbeddedZip(const fs::path& installDir,
                                 std::wstring& errOut) {
    // Locate the embedded ZIP resource.
    HRSRC hRes = FindResourceW(GetModuleHandleW(nullptr),
                                RC_ZIP_NAME, MAKEINTRESOURCEW(10) /*RT_RCDATA*/);
    if (!hRes) {
        errOut = L"The installer's payload is missing. This build was "
                  L"assembled incorrectly.";
        return false;
    }
    HGLOBAL hMem = LoadResource(GetModuleHandleW(nullptr), hRes);
    DWORD zipSize = SizeofResource(GetModuleHandleW(nullptr), hRes);
    void* zipData = LockResource(hMem);
    if (!zipData || zipSize == 0) {
        errOut = L"Couldn't load the bundled LogicBoard payload.";
        return false;
    }

    // Open the ZIP straight from the resource memory — no need to write
    // it to disk first.
    mz_zip_archive zip = {};
    if (!mz_zip_reader_init_mem(&zip, zipData, zipSize, 0)) {
        errOut = L"The bundled ZIP archive is corrupt.";
        return false;
    }

    mz_uint fileCount = mz_zip_reader_get_num_files(&zip);
    if (fileCount == 0) {
        mz_zip_reader_end(&zip);
        errOut = L"The bundled ZIP archive is empty.";
        return false;
    }

    // ---- First pass: detect a single common top-level folder. ----
    // If someone zipped the LogicBoard FOLDER (so every path looks like
    // "LogicBoard/LogicBoard.exe") rather than its CONTENTS, we'd end up
    // installing to %USERPROFILE%\LogicBoard\LogicBoard\... — a
    // double-nested mess where the exe isn't where the shortcuts point.
    // So we find the shared first path segment (if there is exactly one
    // across every entry) and strip it during extraction. If entries
    // have differing roots (already flat), commonRoot stays empty and we
    // extract as-is.
    std::string commonRoot;
    bool rootResolved = false;
    bool hasCommonRoot = true;
    for (mz_uint i = 0; i < fileCount && hasCommonRoot; ++i) {
        mz_zip_archive_file_stat st;
        if (!mz_zip_reader_file_stat(&zip, i, &st)) continue;
        std::string n(st.m_filename);
        for (auto& c : n) if (c == '\\') c = '/';
        // Isolate the first segment.
        size_t slash = n.find('/');
        std::string first = (slash == std::string::npos)
                             ? n : n.substr(0, slash);
        if (first.empty()) continue;
        // A file at the very root (no slash at all) means there is NO
        // single wrapping folder — bail out of the strip entirely.
        if (slash == std::string::npos) { hasCommonRoot = false; break; }
        if (!rootResolved) {
            commonRoot = first;
            rootResolved = true;
        } else if (first != commonRoot) {
            hasCommonRoot = false;
        }
    }
    // The prefix we strip from every entry (with trailing slash), or
    // empty if there's no single wrapping folder.
    std::string stripPrefix;
    if (hasCommonRoot && rootResolved && !commonRoot.empty())
        stripPrefix = commonRoot + "/";

    // Make sure the target directory exists.
    std::error_code ec;
    fs::create_directories(installDir, ec);
    if (ec) {
        // Not a rights problem any more — this is the user's own profile
        // folder. A disk that is full, read-only, or redirected to an
        // unreachable network share is what actually gets here.
        errOut = L"Couldn't create the install folder in your user profile. "
                  L"Check that the disk isn't full or write-protected.";
        mz_zip_reader_end(&zip);
        return false;
    }

    for (mz_uint i = 0; i < fileCount; ++i) {
        mz_zip_archive_file_stat st;
        if (!mz_zip_reader_file_stat(&zip, i, &st)) continue;

        // Normalise to forward slashes first for the strip comparison.
        std::string name(st.m_filename);
        for (auto& c : name) if (c == '\\') c = '/';

        // Strip the detected common root folder, if any.
        if (!stripPrefix.empty() &&
            name.size() >= stripPrefix.size() &&
            name.compare(0, stripPrefix.size(), stripPrefix) == 0) {
            name = name.substr(stripPrefix.size());
        }
        // If stripping left an empty string (the root folder's own dir
        // entry), skip it — there's nothing to create.
        if (name.empty()) continue;

        // Now to Windows separators for the filesystem call.
        for (auto& c : name) if (c == '/') c = '\\';

        // Convert to wstring for the filesystem call.
        int wlen = MultiByteToWideChar(CP_UTF8, 0, name.c_str(), -1,
                                         nullptr, 0);
        std::wstring wname(wlen, L'\0');
        MultiByteToWideChar(CP_UTF8, 0, name.c_str(), -1,
                             wname.data(), wlen);
        if (!wname.empty() && wname.back() == L'\0') wname.pop_back();

        fs::path outPath = installDir / wname;

        if (st.m_is_directory) {
            fs::create_directories(outPath, ec);
            continue;
        }

        // Ensure parent dir exists.
        fs::create_directories(outPath.parent_path(), ec);

        // Extract to memory, then write with std::ofstream. We deliberately
        // do NOT use mz_zip_reader_extract_to_file — that path routes
        // through miniz's internal fopen, which on Windows uses the ANSI
        // code page and can't reliably open wide / non-ASCII paths. Going
        // through a memory buffer + ofstream(fs::path) uses the wide
        // filesystem API and sidesteps that entirely.
        size_t outSize = 0;
        void* p = mz_zip_reader_extract_to_heap(&zip, i, &outSize, 0);
        if (!p) {
            errOut = L"Extraction failed part-way through "
                      L"(couldn't decompress a file). The archive may be "
                      L"corrupt.";
            mz_zip_reader_end(&zip);
            return false;
        }
        {
            std::ofstream f(outPath, std::ios::binary | std::ios::trunc);
            if (!f) {
                mz_free(p);
                // Order matters: a locked file is by far the common case
                // (LogicBoard still running, or an antivirus holding the
                // exe open mid-scan), and this is the user's own profile
                // folder, so "run as administrator" is not the answer and
                // reads as a dead end.
                errOut = L"Couldn't write \"" + fs::path(wname).filename().wstring() +
                          L"\" to the install folder.\n\n"
                          L"The file is most likely in use. Close LogicBoard "
                          L"completely — including the icon in the system "
                          L"tray — and run this installer again. If it keeps "
                          L"failing, check that your antivirus isn't holding "
                          L"the folder, or that the disk isn't write-protected.";
                mz_zip_reader_end(&zip);
                return false;
            }
            f.write(static_cast<const char*>(p), static_cast<std::streamsize>(outSize));
            if (!f.good()) {
                f.close();
                mz_free(p);
                errOut = L"A file couldn't be fully written — the disk "
                          L"may be full.";
                mz_zip_reader_end(&zip);
                return false;
            }
        }
        mz_free(p);

        // Progress: file i+1 of fileCount, mapped to 10..90 percent
        // (leaving room at the ends for setup + registry steps).
        int pct = 10 + (i + 1) * 80 / fileCount;
        g_ctx.progress.store(pct);
        if ((i % 4) == 0) {
            // Only update the status text once every few files —
            // hammering it every file just chews CPU.
            std::wstring cur = L"Extracting " + fs::path(wname).filename().wstring();
            SetStatus(cur);
        }
        RequestRepaint();
    }

    mz_zip_reader_end(&zip);
    return true;
}

static bool CreateShellShortcut(const fs::path& shortcutPath,
                                  const fs::path& targetExe,
                                  const fs::path& iconPath,
                                  const std::wstring& description,
                                  const std::wstring& arguments = L"") {
    // Uses IShellLink + IPersistFile — the standard Windows shortcut
    // creation dance, works on every version since XP.
    IShellLinkW* psl = nullptr;
    HRESULT hr = CoCreateInstance(
        CLSID_ShellLink, nullptr, CLSCTX_INPROC_SERVER,
        IID_IShellLinkW, reinterpret_cast<void**>(&psl));
    if (FAILED(hr)) return false;

    psl->SetPath(targetExe.wstring().c_str());
    psl->SetWorkingDirectory(targetExe.parent_path().wstring().c_str());
    psl->SetDescription(description.c_str());
    psl->SetIconLocation(iconPath.wstring().c_str(), 0);
    psl->SetShowCmd(SW_SHOWNORMAL);
    if (!arguments.empty())
        psl->SetArguments(arguments.c_str());

    // Tag the shortcut with the AppUserModelID so Windows groups its
    // taskbar button correctly + pinned icons survive updates.
    IPropertyStore* pps = nullptr;
    hr = psl->QueryInterface(IID_PPV_ARGS(&pps));
    if (SUCCEEDED(hr)) {
        PROPVARIANT pv;
        PropVariantInit(&pv);
        pv.vt = VT_LPWSTR;
        pv.pwszVal = const_cast<LPWSTR>(APP_AUMID);
        // PKEY_AppUserModel_ID comes from <propkey.h>.
        pps->SetValue(PKEY_AppUserModel_ID, pv);
        pps->Commit();
        pps->Release();
    }

    IPersistFile* ppf = nullptr;
    hr = psl->QueryInterface(IID_IPersistFile,
                              reinterpret_cast<void**>(&ppf));
    bool ok = false;
    if (SUCCEEDED(hr)) {
        hr = ppf->Save(shortcutPath.wstring().c_str(), TRUE);
        ok = SUCCEEDED(hr);
        ppf->Release();
    }
    psl->Release();
    return ok;
}

// -----------------------------------------------------------------------------
// Path comparison + idempotent shortcuts
//
// The duplicate desktop icon came from two pieces of code both being certain
// they owned the shortcut. This installer wrote one to the ALL-USERS desktop
// and deleted the per-user copy; LogicBoard itself, on its next launch,
// re-registered and wrote a PER-USER copy back. Explorer merges the two
// desktops into the one you look at, so the result was two identical icons —
// and re-running either half put them straight back.
//
// The fix is on both sides. LogicBoard now registers per-user only (see
// ui/util/windows_registration.py), matching this install, and everything
// here goes through EnsureShortcut: one known filename per location, left
// untouched when it is already correct, rewritten IN PLACE when it points
// somewhere stale. Neither path can ever produce a second icon.
// -----------------------------------------------------------------------------
static bool SamePath(const fs::path& a, const fs::path& b) {
    if (a.empty() || b.empty()) return false;
    std::wstring x = a.lexically_normal().wstring();
    std::wstring y = b.lexically_normal().wstring();
    auto trim = [](std::wstring& p) {
        while (!p.empty() && (p.back() == L'\\' || p.back() == L'/'))
            p.pop_back();
    };
    trim(x); trim(y);
    return _wcsicmp(x.c_str(), y.c_str()) == 0;
}

// Is ``child`` inside ``parent``? Used to decide whether a shortcut or a
// firewall rule belongs to the installation we are replacing.
static bool PathIsUnder(const fs::path& child, const fs::path& parent) {
    if (child.empty() || parent.empty()) return false;
    std::wstring c = child.lexically_normal().wstring();
    std::wstring p = parent.lexically_normal().wstring();
    while (!p.empty() && (p.back() == L'\\' || p.back() == L'/')) p.pop_back();
    if (c.size() <= p.size()) return false;
    if (_wcsnicmp(c.c_str(), p.c_str(), p.size()) != 0) return false;
    return c[p.size()] == L'\\' || c[p.size()] == L'/';
}

struct ShortcutInfo {
    bool         readable = false;
    std::wstring target;
    std::wstring args;
};

// Read an existing .lnk. A shortcut we cannot read is treated as one we
// cannot trust, and gets rewritten.
static ShortcutInfo ReadShortcut(const fs::path& lnk) {
    ShortcutInfo info;
    std::error_code ec;
    if (!fs::exists(lnk, ec)) return info;

    IShellLinkW* psl = nullptr;
    if (FAILED(CoCreateInstance(CLSID_ShellLink, nullptr, CLSCTX_INPROC_SERVER,
                                 IID_IShellLinkW,
                                 reinterpret_cast<void**>(&psl))))
        return info;
    IPersistFile* ppf = nullptr;
    if (SUCCEEDED(psl->QueryInterface(IID_IPersistFile,
                                       reinterpret_cast<void**>(&ppf)))) {
        if (SUCCEEDED(ppf->Load(lnk.wstring().c_str(), STGM_READ))) {
            wchar_t buf[MAX_PATH] = {};
            // SLGP_RAWPATH: the path as STORED. Resolving it would let the
            // shell "helpfully" chase a moved target and hand us a path the
            // file does not actually contain, which is the one thing this
            // comparison must not be fooled by.
            if (SUCCEEDED(psl->GetPath(buf, MAX_PATH, nullptr, SLGP_RAWPATH))) {
                info.target = buf;
                info.readable = true;
            }
            wchar_t abuf[INFOTIPSIZE] = {};
            if (SUCCEEDED(psl->GetArguments(abuf, INFOTIPSIZE)))
                info.args = abuf;
        }
        ppf->Release();
    }
    psl->Release();
    return info;
}

// Create the shortcut only if it is not already exactly right.
// Returns true when the shortcut ends up correct, whether or not it was
// touched. ``changed`` reports whether anything was written.
static bool EnsureShortcut(const fs::path& lnk, const fs::path& targetExe,
                             const fs::path& iconPath,
                             const std::wstring& description,
                             const std::wstring& arguments = L"",
                             bool* changed = nullptr) {
    if (changed) *changed = false;
    ShortcutInfo cur = ReadShortcut(lnk);
    if (cur.readable && SamePath(cur.target, targetExe)
            && _wcsicmp(cur.args.c_str(), arguments.c_str()) == 0)
        return true;                      // already correct — leave it alone
    std::error_code ec;
    fs::create_directories(lnk.parent_path(), ec);
    bool ok = CreateShellShortcut(lnk, targetExe, iconPath, description,
                                   arguments);
    if (changed) *changed = ok;
    return ok;
}

// -----------------------------------------------------------------------------
// Windows Firewall
//
// A firewall rule for a desktop app records the PROGRAM PATH it applies to —
// not a hash, not a version resource, not a signature. LogicBoard.exe carries
// no version resource at all (PyInstaller is given none) and is unsigned, so
// neither could have been what changed. What changed was the path: 3.0.0 sat
// at <root>\LogicBoard.exe and every reinstall landed on it, while 3.1.0
// moved to <root>\LogicBoard_V3.1.0\LogicBoard.exe. To the firewall that is
// simply a program nobody has allowed yet, so Windows asked again — and left
// the old rule behind pointing at an exe that no longer exists.
//
// Two things follow, and both are done honestly rather than by disguising the
// binary. The install path is now stable across versions, so one approval
// keeps working. And while we are elevated to remove the machine-wide install
// (the only time we have the rights for it), we tidy up the dead rules it
// left and authorise the new path — the same allowance the user would grant
// at the prompt, granted once, at the moment they are already approving an
// administrator action.
// -----------------------------------------------------------------------------
template <typename F>
static void ForEachFirewallRule(INetFwRules* rules, F fn) {
    IUnknown* unk = nullptr;
    if (FAILED(rules->get__NewEnum(&unk)) || !unk) return;
    IEnumVARIANT* it = nullptr;
    if (SUCCEEDED(unk->QueryInterface(__uuidof(IEnumVARIANT),
                                       reinterpret_cast<void**>(&it)))) {
        VARIANT v; VariantInit(&v);
        ULONG got = 0;
        while (it->Next(1, &v, &got) == S_OK && got == 1) {
            if (v.vt == VT_DISPATCH && v.pdispVal) {
                INetFwRule* rule = nullptr;
                if (SUCCEEDED(v.pdispVal->QueryInterface(
                        __uuidof(INetFwRule),
                        reinterpret_cast<void**>(&rule)))) {
                    fn(rule);
                    rule->Release();
                }
            }
            VariantClear(&v);
        }
        it->Release();
    }
    unk->Release();
}

static std::wstring BstrToW(BSTR b) {
    return b ? std::wstring(b, SysStringLen(b)) : std::wstring();
}

// Open the firewall rule collection. Caller releases both on success.
static bool OpenFirewallRules(INetFwPolicy2** policyOut, INetFwRules** rulesOut) {
    *policyOut = nullptr; *rulesOut = nullptr;
    CLSID clsid;
    // ProgID rather than __uuidof(NetFwPolicy2): the class GUID would have to
    // come from a library this project does not otherwise link.
    if (FAILED(CLSIDFromProgID(L"HNetCfg.FwPolicy2", &clsid))) return false;
    INetFwPolicy2* policy = nullptr;
    if (FAILED(CoCreateInstance(clsid, nullptr, CLSCTX_INPROC_SERVER,
                                 __uuidof(INetFwPolicy2),
                                 reinterpret_cast<void**>(&policy))) || !policy)
        return false;
    INetFwRules* rules = nullptr;
    if (FAILED(policy->get_Rules(&rules)) || !rules) {
        policy->Release();
        return false;
    }
    *policyOut = policy; *rulesOut = rules;
    return true;
}

// Delete the inbound rules a previous install's exe path left behind.
//
// The COM API removes rules BY NAME, and every rule Windows created from an
// "allow access?" prompt is named after the program — so several unrelated
// paths can share one name. Removing a name blindly would take rules for
// paths we are not replacing (a developer build, a copy on the desktop) with
// it. So a name is only removed when EVERY rule carrying it points inside the
// folder being deleted; anything shared is left alone.
static int FirewallDropRulesUnder(const fs::path& dir) {
    if (dir.empty()) return 0;
    INetFwPolicy2* policy = nullptr; INetFwRules* rules = nullptr;
    if (!OpenFirewallRules(&policy, &rules)) return 0;

    // name -> (rules under dir, rules elsewhere)
    std::vector<std::pair<std::wstring, std::pair<int, int>>> tally;
    auto bump = [&](const std::wstring& name, bool under) {
        for (auto& e : tally) {
            if (_wcsicmp(e.first.c_str(), name.c_str()) == 0) {
                (under ? e.second.first : e.second.second)++;
                return;
            }
        }
        tally.push_back({ name, under ? std::make_pair(1, 0)
                                      : std::make_pair(0, 1) });
    };

    ForEachFirewallRule(rules, [&](INetFwRule* rule) {
        BSTR bName = nullptr, bApp = nullptr;
        rule->get_Name(&bName);
        rule->get_ApplicationName(&bApp);
        std::wstring name = BstrToW(bName), app = BstrToW(bApp);
        if (bName) SysFreeString(bName);
        if (bApp)  SysFreeString(bApp);
        if (name.empty()) return;
        bump(name, !app.empty() && PathIsUnder(fs::path(app), dir));
    });

    int removed = 0;
    for (auto& e : tally) {
        if (e.second.first == 0 || e.second.second != 0) continue;
        BSTR b = SysAllocString(e.first.c_str());
        if (b) {
            if (SUCCEEDED(rules->Remove(b))) removed += e.second.first;
            SysFreeString(b);
        }
    }
    rules->Release(); policy->Release();
    return removed;
}

// Is this exact program already allowed inbound?
static bool FirewallHasRuleFor(INetFwRules* rules, const fs::path& exe) {
    bool found = false;
    ForEachFirewallRule(rules, [&](INetFwRule* rule) {
        if (found) return;
        BSTR bApp = nullptr;
        if (FAILED(rule->get_ApplicationName(&bApp)) || !bApp) return;
        std::wstring app = BstrToW(bApp);
        SysFreeString(bApp);
        if (!SamePath(fs::path(app), exe)) return;
        NET_FW_RULE_DIRECTION dir = NET_FW_RULE_DIR_IN;
        NET_FW_ACTION action = NET_FW_ACTION_ALLOW;
        rule->get_Direction(&dir);
        rule->get_Action(&action);
        if (dir == NET_FW_RULE_DIR_IN && action == NET_FW_ACTION_ALLOW)
            found = true;
    });
    return found;
}

// Allow LogicBoard through the firewall at its new, stable path.
//
// Exactly what the "allow access?" prompt would create if the user ticked
// Private, and nothing more: one inbound allow rule, scoped to this program,
// on the domain and private profiles. Public is deliberately left out — a
// classroom LAN is not a coffee shop. Idempotent: if a rule for this path
// already exists we add nothing.
static bool FirewallAllowProgram(const fs::path& exe) {
    if (exe.empty()) return false;
    INetFwPolicy2* policy = nullptr; INetFwRules* rules = nullptr;
    if (!OpenFirewallRules(&policy, &rules)) return false;

    bool ok = false;
    if (FirewallHasRuleFor(rules, exe)) {
        ok = true;                                 // nothing to do
    } else {
        CLSID clsid;
        INetFwRule* rule = nullptr;
        if (SUCCEEDED(CLSIDFromProgID(L"HNetCfg.FWRule", &clsid)) &&
            SUCCEEDED(CoCreateInstance(clsid, nullptr, CLSCTX_INPROC_SERVER,
                                        __uuidof(INetFwRule),
                                        reinterpret_cast<void**>(&rule)))) {
            BSTR bName = SysAllocString(APP_NAME);
            BSTR bApp  = SysAllocString(exe.wstring().c_str());
            BSTR bDesc = SysAllocString(
                L"Lets LogicBoard receive files and announcements from "
                L"LogicShare on the classroom network.");
            BSTR bGroup = SysAllocString(APP_NAME);
            rule->put_Name(bName);
            rule->put_ApplicationName(bApp);
            rule->put_Description(bDesc);
            rule->put_Grouping(bGroup);
            rule->put_Protocol(NET_FW_IP_PROTOCOL_ANY);
            rule->put_Direction(NET_FW_RULE_DIR_IN);
            rule->put_Action(NET_FW_ACTION_ALLOW);
            rule->put_Profiles(NET_FW_PROFILE2_DOMAIN | NET_FW_PROFILE2_PRIVATE);
            rule->put_Enabled(VARIANT_TRUE);
            ok = SUCCEEDED(rules->Add(rule));
            if (bName)  SysFreeString(bName);
            if (bApp)   SysFreeString(bApp);
            if (bDesc)  SysFreeString(bDesc);
            if (bGroup) SysFreeString(bGroup);
            rule->Release();
        }
    }
    rules->Release(); policy->Release();
    return ok;
}

// -----------------------------------------------------------------------------
// Removing a previous machine-wide install
//
// Runs in a SHORT-LIVED ELEVATED COPY of this exe (--remove-legacy), started
// only when a Program Files install was actually found. Everything that needs
// administrator rights happens here and nowhere else, so the install itself —
// the long part, the part that runs on every update — stays a per-user
// operation that never asks for credentials.
//
// Exit code is the contract with the unelevated parent: 0 = the machine-wide
// install is gone, non-zero = something is still there and installing on top
// of it would leave the user with two LogicBoards.
// -----------------------------------------------------------------------------
static void TerminateLogicBoard();     // defined next to the uninstall path

static int RemoveLegacyInstall(const fs::path& newExe) {
    std::error_code ec;
    fs::path legacy = LegacyProgramFilesDir();

    // The old copy may be running as a tray process, holding its own exe and
    // DLLs open. Windows will not delete a mapped image.
    TerminateLogicBoard();

    // Firewall rules first, while the folder they name still exists — the
    // check for "is this rule ours" is a path comparison against it.
    if (!legacy.empty()) FirewallDropRulesUnder(legacy);

    // Files.
    if (!legacy.empty() && fs::exists(legacy, ec))
        fs::remove_all(legacy, ec);

    // Machine-wide registration. RegDeleteKeyW does not recurse, and the
    // Uninstall/App Paths keys have no subkeys, so it is the right call here.
    RegDeleteKeyW(HKEY_LOCAL_MACHINE, UNINSTALL_KEY);
    RegDeleteKeyW(HKEY_LOCAL_MACHINE, APP_PATHS_KEY);
    HKEY hRun = nullptr;
    if (RegOpenKeyExW(HKEY_LOCAL_MACHINE, RUN_KEY, 0, KEY_SET_VALUE, &hRun)
            == ERROR_SUCCESS) {
        RegDeleteValueW(hRun, RUN_VALUE_NAME);
        RegCloseKey(hRun);
    }

    // All-users shortcuts. These are the other half of the duplicate-icon
    // problem: the per-user shortcut the new install creates would sit beside
    // this one, because Explorer merges the two desktops into one view.
    auto dropCommon = [&](int csidl, const wchar_t* leaf, bool folder) {
        wchar_t dir[MAX_PATH] = {};
        if (FAILED(SHGetFolderPathW(nullptr, csidl, nullptr, 0, dir))) return;
        fs::path p = fs::path(dir) / leaf;
        if (folder) fs::remove_all(p, ec); else fs::remove(p, ec);
    };
    dropCommon(CSIDL_COMMON_DESKTOPDIRECTORY, L"LogicBoard.lnk", false);
    dropCommon(CSIDL_COMMON_STARTUP,          L"LogicBoard.lnk", false);
    dropCommon(CSIDL_COMMON_PROGRAMS,         L"LogicBoard.lnk", false);
    dropCommon(CSIDL_COMMON_PROGRAMS,         L"LogicBoard",     true);

    // Authorise the new location while we still hold the rights to do it.
    // Best effort — a firewall service that refuses is not a reason to fail
    // the migration; the user simply sees the ordinary Windows prompt once.
    if (!newExe.empty()) FirewallAllowProgram(newExe);

    // Did it actually go?
    if (!legacy.empty() && fs::exists(legacy, ec)) {
        std::wstring dummy;
        if (FindExeUnder(legacy, dummy)) return 2;   // exe still there
    }
    return 0;
}

// Parent side: ask for elevation and wait. Returns 0 on success, 1 if the
// user declined the prompt, 2 if it failed for any other reason.
static int ElevateAndRemoveLegacy(const fs::path& newExe) {
    wchar_t self[MAX_PATH] = {};
    GetModuleFileNameW(nullptr, self, MAX_PATH);
    std::wstring params = std::wstring(REMOVE_LEGACY_SWITCH)
                        + L" \"" + newExe.wstring() + L"\"";

    SHELLEXECUTEINFOW sei = { sizeof(sei) };
    // "runas" is the verb that raises the consent prompt. CreateProcess
    // cannot elevate — it would inherit our medium-integrity token and fail
    // on the first delete in Program Files.
    sei.fMask  = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC;
    sei.lpVerb = L"runas";
    sei.lpFile = self;
    sei.lpParameters = params.c_str();
    sei.nShow = SW_HIDE;
    if (!ShellExecuteExW(&sei)) {
        DWORD err = GetLastError();
        return (err == ERROR_CANCELLED) ? 1 : 2;
    }
    if (!sei.hProcess) return 2;
    // INFINITE rather than a timeout: this deletes a directory tree, and
    // killing it half-way is the one outcome worse than waiting.
    WaitForSingleObject(sei.hProcess, INFINITE);
    DWORD code = 2;
    GetExitCodeProcess(sei.hProcess, &code);
    CloseHandle(sei.hProcess);
    return code == 0 ? 0 : 2;
}

// -----------------------------------------------------------------------------
// Elevating the UNINSTALL
//
// Why at run time and not in the manifest: the uninstaller is not a separate
// binary. It is a copy of THIS exe, dropped into the install folder during
// install, so it carries the installer's manifest — and that manifest is
// asInvoker on purpose. Marking it requireAdministrator would put a consent
// prompt in front of every routine update on exactly the machines (school
// OPS panels) where the person running the update cannot answer one. See
// manifest.xml. A "runas" re-launch keeps the install silent and still
// gates the uninstall.
//
// Why gate it at all, given the install is per-user: because an uninstall
// has more to remove than an install put down. A machine that has been
// through the Program Files era still carries HKLM Uninstall/App Paths/Run
// entries, all-users shortcuts, and firewall rules naming the app — and
// every one of those deletes used to fail silently for want of rights. What
// that looked like was LogicBoard still sitting in Apps & Features after
// being uninstalled.
// -----------------------------------------------------------------------------

static bool IsProcessElevated() {
    HANDLE tok = nullptr;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &tok))
        return false;
    TOKEN_ELEVATION el = {};
    DWORD cb = 0;
    BOOL ok = GetTokenInformation(tok, TokenElevation, &el, sizeof(el), &cb);
    CloseHandle(tok);
    return ok && el.TokenIsElevated != 0;
}

// The SID of the account this process runs as, as a string ("S-1-5-21-...").
// Empty on failure, which every caller treats as "assume same user".
static std::wstring CurrentUserSidString() {
    HANDLE tok = nullptr;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &tok))
        return L"";
    DWORD cb = 0;
    GetTokenInformation(tok, TokenUser, nullptr, 0, &cb);   // sizing call
    std::wstring out;
    if (cb) {
        std::vector<BYTE> buf(cb);
        if (GetTokenInformation(tok, TokenUser, buf.data(), cb, &cb)) {
            auto* tu = reinterpret_cast<TOKEN_USER*>(buf.data());
            LPWSTR str = nullptr;
            if (ConvertSidToStringSidW(tu->User.Sid, &str) && str) {
                out = str;
                LocalFree(str);
            }
        }
    }
    CloseHandle(tok);
    return out;
}

// Value of --user-sid: from a command line, or empty if absent.
static std::wstring ParseUserSid(const wchar_t* cmdLine) {
    if (!cmdLine) return L"";
    const wchar_t* p = wcsstr(cmdLine, USER_SID_SWITCH);
    if (!p) return L"";
    p += wcslen(USER_SID_SWITCH);
    std::wstring out;
    // A SID is printable ASCII with no spaces, so the first space, tab or
    // quote ends it. No quoting needed on the way out either.
    while (*p && *p != L' ' && *p != L'\t' && *p != L'"') out += *p++;
    return out;
}

// Where the PER-USER half of the uninstall lives. Ordinarily HKCU and the
// shell's own answers — the elevated copy is the same account, just with a
// full token. When it is NOT (an administrator's credentials went into the
// prompt), these point at the hive and folders of whoever asked.
struct PerUserRoots {
    HKEY root = HKEY_CURRENT_USER;
    std::wstring prefix;                        // L"" or L"<sid>\\"
    std::wstring desktop, programs, startup;    // empty => see `foreign`
    // True once we know the invoking user is NOT the account this process
    // runs as. It is the difference between an empty folder above meaning
    // "ask the shell" and meaning "we could not find out, so touch
    // nothing" — opening the SID key can succeed while a read one level
    // deeper is refused, and asking the shell at that point would return
    // the ADMINISTRATOR's Desktop. Deleting nothing beats deleting the
    // wrong person's shortcut.
    bool foreign = false;
};

static PerUserRoots ResolveInvokingUser(const std::wstring& sid) {
    PerUserRoots pu;
    if (sid.empty()) return pu;
    if (_wcsicmp(sid.c_str(), CurrentUserSidString().c_str()) == 0)
        return pu;      // same account — HKCU and the shell are already right

    // The hive is only reachable through HKEY_USERS while the user is
    // logged on. If it is not loaded there is nothing better to do than
    // carry on with this process's own view, so say so by leaving pu alone.
    HKEY probe = nullptr;
    if (RegOpenKeyExW(HKEY_USERS, sid.c_str(), 0, KEY_READ, &probe)
            != ERROR_SUCCESS)
        return pu;
    RegCloseKey(probe);

    pu.root    = HKEY_USERS;
    pu.prefix  = sid + L"\\";
    pu.foreign = true;

    // "Shell Folders", NOT "User Shell Folders". The latter stores
    // REG_EXPAND_SZ values like %USERPROFILE%\Desktop, and expanding those
    // in THIS process resolves them against the administrator's profile —
    // precisely the mix-up this function exists to prevent. "Shell Folders"
    // holds the already-expanded paths, and honours folder redirection.
    std::wstring sf = pu.prefix +
        L"Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Shell Folders";
    pu.desktop  = RegReadString(HKEY_USERS, sf.c_str(), L"Desktop");
    pu.programs = RegReadString(HKEY_USERS, sf.c_str(), L"Programs");
    pu.startup  = RegReadString(HKEY_USERS, sf.c_str(), L"Startup");
    return pu;
}

// Parent side: re-launch ourselves elevated to do the whole uninstall, and
// wait. Returns the child's exit code, or one of the UNINST_EXIT_* codes if
// the child never started.
static int ElevateForUninstall() {
    wchar_t self[MAX_PATH] = {};
    GetModuleFileNameW(nullptr, self, MAX_PATH);

    std::wstring params = L"/uninstall ";
    params += ELEVATED_SWITCH;
    std::wstring sid = CurrentUserSidString();
    if (!sid.empty()) {
        params += L" ";
        params += USER_SID_SWITCH;
        params += sid;
    }

    SHELLEXECUTEINFOW sei = { sizeof(sei) };
    sei.fMask  = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC;
    sei.lpVerb = L"runas";
    sei.lpFile = self;
    sei.lpParameters = params.c_str();
    // SW_SHOWNORMAL, unlike the hidden --remove-legacy helper: this child
    // owns the entire uninstall UI, confirm screen included.
    sei.nShow  = SW_SHOWNORMAL;
    if (!ShellExecuteExW(&sei)) {
        DWORD err = GetLastError();
        return (err == ERROR_CANCELLED) ? UNINST_EXIT_DECLINED
                                        : UNINST_EXIT_NO_ELEV;
    }
    if (!sei.hProcess) return UNINST_EXIT_NO_ELEV;
    // Waiting matters here rather than just being tidy: Apps & Features
    // refreshes its list when the process it launched exits, and the
    // process it launched is US. Returning early would leave LogicBoard
    // showing in the list until the user navigated away and back.
    WaitForSingleObject(sei.hProcess, INFINITE);
    DWORD code = 0;
    GetExitCodeProcess(sei.hProcess, &code);
    CloseHandle(sei.hProcess);
    return static_cast<int>(code);
}

// Everything Windows needs to list LogicBoard as an installed app and to
// find it by name — all under HKCU, because this is a per-user install and
// writing HKLM would (a) need administrator rights we no longer take and
// (b) claim on behalf of every account on the machine something that is only
// true for this one.
//
// The install directory and the app directory are now the SAME folder. They
// used to differ (a versioned subfolder inside the install root) and each
// registry value wanted a specific one of the two; with a flat layout that
// distinction is gone, and with it the class of bug where a shortcut pointed
// at the root, which held no exe.
static bool RegisterUninstallEntry(const fs::path& installDir,
                                     const fs::path& iconPath) {
    HKEY hKey = nullptr;
    LONG r = RegCreateKeyExW(HKEY_CURRENT_USER, UNINSTALL_KEY, 0,
                              nullptr, 0, KEY_WRITE, nullptr, &hKey, nullptr);
    if (r != ERROR_SUCCESS) return false;

    auto setStr = [&](const wchar_t* name, const wchar_t* value) {
        RegSetValueExW(hKey, name, 0, REG_SZ,
                        reinterpret_cast<const BYTE*>(value),
                        static_cast<DWORD>((wcslen(value) + 1) * sizeof(wchar_t)));
    };
    auto setDword = [&](const wchar_t* name, DWORD value) {
        RegSetValueExW(hKey, name, 0, REG_DWORD,
                        reinterpret_cast<const BYTE*>(&value), sizeof(value));
    };

    setStr(L"DisplayName",     APP_NAME);
    setStr(L"DisplayVersion",  APP_VERSION);
    setStr(L"Publisher",       APP_PUBLISHER);
    setStr(L"DisplayIcon",     iconPath.wstring().c_str());
    setStr(L"InstallLocation", installDir.wstring().c_str());

    // UninstallString runs a copy of ourselves that we drop next to the
    // app during install — so uninstall works from Apps & Features even
    // when the original installer download is long gone.
    fs::path uninstaller = installDir / L"Uninstaller.exe";
    std::wstring uninstallCmd = L"\"" + uninstaller.wstring() + L"\" /uninstall";
    setStr(L"UninstallString", uninstallCmd.c_str());

    setDword(L"NoModify", 1);
    setDword(L"NoRepair", 1);
    // NoRemove greys out the Uninstall button in Settings. We never set it,
    // but older LogicBoard builds did when run from a folder with no
    // uninstaller (the source tree), and a value nobody writes is a value
    // nobody clears — so an install or update removes it.
    RegDeleteValueW(hKey, L"NoRemove");
    // A per-user install is exactly what this flag describes. Without it,
    // Settings lists LogicBoard among the machine-wide apps and offers an
    // uninstall that would need rights the entry does not have.
    setDword(L"SystemComponent", 0);

    // Estimated size (KB). We EXCLUDE Uninstaller.exe from the tally —
    // it's installer scaffolding, not part of the app footprint the user
    // expects to see. (It's also stripped of its ZIP payload at install
    // time, but skipping it entirely keeps the reported size honest.)
    uintmax_t bytes = 0;
    std::error_code ec;
    for (auto& e : fs::recursive_directory_iterator(installDir, ec)) {
        if (ec) continue;
        if (!e.is_regular_file(ec)) continue;
        if (e.path().filename() == L"Uninstaller.exe") continue;
        bytes += e.file_size(ec);
    }
    if (bytes > 0) setDword(L"EstimatedSize", static_cast<DWORD>(bytes / 1024));

    RegCloseKey(hKey);

    // App Paths — what makes Windows Search and the Run dialog find and
    // launch "LogicBoard" by name. HKCU only, same reasoning as above.
    std::wstring exePath = (installDir / APP_EXE).wstring();
    std::wstring dirPath = installDir.wstring();
    HKEY hApp = nullptr;
    if (RegCreateKeyExW(HKEY_CURRENT_USER, APP_PATHS_KEY, 0, nullptr, 0,
                         KEY_WRITE, nullptr, &hApp, nullptr)
            == ERROR_SUCCESS) {
        RegSetValueExW(hApp, nullptr, 0, REG_SZ,
            reinterpret_cast<const BYTE*>(exePath.c_str()),
            static_cast<DWORD>((exePath.size() + 1) * sizeof(wchar_t)));
        RegSetValueExW(hApp, L"Path", 0, REG_SZ,
            reinterpret_cast<const BYTE*>(dirPath.c_str()),
            static_cast<DWORD>((dirPath.size() + 1) * sizeof(wchar_t)));
        RegCloseKey(hApp);
    }

    return true;
}

// Defined further down (next to the uninstall path, which has always used
// it). Declared here because the INSTALL path needs it too — see the call
// at the top of RunInstall for why.
static void TerminateLogicBoard();

// The full install pipeline. Runs on a background thread.
static void RunInstall() {
    // COM must be initialized on THIS thread — apartments are per-thread,
    // and the CoInitializeEx in wWinMain only covers the UI thread. Without
    // this, every CoCreateInstance(CLSID_ShellLink) below fails with
    // CO_E_NOTINITIALIZED and NO shortcuts get created (silently, since we
    // don't hard-fail on a single shortcut). This apartment is balanced by
    // the CoUninitialize at the end of the function.
    HRESULT comInit = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
    try {
        // %USERPROFILE%\LogicBoard — flat, per-user, stable across versions.
        // installDir and appDir are the same folder now; both names are kept
        // because the steps below read more clearly for it.
        fs::path installDir = InstallDir();
        if (installDir.empty()) {
            std::lock_guard<std::mutex> lk(g_ctx.textLock);
            g_ctx.errorText = L"Couldn't locate your user profile folder.";
            SetScreen(Screen::Error);
            RequestRepaint();
            return;
        }
        fs::path appDir = installDir;

        // Pre-flight: verify the bundled payloads are actually present in
        // the EXE before we touch anything. The ZIP + icon are baked
        // in as resources at build time; if the build was assembled
        // wrong (someone forgot to drop LogicBoard.zip / logo.ico next to
        // the source), we bail cleanly BEFORE creating any folders rather
        // than half-installing.
        {
            HMODULE self = GetModuleHandleW(nullptr);
            HRSRC zipRes = FindResourceW(self, RC_ZIP_NAME,
                                          MAKEINTRESOURCEW(10));
            HRSRC icoRes = FindResourceW(self, MAKEINTRESOURCEW(102),
                                          MAKEINTRESOURCEW(10));
            if (!zipRes || !icoRes) {
                std::lock_guard<std::mutex> lk(g_ctx.textLock);
                g_ctx.errorText =
                    L"This installer is missing its bundled files "
                    L"(LogicBoard.zip / logo.ico were not embedded at "
                    L"build time). Nothing was installed.";
                SetScreen(Screen::Error);
                RequestRepaint();
                return;
            }
        }

        // Step 0a — an older machine-wide install has to go first.
        //
        // This is the ONLY thing in the whole installer that needs
        // administrator rights, so it is the only thing that asks for them —
        // in a separate, short-lived elevated process. Installing on top of
        // a Program Files copy without removing it would leave two
        // LogicBoards on the machine, the old one still registered to start
        // at boot from HKLM, which is worse than not installing at all.
        //
        // A declined prompt stops here rather than installing anyway. The
        // user is told exactly what happened, and nothing has been changed.
        if (g_legacy.present) {
            SetStatus(L"Removing the previous system-wide installation...");
            g_ctx.progress.store(1);
            RequestRepaint();
            int rc = ElevateAndRemoveLegacy(appDir / APP_EXE);
            if (rc != 0) {
                std::lock_guard<std::mutex> lk(g_ctx.textLock);
                g_ctx.errorText = (rc == 1)
                    ? L"LogicBoard is currently installed in Program Files, "
                      L"which needs administrator permission to remove — and "
                      L"the request was declined.\n\nNothing has been "
                      L"changed. Run this installer again and approve the "
                      L"prompt, and the rest of the install will not need "
                      L"administrator rights at all."
                    : L"The previous installation in Program Files could not "
                      L"be removed.\n\nMake sure LogicBoard is completely "
                      L"closed (including the icon in the system tray) and "
                      L"try again. Nothing has been changed.";
                SetScreen(Screen::Error);
                RequestRepaint();
                return;
            }
            g_legacy.present = false;
        }

        // Step 0b — stop any running LogicBoard FIRST.
        //
        // This was the install path's one missing step, and it failed the
        // whole install every time you upgraded without quitting the app.
        // LogicBoard keeps running in the tray (the LogicShare background
        // service), so its LogicBoard.exe, logicboard_engine .pyd and the
        // MSVC/MinGW DLLs under _internal are all held open by the OS.
        // Windows will not let anyone delete or truncate a mapped image:
        //   * Step 1's remove_all silently failed on every locked file
        //     (the error_code is passed and ignored, by design, so one
        //     stray file can't abort the wipe), then
        //   * extraction hit LogicBoard.exe, the ofstream open failed with
        //     a sharing violation, and the whole install rolled back —
        //     reporting "this usually means permissions", which sent you
        //     hunting for an admin problem that was never there.
        // The uninstaller has always called this for exactly this reason;
        // installing over a live copy needs it just as much.
        SetStatus(L"Closing LogicBoard...");
        g_ctx.progress.store(2);
        RequestRepaint();
        TerminateLogicBoard();

        // Step 1 — clean install dir if a prior version exists. We keep
        // Uninstaller.exe out of the way so we don't crash trying to
        // overwrite a locked file — it'll be overwritten later anyway.
        SetStatus(L"Preparing install directory...");
        g_ctx.progress.store(5);
        std::error_code ec;
        if (fs::exists(installDir, ec)) {
            for (auto& entry : fs::directory_iterator(installDir, ec)) {
                if (entry.path().filename() == L"Uninstaller.exe") continue;
                fs::remove_all(entry.path(), ec);
            }
        }
        fs::create_directories(appDir, ec);

        // Step 2 — extract the ZIP payload into the VERSIONED folder.
        std::wstring err;
        if (!ExtractEmbeddedZip(appDir, err)) {
            // Clean up whatever was partially extracted so we never leave
            // a broken half-install behind. We keep Uninstaller.exe if it
            // somehow already existed (locked), but everything this run
            // created gets scrubbed.
            std::error_code rec;
            if (fs::exists(installDir, rec)) {
                for (auto& entry : fs::directory_iterator(installDir, rec)) {
                    if (entry.path().filename() == L"Uninstaller.exe")
                        continue;
                    fs::remove_all(entry.path(), rec);
                }
                // Remove the dir itself if it ended up empty.
                fs::remove(installDir, rec);
            }
            std::lock_guard<std::mutex> lk(g_ctx.textLock);
            g_ctx.errorText = err +
                L"\n\nAny partially-copied files were cleaned up.";
            SetScreen(Screen::Error);
            RequestRepaint();
            return;
        }

        // Step 2.5 — verify the extraction actually produced the exe
        // where every shortcut + registry entry is about to point. If
        // the ZIP was structured in some unexpected way that our root-
        // strip didn't handle, LogicBoard.exe might not be at
        // installDir\LogicBoard.exe — and silently creating shortcuts to
        // a missing target is exactly the kind of "installed but broken"
        // state we want to avoid. Bail with a clear message + full
        // cleanup instead.
        {
            std::error_code vec;
            if (!fs::exists(appDir / APP_EXE, vec)) {
                std::error_code rec;
                if (fs::exists(installDir, rec)) {
                    for (auto& entry : fs::directory_iterator(installDir, rec)) {
                        if (entry.path().filename() == L"Uninstaller.exe")
                            continue;
                        fs::remove_all(entry.path(), rec);
                    }
                    fs::remove(installDir, rec);
                }
                std::lock_guard<std::mutex> lk(g_ctx.textLock);
                g_ctx.errorText =
                    L"Extraction finished but LogicBoard.exe wasn't found "
                    L"where expected. The bundled ZIP may be packaged "
                    L"incorrectly (LogicBoard.exe should sit at the root "
                    L"of the archive). Nothing was left installed.";
                SetScreen(Screen::Error);
                RequestRepaint();
                return;
            }
        }
        fs::path iconPath = installDir / L"logo.ico";
        HRSRC hIcoRes = FindResourceW(
            GetModuleHandleW(nullptr), MAKEINTRESOURCEW(102), MAKEINTRESOURCEW(10) /*RT_RCDATA*/);
        if (hIcoRes) {
            HGLOBAL hMem = LoadResource(GetModuleHandleW(nullptr), hIcoRes);
            DWORD sz = SizeofResource(GetModuleHandleW(nullptr), hIcoRes);
            void* p = LockResource(hMem);
            if (p && sz) {
                std::ofstream f(iconPath, std::ios::binary);
                f.write(static_cast<const char*>(p), sz);
            }
        }

        // Step 4 — self-copy as Uninstaller.exe so Apps & Features can
        // launch us later with /uninstall.
        SetStatus(L"Registering with Windows...");
        g_ctx.progress.store(92);
        wchar_t selfPath[MAX_PATH] = {};
        GetModuleFileNameW(nullptr, selfPath, MAX_PATH);
        fs::path uninstallerPath = installDir / L"Uninstaller.exe";
        CopyFileW(selfPath, uninstallerPath.wstring().c_str(), FALSE);

        // Strip the embedded LogicBoard.zip resource FROM the uninstaller
        // copy. The uninstaller never extracts anything, so carrying the
        // ~700 MB payload just doubles the install footprint AND inflates
        // the size Windows reports in Apps & Features. UpdateResource with
        // a null data pointer deletes the named resource from the copy.
        {
            HANDLE hUpd = BeginUpdateResourceW(
                uninstallerPath.wstring().c_str(), FALSE);
            if (hUpd) {
                // Delete the ZIP payload (name LOGICBOARD_ZIP, type
                // RT_RCDATA = 10). Passing nullptr data + 0 size removes it.
                UpdateResourceW(
                    hUpd,
                    MAKEINTRESOURCEW(10),          // RT_RCDATA
                    RC_ZIP_NAME,                    // "LOGICBOARD_ZIP"
                    MAKELANGID(LANG_NEUTRAL, SUBLANG_NEUTRAL),
                    nullptr, 0);
                // Commit the change. If this fails the uninstaller still
                // works — it's just larger — so we don't treat it as fatal.
                EndUpdateResourceW(hUpd, FALSE);
            }
        }

        // Step 5 — shortcuts + startup. All per-user, all idempotent.
        fs::path targetExe = appDir / APP_EXE;

        // Clear artifacts older builds created that this one no longer does.
        // PER-USER locations only: the all-users copies are removed by the
        // elevated step above, which is the only context with rights there.
        {
            std::error_code oec;
            auto drop = [&](int csidl, const wchar_t* leaf) {
                wchar_t dir[MAX_PATH] = {};
                if (SUCCEEDED(SHGetFolderPathW(nullptr, csidl, nullptr, 0, dir)))
                    fs::remove(fs::path(dir) / leaf, oec);
            };
            // The Startup-FOLDER shortcut. LogicBoard used to write one of
            // these AS WELL AS the Run value below, and Windows enumerates
            // both — so LogicBoard appeared twice in Task Manager -> Startup
            // and twice in Settings -> Apps -> Startup. The Run value is the
            // single registration now, on both sides.
            drop(CSIDL_STARTUP, L"LogicBoard.lnk");
            // A flat Programs\LogicBoard.lnk, from before the Start Menu
            // shortcut moved into its own folder.
            drop(CSIDL_PROGRAMS, L"LogicBoard.lnk");
        }

        // Desktop — the CURRENT USER's desktop, one shortcut, REUSED.
        //
        // EnsureShortcut leaves a correct shortcut untouched and repoints a
        // stale one in place, so re-running the installer, or upgrading from
        // the Program Files layout, updates the icon the user already has
        // instead of putting a second one beside it.
        {
            wchar_t desk[MAX_PATH] = {};
            if (SUCCEEDED(SHGetFolderPathW(nullptr, CSIDL_DESKTOPDIRECTORY,
                                            nullptr, 0, desk)))
                EnsureShortcut(fs::path(desk) / L"LogicBoard.lnk",
                                targetExe, iconPath, L"LogicBoard");
        }

        // Start Menu — inside a "LogicBoard" SUBFOLDER under Programs
        // (...\Start Menu\Programs\LogicBoard\LogicBoard.lnk). A shortcut
        // in its own named folder is what Windows Search reliably picks up,
        // so LogicBoard appears when you type it into the taskbar search.
        {
            wchar_t startMenu[MAX_PATH] = {};
            if (SUCCEEDED(SHGetFolderPathW(nullptr, CSIDL_PROGRAMS,
                                            nullptr, 0, startMenu))) {
                fs::path lbFolder = fs::path(startMenu) / L"LogicBoard";
                std::error_code mkec;
                fs::create_directories(lbFolder, mkec);
                EnsureShortcut(lbFolder / L"LogicBoard.lnk",
                                targetExe, iconPath, L"LogicBoard");
            }
        }

        // Startup — the HKCU Run value, and nothing else.
        //
        // Per-user by design: LogicBoard starts at login to run its
        // LogicShare service, which is a thing this account wants, not a
        // machine-wide service. HKCU also needs no rights, which is what
        // keeps updates free of administrator prompts. Writing the value
        // again with the same name overwrites in place, so re-running the
        // installer cannot produce a second startup entry — and an entry
        // left over from the Program Files install is repointed here rather
        // than left aiming at a folder that no longer exists.
        {
            HKEY hRun = nullptr;
            if (RegCreateKeyExW(HKEY_CURRENT_USER, RUN_KEY, 0, nullptr, 0,
                                 KEY_WRITE, nullptr, &hRun, nullptr)
                    == ERROR_SUCCESS) {
                std::wstring runCmd = L"\"" + targetExe.wstring()
                                       + L"\" " + STARTUP_ARGS;
                RegSetValueExW(hRun, RUN_VALUE_NAME, 0, REG_SZ,
                    reinterpret_cast<const BYTE*>(runCmd.c_str()),
                    static_cast<DWORD>((runCmd.size() + 1) * sizeof(wchar_t)));
                RegCloseKey(hRun);
            }
        }

        // Step 6 — Apps & Features + Search registration.
        SetStatus(L"Finalising...");
        g_ctx.progress.store(97);
        RegisterUninstallEntry(installDir, iconPath);

        // Nudge the shell to re-scan associations + newly-created
        // shortcuts NOW, so LogicBoard shows up in taskbar / Start
        // search promptly instead of after the next background index
        // pass (which can be many minutes, or a reboot).
        SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, nullptr, nullptr);

        // Step 7 — done!
        g_ctx.progress.store(100);
        SetStatus(IsUpdateMode() ? L"Update complete."
                                 : L"Installation complete.");
        SetScreen(Screen::Done);
        RequestRepaint();
    } catch (const std::exception& ex) {
        std::lock_guard<std::mutex> lk(g_ctx.textLock);
        g_ctx.errorText = L"Unexpected error: ";
        // Convert exception message from UTF-8 to wide.
        std::string what = ex.what();
        int wlen = MultiByteToWideChar(CP_UTF8, 0, what.c_str(), -1,
                                         nullptr, 0);
        std::wstring wwhat(wlen, L'\0');
        MultiByteToWideChar(CP_UTF8, 0, what.c_str(), -1,
                             wwhat.data(), wlen);
        // MultiByteToWideChar(-1) counts the null terminator, leaving an
        // embedded L'\0' at the end of the wstring — strip it so the error
        // text concatenates + displays cleanly.
        if (!wwhat.empty() && wwhat.back() == L'\0') wwhat.pop_back();
        g_ctx.errorText += wwhat;
        SetScreen(Screen::Error);
        RequestRepaint();
    }
    // Balance the per-thread COM init. Only uninitialize if the init
    // actually succeeded (S_OK or S_FALSE both mean this thread now owns
    // a reference that must be released).
    if (SUCCEEDED(comInit)) CoUninitialize();
}

static void StartInstallThread() {
    SetScreen(Screen::Installing);
    g_ctx.progress.store(0);
    SetStatus(L"Starting...");
    RequestRepaint();
    std::thread(RunInstall).detach();
}

// Terminate every running LogicBoard.exe. Needed because LogicBoard runs
// as a background (LogicShare) process — if we don't kill it first, its
// EXE + DLLs are locked and deletion fails. Uses the Toolhelp snapshot
// API to find matching processes by name and TerminateProcess on each.
// Forward declarations — these functions cross-reference each other so
// the C++ single-pass compiler needs a heads-up before the first use.
static LRESULT CALLBACK WndProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp);
static void StartInstallThread();
static void StartUninstallThread();

static void TerminateLogicBoard() {
    // Pull in the Toolhelp API locally so the top of the file doesn't
    // need another header ordering dependency.
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE) return;

    PROCESSENTRY32W pe = {};
    pe.dwSize = sizeof(pe);
    if (Process32FirstW(snap, &pe)) {
        do {
            // Case-insensitive match on the image name.
            if (_wcsicmp(pe.szExeFile, APP_EXE) == 0) {
                HANDLE hProc = OpenProcess(
                    PROCESS_TERMINATE | SYNCHRONIZE, FALSE,
                    pe.th32ProcessID);
                if (hProc) {
                    TerminateProcess(hProc, 0);
                    // Give it up to 3 s to actually die so the file
                    // handles are released before we try to delete.
                    WaitForSingleObject(hProc, 3000);
                    CloseHandle(hProc);
                }
            }
        } while (Process32NextW(snap, &pe));
    }
    CloseHandle(snap);
}

// The actual removal work — runs on a detached background thread after the
// user confirms in the Aero-styled uninstall UI. Reports progress + status
// through g_ctx, same as the install thread. Any hard failure flips the UI
// to the Error screen so nothing is silently swallowed.
static void UninstallWorker(std::wstring installDir_s,
                              std::wstring selfPath_s) {
  try {
    fs::path installDir = installDir_s;
    fs::path selfPath   = selfPath_s;

    SetStatus(L"Stopping LogicBoard...");
    g_ctx.progress.store(10);
    TerminateLogicBoard();

    // Who are we cleaning up after? Normally this process's own account.
    // Not necessarily: this runs elevated, and if an administrator's
    // credentials were typed into the consent prompt then "current user"
    // here is that administrator, not the person uninstalling. See
    // ResolveInvokingUser.
    PerUserRoots pu = ResolveInvokingUser(g_invokingUserSid);

    SetStatus(L"Removing shortcuts...");
    g_ctx.progress.store(30);
    std::error_code ec;
    // Desktop + Start Menu + Startup shortcuts — remove BOTH the
    // all-users (installer-created) AND per-user copies so nothing is
    // left behind regardless of which the running config used.
    auto rm_lnk = [&](int csidl, const wchar_t* leaf) {
        wchar_t dir[MAX_PATH] = {};
        if (SUCCEEDED(SHGetFolderPathW(nullptr, csidl, nullptr, 0, dir)))
            fs::remove(fs::path(dir) / leaf, ec);
    };
    // A per-user folder: the invoking user's, when that is not us, and the
    // shell's answer otherwise. Empty means "leave it alone" — every
    // caller guards on it.
    auto user_dir = [&](const std::wstring& resolved, int csidl) -> fs::path {
        if (!resolved.empty()) return fs::path(resolved);
        // Deliberately NOT falling through to the shell for a foreign
        // account: SHGetFolderPathW answers for the token we are running
        // on, which in that case is the administrator who approved the
        // prompt. See the note on PerUserRoots::foreign.
        if (pu.foreign) return fs::path();
        wchar_t dir[MAX_PATH] = {};
        if (SUCCEEDED(SHGetFolderPathW(nullptr, csidl, nullptr, 0, dir)))
            return fs::path(dir);
        return fs::path();
    };
    const fs::path uDesktop  = user_dir(pu.desktop,  CSIDL_DESKTOPDIRECTORY);
    const fs::path uPrograms = user_dir(pu.programs, CSIDL_PROGRAMS);
    const fs::path uStartup  = user_dir(pu.startup,  CSIDL_STARTUP);

    // All-users entries only exist on machines that came from the old
    // Program Files install. They USED to be best-effort no-ops here for
    // want of rights, which is how a machine ended up with LogicBoard
    // still in Apps & Features after being uninstalled; the elevated
    // re-launch is what makes these actually delete.
    rm_lnk(CSIDL_COMMON_DESKTOPDIRECTORY, L"LogicBoard.lnk");
    rm_lnk(CSIDL_COMMON_PROGRAMS,         L"LogicBoard.lnk");
    rm_lnk(CSIDL_COMMON_STARTUP,          L"LogicBoard.lnk");
    for (const fs::path& d : { uDesktop, uPrograms, uStartup })
        if (!d.empty()) fs::remove(d / L"LogicBoard.lnk", ec);
    // Remove the Start Menu "LogicBoard" SUBFOLDER (shortcut inside) that
    // the installer creates for search visibility — both hives.
    auto rm_startmenu_folder = [&](int csidl) {
        wchar_t dir[MAX_PATH] = {};
        if (SUCCEEDED(SHGetFolderPathW(nullptr, csidl, nullptr, 0, dir)))
            fs::remove_all(fs::path(dir) / L"LogicBoard", ec);
    };
    rm_startmenu_folder(CSIDL_COMMON_PROGRAMS);
    if (!uPrograms.empty()) fs::remove_all(uPrograms / L"LogicBoard", ec);

    SetStatus(L"Cleaning registry entries...");
    g_ctx.progress.store(50);
    // Uninstall + App Paths. The per-user pair is where a per-user install
    // lives; the HKLM pair is what a Program Files leftover left behind,
    // and deleting those is one of the things the elevation buys.
    RegDeleteKeyW(pu.root, (pu.prefix + UNINSTALL_KEY).c_str());
    RegDeleteKeyW(HKEY_LOCAL_MACHINE, UNINSTALL_KEY);
    RegDeleteKeyW(pu.root, (pu.prefix + APP_PATHS_KEY).c_str());
    RegDeleteKeyW(HKEY_LOCAL_MACHINE, APP_PATHS_KEY);
    // Run (startup) value — the per-user one is ours; the HKLM one is a
    // leftover. KEY_SET_VALUE rather than KEY_WRITE: deleting a value is
    // all this needs, and asking for less is one fewer way to be refused.
    auto drop_run = [&](HKEY root, const std::wstring& sub) {
        HKEY hRun = nullptr;
        if (RegOpenKeyExW(root, sub.c_str(), 0, KEY_SET_VALUE, &hRun)
                == ERROR_SUCCESS) {
            RegDeleteValueW(hRun, RUN_VALUE_NAME);
            RegCloseKey(hRun);
        }
    };
    drop_run(pu.root, pu.prefix + RUN_KEY);
    drop_run(HKEY_LOCAL_MACHINE, RUN_KEY);

    // File associations. The app registers these itself on first run (see
    // ui/util/file_association.py — the two lists must match): .lb is ours
    // outright; .pdf / .pptx / .ppt only carry OUR value inside their
    // OpenWithProgids, and the extension keys themselves belong to whatever
    // app the user chose, so only that one value goes.
    {
        const std::wstring classes = pu.prefix + L"Software\\Classes\\";
        for (const wchar_t* progid : { L"LogicBoard.Board.v3",
                                        L"LogicBoard.PDF",
                                        L"LogicBoard.PPTX",
                                        L"LogicBoard.PPT" })
            RegDeleteTreeW(pu.root, (classes + progid).c_str());
        RegDeleteTreeW(pu.root, (classes + L".lb").c_str());
        RegDeleteTreeW(pu.root,
            (classes + L"Applications\\LogicBoard.exe").c_str());
        const std::pair<const wchar_t*, const wchar_t*> offered[] = {
            { L".pdf",  L"LogicBoard.PDF"  },
            { L".pptx", L"LogicBoard.PPTX" },
            { L".ppt",  L"LogicBoard.PPT"  },
        };
        for (const auto& o : offered) {
            HKEY h = nullptr;
            if (RegOpenKeyExW(pu.root,
                    (classes + o.first + L"\\OpenWithProgids").c_str(),
                    0, KEY_SET_VALUE, &h) == ERROR_SUCCESS) {
                RegDeleteValueW(h, o.second);
                RegCloseKey(h);
            }
        }
        RegDeleteTreeW(pu.root,
            (pu.prefix + L"Software\\LogicBoard\\Capabilities").c_str());
        // The "PDF / PowerPoint already offered once" flag (see
        // DOCS_ONCE_VALUE in file_association.py): a reinstall is a fresh
        // first run and offers them again.
        {
            HKEY hLb = nullptr;
            if (RegOpenKeyExW(pu.root,
                    (pu.prefix + L"Software\\LogicBoard").c_str(),
                    0, KEY_SET_VALUE, &hLb) == ERROR_SUCCESS) {
                RegDeleteValueW(hLb, L"DocumentTypesRegistered");
                RegCloseKey(hLb);
            }
        }
        // The parent only if nothing else lives there.
        RegDeleteKeyW(pu.root, (pu.prefix + L"Software\\LogicBoard").c_str());
        HKEY hApps = nullptr;
        if (RegOpenKeyExW(pu.root,
                (pu.prefix + L"Software\\RegisteredApplications").c_str(),
                0, KEY_SET_VALUE, &hApps) == ERROR_SUCCESS) {
            RegDeleteValueW(hApps, L"LogicBoard");
            RegCloseKey(hApps);
        }
        // Explorer keeps showing the old icons until told associations moved.
        SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, nullptr, nullptr);
    }

    // Firewall rules naming this install. This is the other thing the
    // elevation buys: the firewall policy object refuses writes from a
    // medium-integrity token, so before, what an uninstall left behind was
    // an inert rule pointing at an exe that no longer existed.
    FirewallDropRulesUnder(installDir);

    SetStatus(L"Deleting installed files...");
    g_ctx.progress.store(75);
    bool anyLocked = false;
    for (auto& e : fs::directory_iterator(installDir, ec)) {
        if (e.path() == selfPath) continue;
        std::error_code rmec;
        fs::remove_all(e.path(), rmec);
        if (rmec) anyLocked = true;
    }

    // Spawn a self-delete + directory-remove chain. cmd waits, deletes
    // Uninstaller.exe, then removes the (now-empty) install directory.
    std::wstring cmdArgs = L"/C ping 127.0.0.1 -n 3 > nul & del /F /Q \""
                            + selfPath.wstring() + L"\" & rmdir /S /Q \""
                            + installDir.wstring() + L"\"";
    STARTUPINFOW si = { sizeof(si) };
    si.dwFlags = STARTF_USESHOWWINDOW;
    si.wShowWindow = SW_HIDE;
    PROCESS_INFORMATION pi = {};
    std::wstring fullCmd = L"cmd.exe " + cmdArgs;
    CreateProcessW(nullptr, fullCmd.data(), nullptr, nullptr, FALSE,
                    CREATE_NO_WINDOW, nullptr, nullptr, &si, &pi);
    if (pi.hProcess) { CloseHandle(pi.hProcess); CloseHandle(pi.hThread); }

    g_ctx.progress.store(100);
    SetStatus(L"Finished.");
    if (anyLocked) {
        std::lock_guard<std::mutex> lk(g_ctx.textLock);
        g_ctx.errorText =
            L"Some files were still in use and couldn't be removed. "
            L"They'll be cleaned up on the next reboot.";
    }
    SetScreen(Screen::UninstDone);
    RequestRepaint();
  } catch (const std::exception& ex) {
    // Hard failure — surface it in the main window instead of silently
    // dying. Convert the UTF-8 what() to wide for display.
    std::lock_guard<std::mutex> lk(g_ctx.textLock);
    std::string what = ex.what();
    int wl = MultiByteToWideChar(CP_UTF8, 0, what.c_str(), -1, nullptr, 0);
    std::wstring wwhat(wl, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, what.c_str(), -1, wwhat.data(), wl);
    if (!wwhat.empty() && wwhat.back() == L'\0') wwhat.pop_back();
    g_ctx.errorText = L"Uninstall failed: " + wwhat;
    SetScreen(Screen::Error);
    RequestRepaint();
  }
}

// -----------------------------------------------------------------------------
// Uninstaller — invoked when the exe is run with /uninstall.
// Opens the SAME Aero-styled window class the installer uses, sets the
// starting screen to UninstConfirm, and lets the shared WndProc drive
// hit-testing + repainting. The heavy lifting happens on a worker thread
// that UninstallWorker manages.
// -----------------------------------------------------------------------------
static int RunUninstaller(HINSTANCE hInst) {
    // Where are we? (Uninstaller.exe lives inside the install dir.)
    wchar_t selfPath[MAX_PATH] = {};
    GetModuleFileNameW(nullptr, selfPath, MAX_PATH);
    fs::path installDir = fs::path(selfPath).parent_path();
    g_ctx.errorText.clear();
    SetScreen(Screen::UninstConfirm);
    g_ctx.progress.store(0);

    // Cache install dir + self path for the worker to pick up when the
    // user clicks Yes. These are copied by value into the worker so we
    // don't hit lifetime problems.
    static std::wstring s_installDir;
    static std::wstring s_selfPath;
    s_installDir = installDir.wstring();
    s_selfPath   = fs::path(selfPath).wstring();

    // We store the "user clicked Yes" trigger on ctx so WndProc's
    // WM_LBUTTONDOWN handler can spawn the worker.
    // (The rcUninstYes / rcUninstNo hit rects come from paint code.)

    // Build the window using the same infrastructure WinMain uses for
    // the installer. Duplicated here (instead of factored) so nothing
    // in the install path has to change to accommodate the uninstall.
    WNDCLASSEXW wc = {};
    wc.cbSize        = sizeof(wc);
    wc.lpfnWndProc   = WndProc;
    wc.hInstance     = hInst;
    wc.hCursor       = LoadCursor(nullptr, IDC_ARROW);
    wc.hbrBackground = nullptr;
    wc.lpszClassName = L"LogicBoardUninstallerWnd";
    wc.hIcon         = static_cast<HICON>(LoadImageW(hInst,
        MAKEINTRESOURCEW(100), IMAGE_ICON, 0, 0, LR_DEFAULTSIZE));
    RegisterClassExW(&wc);

    HDC screen = GetDC(nullptr);
    g_dpi = GetDeviceCaps(screen, LOGPIXELSX);
    ReleaseDC(nullptr, screen);
    int w = Scale(WIN_W), h = Scale(WIN_H);
    int sw = GetSystemMetrics(SM_CXSCREEN);
    int sh = GetSystemMetrics(SM_CYSCREEN);
    HWND hwnd = CreateWindowExW(
        WS_EX_APPWINDOW,
        wc.lpszClassName,
        L"LogicBoard Uninstaller",
        WS_POPUP | WS_VISIBLE,
        (sw - w) / 2, (sh - h) / 2, w, h,
        nullptr, nullptr, hInst, nullptr);
    if (!hwnd) return -1;
    g_ctx.hwnd = hwnd;
    EnableWindowShadow(hwnd);
    ShowWindow(hwnd, SW_SHOW);
    UpdateWindow(hwnd);

    MSG msg;
    while (GetMessageW(&msg, nullptr, 0, 0) > 0) {
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }
    return static_cast<int>(msg.wParam);
}

// Kicks off the uninstall worker thread. Called from WndProc when the
// user hits "Yes, uninstall" on the confirm screen.
static void StartUninstallThread() {
    SetScreen(Screen::UninstWorking);
    g_ctx.progress.store(0);
    SetStatus(L"Preparing...");
    RequestRepaint();

    wchar_t selfPath[MAX_PATH] = {};
    GetModuleFileNameW(nullptr, selfPath, MAX_PATH);
    std::wstring installDir_s =
        fs::path(selfPath).parent_path().wstring();
    std::wstring selfPath_s = std::wstring(selfPath);

    std::thread(UninstallWorker, installDir_s, selfPath_s).detach();
}

// -----------------------------------------------------------------------------
// Window procedure
// -----------------------------------------------------------------------------
static LRESULT CALLBACK WndProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    switch (msg) {
        case WM_ERASEBKGND:
            // OnPaint fully covers the client area via a double-buffered
            // blit, so skip the default erase to avoid flicker.
            return 1;

        case WM_PAINT:
            OnPaint(hwnd);
            return 0;

        case WM_LBUTTONDOWN: {
            POINT pt = { GET_X_LPARAM(lp), GET_Y_LPARAM(lp) };
            if (PtInRect(&g_ctx.rcClose, pt)) {
                if (CurScreen() == Screen::Installing ||
                    CurScreen() == Screen::UninstWorking) {
                    // Mid-operation close guard — leaving now could
                    // leave a half-extracted install or a half-cleaned
                    // registry behind.
                    return 0;
                }
                DestroyWindow(hwnd);
                return 0;
            }
            switch (CurScreen()) {
                case Screen::Welcome:
                    if (PtInRect(&g_ctx.rcNext, pt)) StartInstallThread();
                    break;
                case Screen::Done:
                    if (PtInRect(&g_ctx.rcLaunch, pt)) {
                        // Launch LogicBoard through explorer.exe rather
                        // than directly. The installer normally runs
                        // unelevated now, so this is usually equivalent —
                        // but nothing stops someone right-clicking "Run as
                        // administrator", and a plain ShellExecute would
                        // then hand LogicBoard an elevated token, so the app
                        // and its LogicShare service would run as admin and
                        // write admin-owned files into the user's profile.
                        // Asking explorer to start it means the child always
                        // gets the ordinary medium-integrity user token.
                        fs::path exe = InstallDir() / APP_EXE;
                        // "explorer.exe <path>" launches the target as
                        // the logged-in user, not elevated.
                        ShellExecuteW(nullptr, L"open", L"explorer.exe",
                                       exe.wstring().c_str(),
                                       nullptr, SW_SHOWNORMAL);
                        DestroyWindow(hwnd);
                    } else if (PtInRect(&g_ctx.rcCloseBtn, pt)) {
                        DestroyWindow(hwnd);
                    }
                    break;
                case Screen::Error:
                    if (PtInRect(&g_ctx.rcCloseBtn, pt))
                        DestroyWindow(hwnd);
                    break;
                case Screen::UninstConfirm:
                    if (PtInRect(&g_ctx.rcUninstYes, pt)) {
                        StartUninstallThread();
                    } else if (PtInRect(&g_ctx.rcUninstNo, pt)) {
                        // User bailed on the uninstall — close the
                        // window and end the process cleanly.
                        DestroyWindow(hwnd);
                    }
                    break;
                case Screen::UninstWorking:
                    // Intentionally no interactive buttons — user has
                    // to wait out the removal. The X in the title bar
                    // is guarded below.
                    break;
                case Screen::UninstDone:
                    if (PtInRect(&g_ctx.rcUninstDone, pt))
                        DestroyWindow(hwnd);
                    break;
                default:
                    break;
            }
            return 0;
        }

        case WM_NCHITTEST: {
            // Let the user drag the frameless window by the top strip.
            POINT pt = { GET_X_LPARAM(lp), GET_Y_LPARAM(lp) };
            ScreenToClient(hwnd, &pt);
            if (PtInRect(&g_ctx.rcClose, pt)) return HTCLIENT;
            if (CurScreen() == Screen::Welcome && PtInRect(&g_ctx.rcNext, pt))
                return HTCLIENT;
            if (CurScreen() == Screen::Done &&
                (PtInRect(&g_ctx.rcLaunch,   pt) ||
                 PtInRect(&g_ctx.rcCloseBtn, pt)))
                return HTCLIENT;
            if (CurScreen() == Screen::Error &&
                PtInRect(&g_ctx.rcCloseBtn, pt))
                return HTCLIENT;
            if (CurScreen() == Screen::UninstConfirm &&
                (PtInRect(&g_ctx.rcUninstYes, pt) ||
                 PtInRect(&g_ctx.rcUninstNo,  pt)))
                return HTCLIENT;
            if (CurScreen() == Screen::UninstDone &&
                PtInRect(&g_ctx.rcUninstDone, pt))
                return HTCLIENT;
            if (pt.y < Scale(40)) return HTCAPTION;
            return HTCLIENT;
        }

        case WM_SETCURSOR: {
            POINT pt; GetCursorPos(&pt);
            ScreenToClient(hwnd, &pt);
            RECT* hovers[] = {
                &g_ctx.rcClose, &g_ctx.rcNext,
                &g_ctx.rcLaunch, &g_ctx.rcCloseBtn,
                &g_ctx.rcUninstYes, &g_ctx.rcUninstNo,
                &g_ctx.rcUninstDone };
            for (auto* r : hovers) {
                if (PtInRect(r, pt)) {
                    SetCursor(LoadCursor(nullptr, IDC_HAND));
                    return TRUE;
                }
            }
            break;
        }

        case WM_CLOSE:
            // Block Alt+F4 / system menu close while an install or
            // uninstall is in progress — tearing the window down now would
            // end the message loop and kill the detached worker thread
            // mid-operation, leaving a half-installed or half-removed
            // state. The user must wait for it to finish (or fail).
            if (CurScreen() == Screen::Installing ||
                CurScreen() == Screen::UninstWorking) {
                return 0;
            }
            DestroyWindow(hwnd);
            return 0;

        case WM_DESTROY:
            PostQuitMessage(0);
            return 0;
    }
    return DefWindowProcW(hwnd, msg, wp, lp);
}

// -----------------------------------------------------------------------------
// WinMain
// -----------------------------------------------------------------------------
int WINAPI wWinMain(HINSTANCE hInst, HINSTANCE, LPWSTR cmdLine, int) {
    // Common setup for both installer and uninstaller — COM (for
    // IShellLink), GDI+ (for painting), the app icon (for the logo),
    // and DPI awareness. The uninstaller now shares the installer's
    // Aero window, so it needs all the same infrastructure.
    CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);

    // The elevated helper. Runs with no window, no GDI+, and no message
    // loop — it exists for the seconds it takes to delete a Program Files
    // install, and its exit code is the only thing the parent reads.
    if (cmdLine && wcsstr(cmdLine, REMOVE_LEGACY_SWITCH)) {
        fs::path newExe;
        // The new exe path arrives quoted, as the only quoted argument.
        if (const wchar_t* q1 = wcschr(cmdLine, L'"')) {
            if (const wchar_t* q2 = wcschr(q1 + 1, L'"'))
                newExe = std::wstring(q1 + 1, static_cast<size_t>(q2 - q1 - 1));
        }
        int rc = RemoveLegacyInstall(newExe);
        CoUninitialize();
        return rc;
    }

    // Uninstall route, part one: administrator rights.
    //
    // Checked HERE — before GDI+, the icon and the window class — because a
    // process whose entire job is to raise a consent prompt and wait has no
    // business building a UI first. The elevated child paints everything,
    // confirm screen included, so the user sees one window and not two.
    const bool uninstallMode = cmdLine && (wcsstr(cmdLine, L"/uninstall")
                                        || wcsstr(cmdLine, L"--uninstall"));
    if (uninstallMode && !wcsstr(cmdLine, ELEVATED_SWITCH)
            && !IsProcessElevated()) {
        int rc = ElevateForUninstall();
        // Declining is a legitimate answer, but silence after clicking
        // Uninstall in Settings reads as a broken uninstaller, so say what
        // happened. MessageBox rather than the Aero window: GDI+ is not
        // running yet, and starting it to show one line would be absurd.
        if (rc == UNINST_EXIT_DECLINED) {
            MessageBoxW(nullptr,
                L"Uninstalling LogicBoard needs administrator permission.\n\n"
                L"Nothing has been removed.",
                L"LogicBoard Uninstaller",
                MB_OK | MB_ICONINFORMATION | MB_TASKMODAL);
        } else if (rc == UNINST_EXIT_NO_ELEV) {
            MessageBoxW(nullptr,
                L"Could not ask for administrator permission, so the "
                L"uninstall did not start.\n\nNothing has been removed.",
                L"LogicBoard Uninstaller",
                MB_OK | MB_ICONERROR | MB_TASKMODAL);
        }
        CoUninitialize();
        return rc;
    }

    GdiplusStartupInput gdiplusInput;
    GdiplusStartup(&g_gdiplusToken, &gdiplusInput, nullptr);
    INITCOMMONCONTROLSEX icc = { sizeof(icc), ICC_STANDARD_CLASSES };
    InitCommonControlsEx(&icc);
    SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    g_ctx.logo = LoadEmbeddedIconAsBitmap();

    // Uninstall route, part two — the actual run, AFTER the shared init so
    // the uninstaller has a working GDI+ + icon to paint with. Reaching
    // here means we are elevated (or were launched that way already).
    if (uninstallMode) {
        // Whoever asked, so the per-user half of the cleanup goes to their
        // profile and not to the administrator whose credentials may have
        // been typed into the prompt. Empty when we elevated in place,
        // which is the common case and means "same user".
        g_invokingUserSid = ParseUserSid(cmdLine);
        int rc = RunUninstaller(hInst);
        if (g_ctx.logo) { delete g_ctx.logo; g_ctx.logo = nullptr; }
        GdiplusShutdown(g_gdiplusToken);
        CoUninitialize();
        return rc;
    }

    // Is LogicBoard already on this machine? Decided ONCE, here, before the
    // first frame is painted — the Welcome screen's heading, blurb and
    // primary button all read from it, and a value that changed mid-run
    // would relabel the button under the user's cursor.
    g_existing = DetectPerUserInstall();
    g_legacy   = DetectLegacyInstall();

    WNDCLASSEXW wc = {};
    wc.cbSize        = sizeof(wc);
    wc.lpfnWndProc   = WndProc;
    wc.hInstance     = hInst;
    wc.hCursor       = LoadCursor(nullptr, IDC_ARROW);
    wc.hbrBackground = nullptr;
    wc.lpszClassName = L"LogicBoardInstallerWnd";
    wc.hIcon         = static_cast<HICON>(LoadImageW(hInst,
        MAKEINTRESOURCEW(100), IMAGE_ICON, 0, 0, LR_DEFAULTSIZE));
    RegisterClassExW(&wc);

    // DPI-scaled window size.
    HDC screen = GetDC(nullptr);
    g_dpi = GetDeviceCaps(screen, LOGPIXELSX);
    ReleaseDC(nullptr, screen);
    int w = Scale(WIN_W), h = Scale(WIN_H);

    // Centre on the primary monitor.
    int sw = GetSystemMetrics(SM_CXSCREEN);
    int sh = GetSystemMetrics(SM_CYSCREEN);
    int x = (sw - w) / 2, y = (sh - h) / 2;

    HWND hwnd = CreateWindowExW(
        WS_EX_APPWINDOW,
        wc.lpszClassName,
        IsUpdateMode() ? L"LogicBoard Updater" : L"LogicBoard Installer",
        WS_POPUP | WS_VISIBLE,
        x, y, w, h,
        nullptr, nullptr, hInst, nullptr);
    if (!hwnd) return -1;
    g_ctx.hwnd = hwnd;

    EnableWindowShadow(hwnd);
    ShowWindow(hwnd, SW_SHOW);
    UpdateWindow(hwnd);

    MSG msg;
    while (GetMessageW(&msg, nullptr, 0, 0) > 0) {
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }

    if (g_ctx.logo) { delete g_ctx.logo; g_ctx.logo = nullptr; }
    GdiplusShutdown(g_gdiplusToken);
    CoUninitialize();
    return static_cast<int>(msg.wParam);
}
