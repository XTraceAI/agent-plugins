:; SELF=$0; DIR=$(CDPATH= cd -- "$(dirname -- "$SELF")" && pwd) || exit 0; ROOT=$(CDPATH= cd -- "$DIR/.." && pwd); STATE="$HOME/.config/memhub-plugin"; if [ -f "$ROOT/scripts/cursor_capture.py" ]; then mkdir -p "$STATE" 2>/dev/null; cp "$SELF" "$STATE/cursor_capture.cmd" 2>/dev/null; printf '%s\n' "$ROOT" > "$STATE/cursor-root" 2>/dev/null; else ROOT=$(cat "$STATE/cursor-root" 2>/dev/null); [ -n "$ROOT" ] && cmp -s "$SELF" "$ROOT/hooks/cursor_capture.cmd" || { printf '{"permission":"allow"}\n'; exit 0; }; fi; PY=$(command -v python3 || command -v python); [ -n "$PY" ] || { printf '{"permission":"allow"}\n'; exit 0; }; exec "$PY" "$ROOT/scripts/cursor_capture.py" "$@"
@echo off
rem Batch above, POSIX shell on the polyglot first line.
setlocal
set "STATE=%USERPROFILE%\.config\memhub-plugin"
for %%I in ("%~dp0..") do set "ROOT=%%~fI"
if not exist "%ROOT%\scripts\cursor_capture.py" goto stable_root
if not exist "%STATE%" mkdir "%STATE%" >nul 2>&1
copy /y "%~f0" "%STATE%\cursor_capture.cmd" >nul 2>&1
<nul >"%STATE%\cursor-root" set /p "=%ROOT%"
goto launch
:stable_root
set "ROOT="
if not exist "%STATE%\cursor-root" goto allow
set /p ROOT=<"%STATE%\cursor-root"
if not defined ROOT goto allow
if not exist "%ROOT%\scripts\cursor_capture.py" goto allow
fc /b "%~f0" "%ROOT%\hooks\cursor_capture.cmd" >nul 2>&1
if errorlevel 1 goto allow
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
echo {"permission":"allow"}
exit /b 0
