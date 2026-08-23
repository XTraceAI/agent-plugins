:; STAGED=${2:-}; STAGE_HOME=${3:-$HOME}; cleanup_stage() { P=${1:-}; E=${2:-}; [ -n "$P" ] && [ -n "$E" ] || return; D=$(CDPATH= cd -- "$(dirname -- "$P")" 2>/dev/null && pwd -P) || return; H=$(CDPATH= cd -- "$E" 2>/dev/null && pwd -P) || return; [ "$D" = "$H" ] || return; B=${P##*/}; case "$B" in .memhub-cursor-hook-*.json) M=${B#.memhub-cursor-hook-}; M=${M%.json}; case "$M" in *[!A-Za-z0-9_-]*) return;; esac;; *) return;; esac; rm -f -- "$P" "${P%.json}.out"; }; allow() { cleanup_stage "$STAGED" "$STAGE_HOME"; printf '{"permission":"allow"}\n'; exit 0; }; SELF=$0; DIR=$(CDPATH= cd -- "$(dirname -- "$SELF")" && pwd) || allow; ROOT=$(CDPATH= cd -- "$DIR/.." && pwd); [ -f "$ROOT/scripts/cursor_capture.py" ] || allow; PY=$(command -v python3 || command -v python); [ -n "$PY" ] || allow; exec "$PY" "$ROOT/scripts/cursor_capture.py" "$@"
@echo off
rem Batch above, POSIX shell on the polyglot first line.
rem Cursor plugin hooks resolve ./hooks from the plugin root; this shim then
rem derives every executable path from its own installed location.
setlocal
for %%I in ("%~dp0..") do set "ROOT=%%~fI"
if not exist "%ROOT%\scripts\cursor_capture.py" goto allow
:launch
where py >nul 2>&1
if %errorlevel% equ 0 (
  py -3 "%ROOT%\scripts\cursor_capture.py" "%~1" "%~2"
  if errorlevel 1 goto allow
  exit /b 0
)
where python >nul 2>&1
if %errorlevel% equ 0 (
  python "%ROOT%\scripts\cursor_capture.py" "%~1" "%~2"
  if errorlevel 1 goto allow
  exit /b 0
)
:allow
call :cleanup_stage "%~2" "%~3"
echo {"permission":"allow"}
exit /b 0
:cleanup_stage
if "%~1"=="" exit /b 0
set "EXPECTED_HOME=%~2"
if not defined EXPECTED_HOME set "EXPECTED_HOME=%USERPROFILE%"
for %%H in ("%EXPECTED_HOME%") do set "HOME_DIR=%%~fH\"
if /i not "%~dp1"=="%HOME_DIR%" exit /b 0
if /i not "%~x1"==".json" exit /b 0
set "STEM=%~n1"
if /i not "%STEM:~0,20%"==".memhub-cursor-hook-" exit /b 0
del /f /q "%~f1" 2>nul
for %%F in ("%~f1") do del /f /q "%%~dpnF.out" 2>nul
exit /b 0
