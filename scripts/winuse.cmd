@echo off
rem WinUse launcher (Windows).
rem KEEP THIS FILE PURE ASCII: cmd.exe reads .cmd in the system ANSI codepage
rem (GBK here), so UTF-8 non-ASCII comments get mangled and then executed as
rem commands. Same class of trap as CRLF killing Linux shebangs.
rem
rem Usage: scripts\winuse.cmd doctor
setlocal
set "WINUSE_ROOT=%~dp0.."
set "PYTHONPATH=%WINUSE_ROOT%;%PYTHONPATH%"
python -m winuse %*
exit /b %ERRORLEVEL%
