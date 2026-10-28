$py = "py"
$files = @(
  "main.py",
  "narci/main.py",
  "narci/scrcpy_client.py",
  "narci/scrcpy_protocol.py",
  "narci/adb.py",
  "narci/scheduler.py",
  "narci/config.py",
  "narci/db.py",
  "narci/scenario.py",
  "narci/events.py"
)
$fail = $false
foreach ($f in $files) {
  $r = & $py -c ("import py_compile; py_compile.compile('$f', doraise=True)")
  if ($LASTEXITCODE -ne 0) {
    Write-Output ("FAIL: " + $f)
    $fail = $true
  }
}
if (-not $fail) { Write-Output "ALL_PY_COMPILE_OK" }
exit [int]$fail
