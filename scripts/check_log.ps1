param([string]$PatternFolder="C:\Tools2\narciFarm\logs")
$files = Get-ChildItem -Path $PatternFolder -Filter "*.log" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending
if (-not $files) { Write-Output "no log file"; exit 0 }
$name = $files[0].FullName
Write-Output "logfile=$name"
$text = Get-Content $name -Raw -ErrorAction SilentlyContinue
if ($text -match 'Event\.wait|event\.wait|timeout.*15|TypeError.*wait|投屏连接失败|fallback.*Event') {
    Write-Output "FOUND_BUG_LOG"
    $text -split "\r?\n" | Where-Object { $_ -match 'Event\.wait|timeout|TypeError|投屏连接失败|fallback|Error' } | Select-Object -First 15 | ForEach-Object { Write-Output $_ }
    exit 0
}
Write-Output "no_bug_log_found"
$text -split "\r?\n" | Where-Object { $_ -match '已启动|HYUONZDMW8CQ9TON|scid|投屏会话已建立|关闭|优雅' } | Select-Object -First 15 | ForEach-Object { Write-Output $_ }
