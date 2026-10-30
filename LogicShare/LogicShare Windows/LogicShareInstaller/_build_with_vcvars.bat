@echo off
REM Wrapper: set UTF-8 codepage (the project path contains an emoji, which
REM cmd cannot address under the legacy OEM codepage), initialise the MSVC
REM x64 environment, then run build.bat from this folder.
REM
REM build.bat is invoked by ABSOLUTE path: cmd resolves a relative command
REM name against an OEM-encoded copy of the current directory, and that
REM conversion fails on the emoji, so "call build.bat" reports "not
REM recognized" even though the CWD is right and dir can see the file.
REM
REM vcvars64.bat is called BEFORE the cd into this folder, on purpose. It
REM shells out to vswhere and other tools that inherit the working
REM directory, and those inherit the same emoji problem if the cd happens
REM first.
chcp 65001 >nul
call "C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat" >nul
cd /d "%~dp0"
call "%~dp0build.bat"
exit /b %errorlevel%
