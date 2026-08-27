:; allow() { printf '{"permission":"allow"}\n'; exit 0; }; SELF=$0; DIR=$(CDPATH= cd -- "$(dirname -- "$SELF")" && pwd) || allow; ROOT=$(CDPATH= cd -- "$DIR/.." && pwd); [ -f "$ROOT/scripts/cursor_capture.py" ] || allow; PY=$(command -v python3 || command -v python); [ -n "$PY" ] || allow; exec "$PY" "$ROOT/scripts/cursor_capture.py" "$@"
@echo off
rem Batch below; POSIX shells execute only the polyglot first line above.
rem Cursor resolves ./hooks from the plugin root; this shim then derives every
rem executable path from its own installed location. Hook JSON stays on stdin;
rem no named transcript or command payload file is created.
setlocal
for %%I in ("%~dp0..") do set "ROOT=%%~fI"
if not exist "%ROOT%\scripts\cursor_capture.py" goto allow
:launch
where py >nul 2>&1
if %errorlevel% equ 0 (
  py -3 "%ROOT%\scripts\cursor_capture.py" %*
  if errorlevel 1 goto allow
  exit /b 0
)
where python >nul 2>&1
if %errorlevel% equ 0 (
  python "%ROOT%\scripts\cursor_capture.py" %*
  if errorlevel 1 goto allow
  exit /b 0
)
:allow
echo {"permission":"allow"}
exit /b 0
