@echo off
REM ============================================================================
REM build.bat — Builds LogicBoardInstaller.exe as a single-file installer.
REM
REM Run from a "x64 Native Tools Command Prompt for VS 2022" (or later) so
REM cl.exe / rc.exe / link.exe / mt.exe are all on PATH. If you don't have
REM Visual Studio, the Visual C++ Build Tools are enough — download from
REM https://visualstudio.microsoft.com/visual-cpp-build-tools/ and check
REM "Desktop development with C++" during install.
REM
REM Before running, place these two payload files next to this script:
REM     LogicBoard.zip   — zipped LogicBoard app tree (with LogicBoard.exe
REM                        at the root)
REM     logo.ico          — the LogicBoard icon
REM ============================================================================
setlocal enabledelayedexpansion

if not exist LogicBoard.zip (
    echo [!] LogicBoard.zip is missing. Drop it in this folder and rerun.
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

echo === Compiling LogicBoardInstaller.cpp ===
cl /nologo /c /EHsc /std:c++17 /O2 /MT /W3 LogicBoardInstaller.cpp
if errorlevel 1 goto :fail

echo === Linking ===
link /nologo /SUBSYSTEM:WINDOWS /OUT:LogicBoardInstaller.exe ^
    /MANIFEST:EMBED /MANIFESTUAC:NO /MANIFESTINPUT:manifest.xml ^
    LogicBoardInstaller.obj miniz.obj resources.res ^
    user32.lib gdi32.lib gdiplus.lib shell32.lib shlwapi.lib ^
    ole32.lib oleaut32.lib comctl32.lib dwmapi.lib uxtheme.lib ^
    advapi32.lib propsys.lib msimg32.lib uuid.lib
if errorlevel 1 goto :fail

echo.
echo === BUILD OK ===
echo Output: %CD%\LogicBoardInstaller.exe
echo.
dir /b LogicBoardInstaller.exe
exit /b 0

:fail
echo.
echo === BUILD FAILED ===
exit /b 1
