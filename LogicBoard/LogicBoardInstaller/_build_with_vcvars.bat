@echo off
REM Wrapper: set UTF-8 codepage (the project path contains an emoji, which
REM cmd cannot address under the legacy OEM codepage), initialise the MSVC
REM x64 environment, then run build.bat from this folder.
REM
REM build.bat is invoked by ABSOLUTE path: cmd resolves a relative command
REM name against an OEM-encoded copy of the current directory, and that
REM conversion fails on the emoji, so "call build.bat" reports "not
REM recognized" even though the CWD is right and dir can see the file.
chcp 65001 >nul
call "C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat" >nul
cd /d "%~dp0"
call "%~dp0build.bat"
exit /b %errorlevel%
