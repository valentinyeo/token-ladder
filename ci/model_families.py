import json
import re
import sys
from pathlib import Path

FAMILY_PATTERNS = {
    'deepseek': r'deepseek(?:[-.].*)?',
    'openai': r'(?:openai|gpt(?:[-.].*)?|chatgpt(?:[-.].*)?|codex(?:[-.].*)?|o[134](?:[-.].*)?)',
    'moonshotai': r'(?:moonshotai|moonshot(?:[-.].*)?|kimi(?:[-.].*)?)',
    'zai': r'(?:zai|z-ai|z\.ai|glm(?:[-.].*)?)',
    'google': r'(?:google|gemini(?:[-.].*)?)',
    'anthropic': r'(?:anthropic|claude(?:[-.].*)?)',
    'cursor': r'(?:cursor|composer(?:[-.].*)?)',
}


def model_family(model):
    for part in reversed(model.strip().lower().split('/')):
        for family, pattern in FAMILY_PATTERNS.items():
            if re.fullmatch(pattern, part):
                return family
    raise ValueError('unknown model family')


def author_families(body):
    families = set()
    for line in (body or '').splitlines():
        match = re.match(r'^\s*Author model:[ \t]*(.*)$', line, re.IGNORECASE)
        if not match:
            continue
        model = match.group(1).strip()
        if not re.fullmatch(r'[^\s/]+(?:/[^\s/]+)+', model):
            raise ValueError('Author model: must be <family>/<model>')
        families.add(model_family(model))
    return families


def filter_lanes(lanes, excluded_families):
    if not excluded_families:
        return lanes
    return [lane for lane in lanes if model_family(lane['model']) not in excluded_families]


def main():
    event = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    try:
        families = author_families(event['pull_request'].get('body'))
    except ValueError as exc:
        print(f'::error::verifier: {exc}', file=sys.stderr)
        return 1
    if not families:
        print('::warning::verifier: no Author model: line in PR body', file=sys.stderr)
        print('::error::verifier: add Author model: <family>/<model> before verification', file=sys.stderr)
        return 1
    print(','.join(sorted(families)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
