$exe = "C:\Tools2\narciFarm\dist\narciFarm.exe"
if (-not (Test-Path $exe)) { Write-Output "exe not found: $exe"; exit 1 }
$start = Start-Process -FilePath $exe -PassThru -WindowStyle Hidden
Start-Sleep -Milliseconds 800
if ($start.HasExited) {
    Write-Output ("EXE_EXITED rc="+ $start.ExitCode)
    exit 2
}
Write-Output ("STARTED pid="+ $start.Id + " path="+ $start.Path)
