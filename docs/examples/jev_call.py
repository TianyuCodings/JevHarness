"""Inspect an actual archived Jev call; --live explicitly sends a new request."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def load_example():
    example = json.loads(Path(__file__).with_name('pokemon-turn12-jev.json').read_text())
    for field in ('request', 'response'):
        if digest(example[field]) != example['provenance'][f'{field}_json_sha256']:
            raise ValueError(f'Recorded {field} integrity check failed')
    return example


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true',
                        help='Send the recorded request to Jev through Vercel; requires AI_GATEWAY_API_KEY')
    args = parser.parse_args()
    example = load_example()
    if args.live:
        from dotenv import load_dotenv
        from auto_jev.providers import JevClient

        load_dotenv()
        response = JevClient(transport='vercel').judge(**example['request'])
        print('New Jev response; it may differ from the recorded answer.')
    else:
        response = example['response']
        print('Recorded Jev response; no API request was made.')
    print(json.dumps(response['answers']['action'], indent=2))


if __name__ == '__main__':
    main()
