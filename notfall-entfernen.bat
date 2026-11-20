@echo off
REM ============================================================
REM  KlarBlock - Notfall-Entfernung
REM  Entfernt KlarBlock vollstaendig, OHNE 24-Stunden-Wartezeit.
REM  Gedacht zum Testen und fuer echte Notfaelle (z. B. defekte Installation).
REM  Rechtsklick > "Als Administrator ausfuehren".
REM ============================================================
net session >nul 2>&1
if errorlevel 1 (
  echo Bitte mit Rechtsklick ^> "Als Administrator ausfuehren" starten.
  pause
  exit /b 1
)

echo [1/6] Geplante Aufgaben entfernen ...
schtasks /delete /f /tn "KlarBlock" >nul 2>&1
schtasks /delete /f /tn "KlarBlock-Waechter" >nul 2>&1

echo [2/6] Hintergrundprozess beenden ...
taskkill /f /im KlarBlock.exe >nul 2>&1
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*klarblock.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
timeout /t 2 /nobreak >nul

echo [3/6] KlarBlock-Eintraege aus der Hosts-Datei entfernen ...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$h = Join-Path $env:SystemRoot 'System32\drivers\etc\hosts';" ^
  "attrib -r $h;" ^
  "$t = [IO.File]::ReadAllText($h);" ^
  "$n = [regex]::Replace($t, '(?s)# >>> KlarBlock BEGIN.*?# <<< KlarBlock END <<<\r?\n?', '');" ^
  "for ($i=0; $i -lt 10; $i++) { try { [IO.File]::WriteAllText($h, $n, (New-Object Text.UTF8Encoding $false)); Write-Host '    Hosts-Datei bereinigt.'; break } catch { Start-Sleep -Seconds 1 } }"

echo [4/6] Browser-Richtlinien zuruecksetzen ...
reg delete "HKLM\SOFTWARE\Policies\Google\Chrome" /v DnsOverHttpsMode /f >nul 2>&1
reg delete "HKLM\SOFTWARE\Policies\Microsoft\Edge" /v DnsOverHttpsMode /f >nul 2>&1
reg delete "HKLM\SOFTWARE\Policies\BraveSoftware\Brave" /v DnsOverHttpsMode /f >nul 2>&1
reg delete "HKLM\SOFTWARE\Policies\BraveSoftware\Brave" /v TorDisabled /f >nul 2>&1
reg delete "HKLM\SOFTWARE\Policies\Mozilla\Firefox\DNSOverHTTPS" /v Enabled /f >nul 2>&1
reg delete "HKLM\SOFTWARE\Policies\Mozilla\Firefox\DNSOverHTTPS" /v Locked /f >nul 2>&1

echo [5/6] Programmdateien und Einstellungen loeschen ...
rmdir /s /q "%ProgramFiles%\KlarBlock" >nul 2>&1
rmdir /s /q "%ProgramData%\KlarBlock" >nul 2>&1

echo [6/6] DNS-Cache leeren ...
ipconfig /flushdns >nul

echo.
echo KlarBlock wurde entfernt. Browser einmal komplett schliessen und neu oeffnen.
pause
