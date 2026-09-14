@echo off
REM ---------------------------------------------------------------------
REM Publish the Supabase mirror - the whole release, in one double-click.
REM ---------------------------------------------------------------------
REM Three steps that must happen in order, each stopping the run if it
REM fails. They are separated into their own scripts because they are
REM separately re-runnable; this file exists so nobody has to remember
REM the order or retype three paths.
REM
REM A .bat rather than a one-liner on purpose: this machine's shell is
REM Windows PowerShell 5.1, where && is a parser error, so a chained
REM command pasted into that window does not run at all. cmd has no such
REM problem, and a batch file can simply be double-clicked.
REM
REM Nothing here handles the secret key in the open: link_publish_key
REM moves it between two gitignored files and prints only its length.

cd /d "%~dp0.."

echo.
echo === 1/3  linking the publish key ===
python tools\link_publish_key.py
if errorlevel 1 goto failed

echo.
echo === 2/3  checking the bucket ===
python tools\setup_mirror_bucket.py
if errorlevel 1 goto failed

echo.
echo === 3/3  publishing ===
python tools\publish_supabase.py
if errorlevel 1 goto failed

echo.
echo ======================================================
echo  Done. Paste the output above back into Claude and it
echo  will verify the mirror from outside.
echo ======================================================
goto end

:failed
echo.
echo ------------------------------------------------------
echo  STOPPED - the step above failed, so nothing further
echo  ran. The mirror is unchanged: until the manifest is
echo  uploaded (last), clients keep seeing the previous
echo  version rather than a half-published one.
echo ------------------------------------------------------

:end
echo.
pause
