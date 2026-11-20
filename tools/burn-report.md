# YPER4-509 burn report

Measured 2026-10-19T22:47:27+00:00; rolling seven-day window, UTC days (first/last partial).
Source: 204 top-level Hypertask JSONL files modified within seven days; 2 malformed lines skipped.
Counts are raw tool_use blocks with event timestamps in the window; hax counts only Bash commands starting exactly `hax `.

| UTC date | Agent | hax |
| --- | ---: | ---: |
| 2026-10-12 | 0 | 0 |
| 2026-10-13 | 27 | 2 |
| 2026-10-14 | 3 | 0 |
| 2026-10-15 | 0 | 0 |
| 2026-10-16 | 1 | 0 |
| 2026-10-17 | 0 | 0 |
| 2026-10-18 | 0 | 3 |
| 2026-10-19 | 0 | 1 |
| Total | 31 | 6 |

CPU: three `ps -eo pcpu,rss,comm,args --sort=-pcpu | head -40` samples, scheduled 20 seconds apart.
CPU percentages are ps lifetime averages, summed across the top 39 processes (100% = one core); RSS is summed MiB, not unique memory.
Commands/arguments are inspected only in memory and never written or printed. Next build/server combines Next workers/servers whose build phase is not reliably visible.

| Sample UTC | Category | Processes | CPU % | RSS MiB |
| --- | --- | ---: | ---: | ---: |
| 2026-10-19T22:47:51+00:00 | node test runners | 5 | 125.8 | 1799.7 |
| 2026-10-19T22:47:51+00:00 | next build/server | 0 | 0.0 | 0.0 |
| 2026-10-19T22:47:51+00:00 | docker | 0 | 0.0 | 0.0 |
| 2026-10-19T22:47:51+00:00 | claude | 18 | 147.0 | 8070.8 |
| 2026-10-19T22:47:51+00:00 | codex/hax | 0 | 0.0 | 0.0 |
| 2026-10-19T22:47:51+00:00 | other | 16 | 2105.3 | 1896.1 |
| 2026-10-19T22:48:11+00:00 | node test runners | 4 | 222.0 | 2069.2 |
| 2026-10-19T22:48:11+00:00 | next build/server | 0 | 0.0 | 0.0 |
| 2026-10-19T22:48:11+00:00 | docker | 0 | 0.0 | 0.0 |
| 2026-10-19T22:48:11+00:00 | claude | 15 | 132.0 | 6935.4 |
| 2026-10-19T22:48:11+00:00 | codex/hax | 0 | 0.0 | 0.0 |
| 2026-10-19T22:48:11+00:00 | other | 20 | 1633.4 | 1972.3 |
| 2026-10-19T22:48:31+00:00 | node test runners | 5 | 173.2 | 2288.4 |
| 2026-10-19T22:48:31+00:00 | next build/server | 0 | 0.0 | 0.0 |
| 2026-10-19T22:48:31+00:00 | docker | 0 | 0.0 | 0.0 |
| 2026-10-19T22:48:31+00:00 | claude | 13 | 128.0 | 5939.5 |
| 2026-10-19T22:48:31+00:00 | codex/hax | 1 | 9.8 | 14.8 |
| 2026-10-19T22:48:31+00:00 | other | 20 | 1686.4 | 2173.3 |

Largest CPU consumers are frequently unclassified; these samples cannot establish their workload.

Top five at 2026-10-19T22:47:51+00:00: other 1300.0%; other 255.0%; other 150.0%; other 102.0%; node test runners 102.0%.
Top five at 2026-10-19T22:48:11+00:00: other 700.0%; other 245.0%; other 150.0%; node test runners 106.0%; other 103.0%.
Top five at 2026-10-19T22:48:31+00:00: other 900.0%; other 234.0%; other 105.0%; node test runners 96.5%; other 71.7%.
