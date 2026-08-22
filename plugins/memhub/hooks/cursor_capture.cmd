:; SELF=$0; DIR=$(CDPATH= cd -- "$(dirname -- "$SELF")" && pwd) || exit 0; ROOT=$(CDPATH= cd -- "$DIR/.." && pwd); [ -f "$ROOT/scripts/cursor_capture.py" ] || { printf '{"permission":"allow"}\n'; exit 0; }; PY=$(command -v python3 || command -v python); [ -n "$PY" ] || { printf '{"permission":"allow"}\n'; exit 0; }; exec "$PY" "$ROOT/scripts/cursor_capture.py" "$@"
@echo off
rem Batch above, POSIX shell on the polyglot first line.
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
echo {"permission":"allow"}
exit /b 0
