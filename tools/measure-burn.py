import collections
import datetime as dt
import glob
import json
import os
from pathlib import Path
import sched
import subprocess
import threading
import time

now = dt.datetime.now(dt.timezone.utc)
cutoff = now - dt.timedelta(days=7)
counts = collections.defaultdict(collections.Counter)
for offset in range(8):
    counts[(cutoff + dt.timedelta(days=offset)).date().isoformat()]
files = 0
bad_lines = 0
for path in glob.glob(os.path.expanduser("~/.claude/projects/-home-valentin-projects-hypertask/*.jsonl")):
    if os.path.getmtime(path) < cutoff.timestamp():
        continue
    files += 1
    with open(path) as source:
        for line in source:
            try:
                event = json.loads(line)
            except ValueError:
                bad_lines += 1
                continue
            timestamp = event.get("timestamp")
            if not isinstance(timestamp, str):
                continue
            try:
                stamp = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError:
                continue
            if stamp.tzinfo is None or not cutoff <= stamp <= now:
                continue
            content = (event.get("message") or {}).get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                if block.get("name") == "Agent":
                    counts[stamp.date().isoformat()]["Agent"] += 1
                elif block.get("name") == "Bash" and (block.get("input") or {}).get("command", "").startswith("hax "):
                    counts[stamp.date().isoformat()]["hax"] += 1


def classify(comm, args):
    text = (comm + " " + args).lower()
    if any(word in text for word in ("vitest", "jest", "playwright", "node --test", "test-runner", "test_runner")):
        return "node test runners"
    if "next" in text and any(word in text for word in ("build", "worker", "next-server")):
        return "next build/server"
    if any(word in text for word in ("docker", "containerd", "buildkit")):
        return "docker"
    if "codex" in text or comm == "hax":
        return "codex/hax"
    if "claude" in text:
        return "claude"
    return "other"


samples = []


def sample():
    taken = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    raw = subprocess.check_output("ps -eo pcpu,rss,comm,args --sort=-pcpu | head -40", shell=True, text=True)
    categories = collections.defaultdict(lambda: [0, 0.0, 0])
    top = []
    for row in raw.splitlines()[1:]:
        fields = row.split(None, 3)
        if len(fields) < 4:
            continue
        cpu, rss, comm, args = fields
        category = classify(comm, args)
        values = categories[category]
        values[0] += 1
        values[1] += float(cpu)
        values[2] += int(rss)
        if len(top) < 5:
            top.append((category, float(cpu)))
    samples.append((taken, categories, top))


scheduler = sched.scheduler(time.monotonic, threading.Event().wait)
start = time.monotonic()
for offset in (0, 20, 40):
    scheduler.enterabs(start + offset, 1, sample)
scheduler.run()

lines = ["# YPER4-509 burn report", "",
         f"Measured {now.isoformat(timespec='seconds')}; rolling seven-day window, UTC days (first/last partial).",
         f"Source: {files} top-level Hypertask JSONL files modified within seven days; {bad_lines} malformed lines skipped.",
         "Counts are raw tool_use blocks with event timestamps in the window; hax counts only Bash commands starting exactly `hax `.",
         "", "| UTC date | Agent | hax |", "| --- | ---: | ---: |"]
for day, values in sorted(counts.items()):
    lines.append(f"| {day} | {values['Agent']} | {values['hax']} |")
lines.extend([f"| Total | {sum(v['Agent'] for v in counts.values())} | {sum(v['hax'] for v in counts.values())} |", "",
              "CPU: three `ps -eo pcpu,rss,comm,args --sort=-pcpu | head -40` samples, scheduled 20 seconds apart.",
              "CPU percentages are ps lifetime averages, summed across the top 39 processes (100% = one core); RSS is summed MiB, not unique memory.",
              "Commands/arguments are inspected only in memory and never written or printed. Next build/server combines Next workers/servers whose build phase is not reliably visible.", "",
              "| Sample UTC | Category | Processes | CPU % | RSS MiB |", "| --- | --- | ---: | ---: | ---: |"])
for taken, categories, top in samples:
    for category in ("node test runners", "next build/server", "docker", "claude", "codex/hax", "other"):
        n, cpu, rss = categories[category]
        lines.append(f"| {taken} | {category} | {n} | {cpu:.1f} | {rss / 1024:.1f} |")
lines.extend(["", "Largest CPU consumers are frequently unclassified; these samples cannot establish their workload.", ""])
for taken, categories, top in samples:
    lines.append(f"Top five at {taken}: " + "; ".join(f"{category} {cpu:.1f}%" for category, cpu in top) + ".")
Path(__file__).with_name("burn-report.md").write_text("\n".join(lines) + "\n")
print("Burn report written: tools/burn-report.md")
