import calendar
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

GATEWAY = 'https://ai-gateway.vercel.sh/v1'


def gateway_key():
    key = os.environ.get('AI_GATEWAY_API_KEY', '').strip()
    if key:
        return key
    result = subprocess.run([str(Path.home() / '.npm-global/bin/pi'), 'auth', 'print-api-key',
                             '--provider', 'vercel-ai-gateway'], capture_output=True, text=True, timeout=60)
    return result.stdout.strip() if result.returncode == 0 else ''


def http_json(url, key):
    request = urllib.request.Request(url, headers={'Authorization': f'Bearer {key}', 'User-Agent': 'token-ladder'})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def cycle_window(credits, now):
    try:
        reset = datetime.fromisoformat(credits['token_pass']['resets_at'].replace('Z', '+00:00'))
        if reset.tzinfo is None:
            raise ValueError('reset requires a timezone')
        reset = reset.astimezone(timezone.utc)
        year, month = (reset.year, reset.month - 1) if reset.month > 1 else (reset.year - 1, 12)
        start = reset.replace(year=year, month=month, day=min(reset.day, calendar.monthrange(year, month)[1]))
        if not start <= now < reset:
            raise ValueError('reset is not in the current cycle')
        return start, reset
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise ValueError('current Token Pass cycle unavailable') from exc


def report_buckets(report, group):
    if not isinstance(report, dict) or not isinstance(report.get('results'), list):
        raise ValueError('missing report results')
    buckets = {}
    for row in report['results']:
        try:
            label = row[group]
            cost = Decimal(str(row['total_cost']))
            count = row['request_count']
        except (KeyError, TypeError, InvalidOperation) as exc:
            raise ValueError('invalid report row') from exc
        if not cost.is_finite() or cost < 0 or type(count) is not int or count < 0:
            raise ValueError('invalid report metrics')
        if label is not None and not isinstance(label, str):
            raise ValueError('invalid report group')
        previous_cost, previous_count = buckets.get(label, (Decimal(0), 0))
        buckets[label] = (previous_cost + cost, previous_count + count)
    return buckets


def parse_reports(model_report, tag_report):
    models = report_buckets(model_report, 'model')
    if any(not name for name in models):
        raise ValueError('missing model name')
    tags = report_buckets(tag_report, 'tag')
    callers = {tag: metrics for tag, metrics in tags.items()
               if tag and len(tag) <= 64 and re.fullmatch(r'caller=[A-Za-z0-9_-]+', tag)}
    cost = sum((metrics[0] for metrics in models.values()), Decimal(0))
    count = sum(metrics[1] for metrics in models.values())
    untagged_cost = cost - sum((metrics[0] for metrics in callers.values()), Decimal(0))
    untagged_count = count - sum(metrics[1] for metrics in callers.values())
    if untagged_cost < 0 or untagged_count < 0:
        raise ValueError('caller totals exceed model totals; overlapping tags or reporting delay')
    callers['untagged'] = (untagged_cost, untagged_count)
    return models, callers


def report_lines(get=http_json, key=None, now=None):
    lines = ['Vercel AI Gateway burn report']
    try:
        key = gateway_key() if key is None else key
        if not key:
            raise ValueError('no gateway credential')
        now = datetime.now(timezone.utc) if now is None else now
        start, reset = cycle_window(get(f'{GATEWAY}/credits', key), now)
        lines += [f'Token Pass cycle: {start.isoformat()} to {reset.isoformat()}',
                  f'Report UTC dates: {start.date()} through {now.date()} inclusive; team-wide charged USD.',
                  'Date-only API includes spend before the reset hour on the first day.',
                  'Untagged means no caller= tag. PR, brief and ticket tags are not added to caller totals.',
                  'Reports can lag a few minutes; each request must have at most one caller= tag.']
        reports = []
        for group in ('model', 'tag'):
            query = urllib.parse.urlencode({'start_date': start.date().isoformat(),
                                            'end_date': now.date().isoformat(), 'group_by': group})
            reports.append(get(f'{GATEWAY}/report?{query}', key))
        models, callers = parse_reports(*reports)
        lines.append('Spend per model:')
        for model, (cost, count) in sorted(models.items()):
            lines.append(f'  {model}: ${cost:.6f} ({count} requests)')
        total = sum((metrics[0] for metrics in models.values()), Decimal(0))
        lines += [f'Total: ${total:.6f}', 'Spend per caller tag:']
        for caller, (cost, count) in sorted(callers.items()):
            lines.append(f'  {caller}: ${cost:.6f} ({count} requests)')
    except urllib.error.HTTPError as exc:
        lines.append(f'Vercel report unavailable: HTTP {exc.code}. Set AI_GATEWAY_API_KEY to a regular gateway key; '
                     'Token Pass-scoped keys cannot read /v1/report. Reporting requires Pro or Enterprise.')
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        lines.append(f'Vercel report unavailable: {type(exc).__name__}. Check the gateway credential, '
                     'current cycle and report response; no spend total is available.')
    return lines
