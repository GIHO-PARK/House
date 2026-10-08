# Creates the "RNZone AutoTrade" desktop shortcut. Called by install.bat.
param([string]$AppDir)
$AppDir = $AppDir.TrimEnd('\')
$desktop = [Environment]::GetFolderPath('Desktop')
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut((Join-Path $desktop 'RNZone AutoTrade.lnk'))
$lnk.TargetPath = Join-Path $AppDir '.venv\Scripts\pythonw.exe'
$lnk.Arguments = '"' + (Join-Path $AppDir 'RNZoneTrader.pyw') + '"'
$lnk.WorkingDirectory = $AppDir
$lnk.Description = 'RN-zone Kiwoom auto-trade'
$lnk.Save()
Write-Output ("Shortcut created: " + (Join-Path $desktop 'RNZone AutoTrade.lnk'))
