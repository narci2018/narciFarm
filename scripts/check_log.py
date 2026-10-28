import os, glob, re

log_dir = r"C:\Tools2\narciFarm\logs"
files = sorted(glob.glob(os.path.join(log_dir, "*.log")), key=os.path.getmtime, reverse=True)
if not files:
    print("no log file")
    raise SystemExit(0)

name = files[0]
with open(name, encoding="utf-8", errors="replace") as f:
    text = f.read()

print("logfile=" + name)

bug = re.search(r"Event\.wait|event\.wait|timeout.*15|TypeError.*wait|投屏连接失败|fallback.*Event", text)
if bug:
    print("FOUND_BUG_LOG")
    for line in text.splitlines():
        if re.search(r"Event\.wait|timeout|TypeError|投屏连接失败|fallback|Error", line):
            print(line)
    raise SystemExit(0)

print("no_bug_log_found")
for line in text.splitlines():
    if re.search(r"已启动|HYUONZDMW8CQ9TON|scid|投屏会话已建立|关闭|优雅", line):
        print(line)
