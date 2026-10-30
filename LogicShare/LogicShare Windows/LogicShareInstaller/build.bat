@echo off
REM ============================================================================
REM build.bat — Builds LogicShareInstaller.exe as a single-file installer.
REM
REM Run from a "x64 Native Tools Command Prompt for VS 2022" (or later) so
REM cl.exe / rc.exe / link.exe are all on PATH. If you don't have Visual
REM Studio, the Visual C++ Build Tools are enough — download from
REM https://visualstudio.microsoft.com/visual-cpp-build-tools/ and check
REM "Desktop development with C++" during install.
REM
REM Before running, place these two payload files next to this script:
REM     LogicShare.zip   — zipped LogicShare app tree (with LogicShare.exe
REM                        at the root)
REM     logo.ico         — the LogicShare icon
REM
REM `package_installer.py` in the parent folder does both of those for you,
REM then calls this script.
REM ============================================================================
setlocal enabledelayedexpansion

if not exist LogicShare.zip (
    echo [!] LogicShare.zip is missing. Drop it in this folder and rerun.
    exit /b 1
)
if not exist logo.ico (
    echo [!] logo.ico is missing. Drop it in this folder and rerun.
    exit /b 1
)

echo === Compiling resources.rc ===
rc /nologo /fo resources.res resources.rc
if errorlevel 1 goto :fail

echo === Compiling miniz ===
cl /nologo /c /O2 /MT /DMINIZ_NO_ZLIB_APIS miniz.c
if errorlevel 1 goto :fail

echo === Compiling LogicShareInstaller.cpp ===
cl /nologo /c /EHsc /std:c++17 /O2 /MT /W3 LogicShareInstaller.cpp
if errorlevel 1 goto :fail

echo === Linking ===
link /nologo /SUBSYSTEM:WINDOWS /OUT:LogicShareInstaller.exe ^
    /MANIFEST:EMBED /MANIFESTUAC:NO /MANIFESTINPUT:manifest.xml ^
    LogicShareInstaller.obj miniz.obj resources.res ^
    user32.lib gdi32.lib gdiplus.lib shell32.lib shlwapi.lib ^
    ole32.lib oleaut32.lib comctl32.lib dwmapi.lib uxtheme.lib ^
    advapi32.lib propsys.lib msimg32.lib uuid.lib
if errorlevel 1 goto :fail

echo.
echo === BUILD OK ===
echo Output: %CD%\LogicShareInstaller.exe
echo.
dir /b LogicShareInstaller.exe
exit /b 0

:fail
echo.
echo === BUILD FAILED ===
exit /b 1
