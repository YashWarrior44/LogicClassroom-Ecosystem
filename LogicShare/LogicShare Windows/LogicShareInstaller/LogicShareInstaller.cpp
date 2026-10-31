// =============================================================================
// LogicShareInstaller.cpp
//
// GENERATED from LogicBoardInstaller.cpp — see
// LogicShareInstaller/derive_from_logicboard.py. The two
// installers are one program with two product identities, so fixes to the
// install pipeline belong in the LogicBoard source and get re-derived here
// rather than being applied twice.
//
// Single-file Windows installer for LogicShare, with an Aero-glass modern UI.
//
//   * Frameless translucent window using DWM's system-backdrop APIs
//     (Windows 11 acrylic on 22H2+, Aero blur fallback on 10 / early 11).
//   * Three screens: Welcome -> Installing -> Done, plus an Error surface
//     shown if anything goes wrong along the way. All painted with GDI+
//     for smooth rounded panels + anti-aliased text.
//   * ZIP payload + icon are BAKED INTO THE EXE via a .rc resource file
//     (see resources.rc), so the shipped installer is truly a single file.
//   * Requires admin elevation (see manifest.xml embedded via linker).
//   * Installs to C:\Program Files\LogicShare, fixed — no path picker.
//   * Creates Desktop + Start Menu shortcuts (IShellLink / IPersistFile).
//   * Registers under HKLM Uninstall so LogicShare shows up in
//     Apps & Features and is reachable via Windows Search.
//   * Self-copies to the install dir as ``Uninstaller.exe``; invoking it
//     with ``/uninstall`` reverses everything.
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
constexpr wchar_t APP_NAME[]       = L"LogicShare";
constexpr wchar_t APP_PUBLISHER[]  = L"LogicClassroom";       // shown in Apps & Features
// Version lives in a macro, not just the constexpr array, because some
// UI strings paste it into an adjacent-literal concatenation
// (L"...v" APP_VERSION_TEXT L"...") and a constexpr array cannot be used
// that way. The array is derived from the macro so they cannot drift.
#define APP_VERSION_TEXT L"1.1.3"
constexpr wchar_t APP_VERSION[]    = APP_VERSION_TEXT;
constexpr wchar_t APP_EXE[]        = L"LogicShare.exe";
// AppUserModelID — an internal, stable Windows grouping id (taskbar
// grouping, pinned-icon identity). NOT a user-visible company name.
// Renamed off the old "InnovaLogiX" token per the branding change.
constexpr wchar_t APP_AUMID[]      = L"InnovaLogiX.LogicShare";
// Resource names — must match resources.rc.
constexpr wchar_t RC_ZIP_NAME[]    = L"LOGICSHARE_ZIP";
// (The ZIP's resource TYPE is RT_RCDATA; we pass MAKEINTRESOURCEW(10)
// inline at the FindResource call sites rather than storing a string,
// since FindResourceW wants the integer resource-type atom there.)
// Fixed install destination — no path picker.
constexpr wchar_t INSTALL_SUBDIR[] = L"LogicShare";

// The app files live in a VERSIONED subfolder of the install root:
//
//     C:\Program Files\LogicShare\              <- install root
//         Uninstaller.exe                       <- survives every update
//         logo.ico
//         LogicShare_V3.1.0\                    <- this build
//             LogicShare.exe
//             _internal\
//
// This is not decoration. The in-app updater installs each new version
// into ``<install root>\LogicShare_V<new>`` beside the current one and
// deletes the older siblings once the new one runs — a scheme that is
// crash-safe (a failed download never touches the working install) but
// only works if THIS installer lays the first version down the same way.
// Installing flat meant the very first install had no version folder, so
// the updater nested the new version INSIDE it and could never identify
// the original ~240 MB copy as something to clean up. It stayed forever.
//
// Uninstaller.exe and logo.ico deliberately stay at the ROOT: both are
// referenced by registry values that must outlive any single version, and
// anything inside a version folder is by definition something the updater
// is allowed to delete.
static std::wstring VersionedFolderName() {
    return std::wstring(APP_NAME) + L"_V" + APP_VERSION;
}

// Uninstall registry key path (HKLM).
constexpr wchar_t UNINSTALL_KEY[]  =
    L"Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\LogicShare";

// Startup registry value — under HKLM Run so LogicShare launches at boot
// to start its LogicShare background service. Value name is the product
// name; value data is the quoted exe path + a --background flag.
constexpr wchar_t RUN_KEY[]        =
    L"Software\\Microsoft\\Windows\\CurrentVersion\\Run";
constexpr wchar_t RUN_VALUE_NAME[] = L"LogicShare";

// Window sizing (design units — DPI-scaled at runtime).
constexpr int WIN_W = 720;
constexpr int WIN_H = 500;

// -----------------------------------------------------------------------------
// Existing-install detection
//
// The same binary is BOTH the first-time installer and the updater — the
// install pipeline is identical either way (wipe the old version folders,
// lay the new one down, re-point every shortcut and registry value). What
// changes is only what we TELL the user: showing "Install LogicShare" to
// someone who already has 3.0.0 on the machine reads as "this will give you
// a second copy", which is the one thing it will not do.
//
// The probe is deliberately two-tiered:
//   1. HKLM Uninstall\LogicShare — the authoritative record, and the only
//      place the previously-installed VERSION is written down.
//   2. A LogicShare.exe actually present under %ProgramFiles%\LogicShare.
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

static ExistingInstall g_existing;

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

static ExistingInstall DetectExistingInstall() {
    ExistingInstall e;

    e.version  = RegReadString(HKEY_LOCAL_MACHINE, UNINSTALL_KEY,
                                L"DisplayVersion");
    e.location = RegReadString(HKEY_LOCAL_MACHINE, UNINSTALL_KEY,
                                L"InstallLocation");
    if (!e.version.empty() || !e.location.empty())
        e.present = true;

    // Filesystem cross-check. Look for the exe anywhere one build deep —
    // it lives in a versioned subfolder now, but flat installs from older
    // builds put it at the root, and both count.
    std::error_code ec;
    wchar_t progFiles[MAX_PATH] = {};
    if (SUCCEEDED(SHGetFolderPathW(nullptr, CSIDL_PROGRAM_FILES,
                                    nullptr, 0, progFiles))) {
        fs::path root = fs::path(progFiles) / INSTALL_SUBDIR;
        if (fs::exists(root / APP_EXE, ec)) {
            e.present = true;
            if (e.location.empty()) e.location = root.wstring();
        } else if (fs::exists(root, ec)) {
            for (auto& entry : fs::directory_iterator(root, ec)) {
                if (ec) break;
                if (!entry.is_directory(ec)) continue;
                if (fs::exists(entry.path() / APP_EXE, ec)) {
                    e.present = true;
                    if (e.location.empty()) e.location = root.wstring();
                    // Folder name is "LogicShare_V3.0.0" — recover the
                    // version from it when the registry didn't have one
                    // (hand-cleaned registry, or an install that predates
                    // the Uninstall key).
                    if (e.version.empty()) {
                        std::wstring leaf = entry.path().filename().wstring();
                        std::wstring prefix = std::wstring(APP_NAME) + L"_V";
                        if (leaf.size() > prefix.size() &&
                            leaf.compare(0, prefix.size(), prefix) == 0)
                            e.version = leaf.substr(prefix.size());
                    }
                    break;
                }
            }
        }
    }
    return e;
}

// True when this run is an upgrade/reinstall rather than a first install.
// Every piece of user-facing copy branches on exactly this.
static inline bool IsUpdateMode() { return g_existing.present; }

// "Install LogicShare" / "Update LogicShare" — the primary button, and the
// single most important word on the Welcome screen.
static std::wstring PrimaryActionLabel() {
    return IsUpdateMode() ? L"Update LogicShare" : L"Install LogicShare";
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
        DrawTextGdi(g, L"LogicShare", nbox, fontName, kTextPri,
                  StringAlignmentCenter, StringAlignmentCenter);
        Font fontTag(L"Segoe UI", ScaleF(11.5f), FontStyleRegular, UnitPixel);
        RectF tgbox(0.0f, static_cast<REAL>((bodyH) / 2 + Scale(98)),
                      static_cast<REAL>(leftW), static_cast<REAL>(Scale(20)));
        DrawTextGdi(g, L"Send files straight to the board", tgbox, fontTag,
                  kTextDim, StringAlignmentCenter, StringAlignmentCenter);
    }

    // RIGHT PANE — creator info + features + about.
    {
        int rx = leftW + Scale(36);     // right pane content x
        int rw = w - rx - Scale(36);    // right pane content width
        int y  = Scale(40);

        // Heading — the update path says so up front. Someone who already
        // has LogicShare on the board should not have to infer from a
        // progress bar that this is an upgrade rather than a second copy.
        Font fontH1(L"Segoe UI Semibold", ScaleF(24.0f), FontStyleRegular, UnitPixel);
        RectF h1(static_cast<REAL>(rx), static_cast<REAL>(y),
                   static_cast<REAL>(rw), static_cast<REAL>(Scale(34)));
        DrawTextGdi(g,
                  IsUpdateMode() ? L"Update LogicShare"
                                 : L"Welcome to LogicShare",
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
            subLine = L"Free, open-source file sharing for classrooms";
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
                L"build. Your teacher profile, announcement history and "
                L"shortcuts stay exactly where they are - only the app "
                L"itself is swapped."
              : L"LogicShare lets teachers send files and announcements to the "
                L"classroom Smart Board straight from their own laptop - no "
                L"cables, no USB sticks. Free to use, share and improve.",
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
            L"Finds every Smart Board on the Wi-Fi automatically",
            L"Send PDFs, slides, documents, images & video",
            L"Broadcast announcements to one board or all of them",
            L"Live transfer progress with speed + time remaining",
            L"Updates itself straight from GitHub",
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
              IsUpdateMode() ? L"Updating LogicShare..."
                             : L"Installing LogicShare...",
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
                ? L"LogicShare is now on v" APP_VERSION_TEXT L".\nHit launch to open it now."
                : L"LogicShare is installed and ready to go.\nHit launch to open it now.",
              sbox, fontSub, kTextSec,
              StringAlignmentCenter, StringAlignmentNear);

    // Buttons: [Close]  [Launch LogicShare]
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
    DrawButtonFilled(g, ctx->rcLaunch, L"Launch LogicShare",
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
    DrawTextGdi(g, L"Uninstall LogicShare?", hbox, fontHero,
              kTextPri, StringAlignmentCenter, StringAlignmentCenter);
    Font fontSub(L"Segoe UI", ScaleF(13.0f), FontStyleRegular, UnitPixel);
    RectF sbox(static_cast<REAL>(Scale(60)),
                 static_cast<REAL>(Scale(260)),
                 static_cast<REAL>(w - Scale(120)),
                 static_cast<REAL>(Scale(70)));
    DrawTextGdi(g,
        L"This will completely remove LogicShare from your PC.\n"
        L"Your profile and announcement history will stay untouched.",
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
    DrawTextGdi(g, L"Removing LogicShare...", hbox, fontHero,
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
        L"LogicShare has been removed from your PC.\nSee you next time.",
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
        errOut = L"Couldn't load the bundled LogicShare payload.";
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
    // If someone zipped the LogicShare FOLDER (so every path looks like
    // "LogicShare/LogicShare.exe") rather than its CONTENTS, we'd end up
    // installing to C:\Program Files\LogicShare\LogicShare\... — a
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
        errOut = L"Couldn't create the install directory. "
                  L"Try running the installer as administrator.";
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
                // (LogicShare still running, or an antivirus holding the
                // exe open mid-scan), and the installer already elevates
                // via its manifest, so "run as administrator" is rarely
                // the answer and reads as a dead end when it isn't.
                errOut = L"Couldn't write \"" + fs::path(wname).filename().wstring() +
                          L"\" to the install folder.\n\n"
                          L"The file is most likely in use. Close LogicShare "
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

// ``installDir`` is the install ROOT (what uninstall removes and what
// Apps & Features reports); ``appDir`` is the versioned folder holding this
// build's exe. They differ, and which one each registry value wants is not
// interchangeable: InstallLocation must survive an update, while App Paths
// has to point at the exe that exists right now.
static bool RegisterUninstallEntry(const fs::path& installDir,
                                     const fs::path& appDir,
                                     const fs::path& iconPath) {
    // HKLM Uninstall key — the one Apps & Features / Settings reads.
    HKEY hKey = nullptr;
    LONG r = RegCreateKeyExW(HKEY_LOCAL_MACHINE, UNINSTALL_KEY, 0,
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

    // Register App Paths so Windows Search / the Run dialog surface
    // "LogicShare" for user queries. We write BOTH hives:
    //   * HKLM — machine-wide, the canonical location (we're elevated).
    //   * HKCU — indexed faster + always readable by the search service,
    //     so the app shows up in taskbar/Start search promptly instead
    //     of waiting on the Start Menu shortcut to be indexed.
    const wchar_t* appPathsSub =
        L"Software\\Microsoft\\Windows\\CurrentVersion\\App Paths\\LogicShare.exe";
    // The versioned folder, not the root — this is a path to an EXE that
    // has to exist, and after this install the root holds no exe at all.
    std::wstring exePath = (appDir / APP_EXE).wstring();
    std::wstring dirPath = appDir.wstring();
    for (HKEY root : { HKEY_LOCAL_MACHINE, HKEY_CURRENT_USER }) {
        HKEY hApp = nullptr;
        if (RegCreateKeyExW(root, appPathsSub, 0, nullptr, 0,
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
    }

    return true;
}

// Defined further down (next to the uninstall path, which has always used
// it). Declared here because the INSTALL path needs it too — see the call
// at the top of RunInstall for why.
static void TerminateLogicShare();

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
        wchar_t progFiles[MAX_PATH] = {};
        // CSIDL_PROGRAM_FILES — 64-bit Program Files on 64-bit Windows.
        if (FAILED(SHGetFolderPathW(nullptr, CSIDL_PROGRAM_FILES,
                                     nullptr, 0, progFiles))) {
            std::lock_guard<std::mutex> lk(g_ctx.textLock);
            g_ctx.errorText = L"Couldn't locate Program Files.";
            SetScreen(Screen::Error);
            RequestRepaint();
            return;
        }
        fs::path installDir = fs::path(progFiles) / INSTALL_SUBDIR;
        // Where this build's files actually go. See VersionedFolderName.
        fs::path appDir = installDir / VersionedFolderName();

        // Pre-flight: verify the bundled payloads are actually present in
        // the EXE before we touch Program Files. The ZIP + icon are baked
        // in as resources at build time; if the build was assembled
        // wrong (someone forgot to drop LogicShare.zip / logo.ico next to
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
                    L"(LogicShare.zip / logo.ico were not embedded at "
                    L"build time). Nothing was installed.";
                SetScreen(Screen::Error);
                RequestRepaint();
                return;
            }
        }

        // Step 0 — stop any running LogicShare FIRST.
        //
        // This was the install path's one missing step, and it failed the
        // whole install every time you upgraded without quitting the app.
        // LogicShare keeps running in the tray (the LogicShare background
        // service), so its LogicShare.exe, logicshare_engine .pyd and the
        // MSVC/MinGW DLLs under _internal are all held open by the OS.
        // Windows will not let anyone delete or truncate a mapped image:
        //   * Step 1's remove_all silently failed on every locked file
        //     (the error_code is passed and ignored, by design, so one
        //     stray file can't abort the wipe), then
        //   * extraction hit LogicShare.exe, the ofstream open failed with
        //     a sharing violation, and the whole install rolled back —
        //     reporting "this usually means permissions", which sent you
        //     hunting for an admin problem that was never there.
        // The uninstaller has always called this for exactly this reason;
        // installing over a live copy needs it just as much.
        SetStatus(L"Closing LogicShare...");
        g_ctx.progress.store(2);
        RequestRepaint();
        TerminateLogicShare();

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
        // strip didn't handle, LogicShare.exe might not be at
        // installDir\LogicShare.exe — and silently creating shortcuts to
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
                    L"Extraction finished but LogicShare.exe wasn't found "
                    L"where expected. The bundled ZIP may be packaged "
                    L"incorrectly (LogicShare.exe should sit at the root "
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

        // Strip the embedded LogicShare.zip resource FROM the uninstaller
        // copy. The uninstaller never extracts anything, so carrying the
        // ~700 MB payload just doubles the install footprint AND inflates
        // the size Windows reports in Apps & Features. UpdateResource with
        // a null data pointer deletes the named resource from the copy.
        {
            HANDLE hUpd = BeginUpdateResourceW(
                uninstallerPath.wstring().c_str(), FALSE);
            if (hUpd) {
                // Delete the ZIP payload (name LOGICSHARE_ZIP, type
                // RT_RCDATA = 10). Passing nullptr data + 0 size removes it.
                UpdateResourceW(
                    hUpd,
                    MAKEINTRESOURCEW(10),          // RT_RCDATA
                    RC_ZIP_NAME,                    // "LOGICSHARE_ZIP"
                    MAKELANGID(LANG_NEUTRAL, SUBLANG_NEUTRAL),
                    nullptr, 0);
                // Commit the change. If this fails the uninstaller still
                // works — it's just larger — so we don't treat it as fatal.
                EndUpdateResourceW(hUpd, FALSE);
            }
        }

        // Step 5 — shortcuts.
        // The versioned folder's exe — every shortcut, the Run key and
        // App Paths all resolve here, not to the (now exe-less) root.
        fs::path targetExe = appDir / APP_EXE;

        // Clear artifacts that OLDER installers created and this one no
        // longer does. Installing over an existing copy only ever added
        // shortcuts, so without this an upgrade would keep the duplicate
        // desktop icon and the duplicate startup entry forever — the user
        // would have to fully uninstall first to get rid of them. These
        // removals are harmless on a clean machine (the files won't exist).
        {
            std::error_code oec;
            auto drop = [&](int csidl, const wchar_t* leaf) {
                wchar_t dir[MAX_PATH] = {};
                if (SUCCEEDED(SHGetFolderPathW(nullptr, csidl, nullptr, 0, dir)))
                    fs::remove(fs::path(dir) / leaf, oec);
            };
            // Second desktop icon: the per-user copy. Explorer merges the
            // per-user and all-users desktops, so this showed up alongside
            // the all-users one as an identical twin.
            drop(CSIDL_DESKTOPDIRECTORY, L"LogicShare.lnk");
            // Second startup entry: the Startup-FOLDER shortcut, which
            // Windows lists separately from the Run key.
            drop(CSIDL_STARTUP,        L"LogicShare.lnk");
            drop(CSIDL_COMMON_STARTUP, L"LogicShare.lnk");
        }

        // Desktop shortcut — ALL-USERS desktop only.
        //
        // This used to also write a per-user copy, on the theory that some
        // shell configs might not surface common-desktop items immediately.
        // They do: Explorer MERGES the common desktop and the user desktop
        // into the single desktop view you actually look at. Writing to both
        // therefore doesn't add a fallback, it guarantees TWO identical
        // LogicShare icons side by side — which is exactly what was being
        // reported. One shortcut, in the all-users location, is visible to
        // every account on the board, which is what we want in a classroom.
        {
            wchar_t commonDesk[MAX_PATH] = {};
            if (SUCCEEDED(SHGetFolderPathW(nullptr,
                    CSIDL_COMMON_DESKTOPDIRECTORY, nullptr, 0, commonDesk))) {
                CreateShellShortcut(fs::path(commonDesk) / L"LogicShare.lnk",
                                     targetExe, iconPath, L"LogicShare");
            }
        }

        // Start Menu — put the shortcut inside a "LogicShare" SUBFOLDER
        // under Programs (i.e. ...\Start Menu\Programs\LogicShare\
        // LogicShare.lnk). A shortcut in its own Start-Menu folder is
        // reliably picked up by Windows Search, so LogicShare shows up
        // when you type it into the taskbar / Start search.
        {
            wchar_t startMenu[MAX_PATH] = {};
            if (SUCCEEDED(SHGetFolderPathW(nullptr, CSIDL_COMMON_PROGRAMS,
                                            nullptr, 0, startMenu))) {
                fs::path lbFolder = fs::path(startMenu) / L"LogicShare";
                std::error_code mkec;
                fs::create_directories(lbFolder, mkec);
                CreateShellShortcut(lbFolder / L"LogicShare.lnk",
                                     targetExe, iconPath, L"LogicShare");
            }
        }

        // Startup entry — deliberately NOT registered.
        //
        // LogicShare writes an HKCU\\Run value so the board is listening for
        // incoming files from the moment it boots. LogicShare is the other
        // half of that conversation: it runs on a teacher's own laptop and
        // only does anything while someone is actively sending something. An
        // app with nothing to wait for has no business launching itself at
        // every login, so there is no Run value here.
        //
        // The uninstaller still deletes that value (see UninstallWorker) —
        // cleaning up after a build that did set it costs one API call and
        // means nobody is left with an orphaned startup entry.

        // Step 6 — Apps & Features + Search registration.
        SetStatus(L"Finalising...");
        g_ctx.progress.store(97);
        RegisterUninstallEntry(installDir, appDir, iconPath);

        // Nudge the shell to re-scan associations + newly-created
        // shortcuts NOW, so LogicShare shows up in taskbar / Start
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

// Terminate every running LogicShare.exe. Needed because LogicShare runs
// as a background (LogicShare) process — if we don't kill it first, its
// EXE + DLLs are locked and deletion fails. Uses the Toolhelp snapshot
// API to find matching processes by name and TerminateProcess on each.
// Forward declarations — these functions cross-reference each other so
// the C++ single-pass compiler needs a heads-up before the first use.
static LRESULT CALLBACK WndProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp);
static void StartInstallThread();
static void StartUninstallThread();

static void TerminateLogicShare() {
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

    SetStatus(L"Stopping LogicShare...");
    g_ctx.progress.store(10);
    TerminateLogicShare();

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
    rm_lnk(CSIDL_COMMON_DESKTOPDIRECTORY, L"LogicShare.lnk");
    rm_lnk(CSIDL_DESKTOPDIRECTORY,        L"LogicShare.lnk");
    rm_lnk(CSIDL_COMMON_PROGRAMS,         L"LogicShare.lnk");
    rm_lnk(CSIDL_PROGRAMS,                L"LogicShare.lnk");
    rm_lnk(CSIDL_COMMON_STARTUP,          L"LogicShare.lnk");
    rm_lnk(CSIDL_STARTUP,                 L"LogicShare.lnk");
    // Remove the Start Menu "LogicShare" SUBFOLDER (shortcut inside) that
    // the installer creates for search visibility — both hives.
    auto rm_startmenu_folder = [&](int csidl) {
        wchar_t dir[MAX_PATH] = {};
        if (SUCCEEDED(SHGetFolderPathW(nullptr, csidl, nullptr, 0, dir)))
            fs::remove_all(fs::path(dir) / L"LogicShare", ec);
    };
    rm_startmenu_folder(CSIDL_COMMON_PROGRAMS);
    rm_startmenu_folder(CSIDL_PROGRAMS);

    SetStatus(L"Cleaning registry entries...");
    g_ctx.progress.store(50);
    // Uninstall + App Paths — the elevated installer wrote these to
    // HKLM; the runtime updater may have written HKCU. Remove both.
    RegDeleteKeyW(HKEY_LOCAL_MACHINE, UNINSTALL_KEY);
    RegDeleteKeyW(HKEY_CURRENT_USER,  UNINSTALL_KEY);
    RegDeleteKeyW(HKEY_LOCAL_MACHINE,
        L"Software\\Microsoft\\Windows\\CurrentVersion\\App Paths\\LogicShare.exe");
    RegDeleteKeyW(HKEY_CURRENT_USER,
        L"Software\\Microsoft\\Windows\\CurrentVersion\\App Paths\\LogicShare.exe");
    // Run (startup) value — installer now writes HKCU; older builds wrote
    // HKLM. Remove from both hives.
    for (HKEY root : { HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE }) {
        HKEY hRun = nullptr;
        if (RegOpenKeyExW(root, RUN_KEY, 0, KEY_WRITE, &hRun)
                == ERROR_SUCCESS) {
            RegDeleteValueW(hRun, RUN_VALUE_NAME);
            RegCloseKey(hRun);
        }
    }

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
    wc.lpszClassName = L"LogicShareUninstallerWnd";
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
        L"LogicShare Uninstaller",
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
                        // Launch LogicShare — DE-ELEVATED. The installer
                        // is running as admin; a plain ShellExecute here
                        // would hand LogicShare that elevated token, so
                        // the app + its LogicShare service would run as
                        // admin and write admin-owned files into the
                        // user's profile. Instead we ask explorer.exe to
                        // start it: the child then inherits explorer's
                        // normal medium-integrity user token.
                        wchar_t progFiles[MAX_PATH] = {};
                        SHGetFolderPathW(nullptr, CSIDL_PROGRAM_FILES,
                                          nullptr, 0, progFiles);
                        fs::path exe = fs::path(progFiles)
                                        / INSTALL_SUBDIR
                                        / VersionedFolderName() / APP_EXE;
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
    GdiplusStartupInput gdiplusInput;
    GdiplusStartup(&g_gdiplusToken, &gdiplusInput, nullptr);
    INITCOMMONCONTROLSEX icc = { sizeof(icc), ICC_STANDARD_CLASSES };
    InitCommonControlsEx(&icc);
    SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    g_ctx.logo = LoadEmbeddedIconAsBitmap();

    // Uninstall route — the switch happens AFTER the shared init so
    // the uninstaller has a working GDI+ + icon to paint with.
    if (cmdLine && (wcsstr(cmdLine, L"/uninstall")
                     || wcsstr(cmdLine, L"--uninstall"))) {
        int rc = RunUninstaller(hInst);
        if (g_ctx.logo) { delete g_ctx.logo; g_ctx.logo = nullptr; }
        GdiplusShutdown(g_gdiplusToken);
        CoUninitialize();
        return rc;
    }

    // Is LogicShare already on this machine? Decided ONCE, here, before the
    // first frame is painted — the Welcome screen's heading, blurb and
    // primary button all read from it, and a value that changed mid-run
    // would relabel the button under the user's cursor.
    g_existing = DetectExistingInstall();

    WNDCLASSEXW wc = {};
    wc.cbSize        = sizeof(wc);
    wc.lpfnWndProc   = WndProc;
    wc.hInstance     = hInst;
    wc.hCursor       = LoadCursor(nullptr, IDC_ARROW);
    wc.hbrBackground = nullptr;
    wc.lpszClassName = L"LogicShareInstallerWnd";
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
        IsUpdateMode() ? L"LogicShare Updater" : L"LogicShare Installer",
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
