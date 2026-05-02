#!/usr/bin/env python3
"""
One-shot sweep: detect contacts whose stored linkedin URL points to a profile
of someone with a clearly different name.

Reads LINKDAPI_KEY from env (load via `set -a; source .env; set +a` first).
Writes report CSV to /Users/rap/prospect-system/reports/linkedin_name_mismatches.csv.
Does NOT modify any contact data.
"""
import os
import sys
import csv
import time
import httpx
import psycopg2
import psycopg2.extras

# Make app/ importable so we can reuse the runtime name-match logic
sys.path.insert(0, '/Users/rap/prospect-system/app')
from services.campaign_executor import CampaignExecutor  # noqa: E402

REPORT_DIR = '/Users/rap/prospect-system/reports'
REPORT_PATH = os.path.join(REPORT_DIR, 'linkedin_name_mismatches.csv')
SLEEP_BETWEEN_CALLS = 0.35  # ~3 calls/sec


def extract_username(linkedin_url: str) -> str:
    """Extract username from LinkedIn URL, mirroring campaign_executor logic."""
    return linkedin_url.rstrip('/').split('/in/')[-1].split('/')[0].split('?')[0]


def main() -> int:
    api_key = (os.getenv('LINKDAPI_KEY') or '').strip()
    if not api_key:
        print('ERROR: LINKDAPI_KEY not set. Run: set -a; source .env; set +a', file=sys.stderr)
        return 2

    os.makedirs(REPORT_DIR, exist_ok=True)

    executor = CampaignExecutor()

    # Force local connection — explicitly override user too, since sourcing .env
    # exposes PGUSER=neondb_owner which doesn't exist locally.
    conn = psycopg2.connect(host='localhost', port=5432, dbname='intel', user=os.getenv('USER') or 'rap')
    conn.set_session(readonly=True, autocommit=True)
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute(
        """
        SELECT id, nome, linkedin
        FROM contacts
        WHERE linkedin IS NOT NULL AND linkedin != ''
          AND linkedin ILIKE '%linkedin.com/in/%'
        ORDER BY id
        """
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    total = len(rows)
    print(f'Sweeping {total} contacts...', file=sys.stderr)

    counts = {'match': 0, 'mismatch': 0, 'error': 0, 'no_name': 0, 'bad_url': 0}

    out_f = open(REPORT_PATH, 'w', newline='', encoding='utf-8')
    writer = csv.writer(out_f)
    writer.writerow(['id', 'nome', 'linkedin', 'actual_name', 'status'])

    client = httpx.Client(timeout=15.0)
    try:
        for idx, row in enumerate(rows, start=1):
            cid = row['id']
            nome = row['nome'] or ''
            linkedin = row['linkedin'] or ''

            username = extract_username(linkedin)
            if not username:
                counts['bad_url'] += 1
                writer.writerow([cid, nome, linkedin, '', 'bad_url'])
                continue

            try:
                resp = client.get(
                    'https://linkdapi.com/api/v1/profile/full',
                    headers={'X-linkdapi-apikey': api_key},
                    params={'username': username},
                )
            except Exception as e:
                counts['error'] += 1
                writer.writerow([cid, nome, linkedin, '', f'error:{type(e).__name__}:{str(e)[:80]}'])
                time.sleep(SLEEP_BETWEEN_CALLS)
                continue

            if resp.status_code != 200:
                counts['error'] += 1
                writer.writerow([cid, nome, linkedin, '', f'error:http_{resp.status_code}'])
                time.sleep(SLEEP_BETWEEN_CALLS)
                continue

            try:
                data = resp.json()
            except Exception as e:
                counts['error'] += 1
                writer.writerow([cid, nome, linkedin, '', f'error:json:{type(e).__name__}'])
                time.sleep(SLEEP_BETWEEN_CALLS)
                continue

            d = (data.get('data') or {}) if isinstance(data, dict) else {}
            first_name = d.get('firstName') or ''
            last_name = d.get('lastName') or ''
            actual_name = f'{first_name} {last_name}'.strip()

            if not actual_name:
                counts['no_name'] += 1
                writer.writerow([cid, nome, linkedin, '', 'no_name'])
                time.sleep(SLEEP_BETWEEN_CALLS)
                continue

            if executor._names_match(nome, actual_name):
                counts['match'] += 1
            else:
                counts['mismatch'] += 1
                writer.writerow([cid, nome, linkedin, actual_name, 'mismatch'])

            if idx % 50 == 0:
                print(
                    f'[{idx}/{total}] match={counts["match"]} '
                    f'mismatch={counts["mismatch"]} error={counts["error"]} '
                    f'no_name={counts["no_name"]} bad_url={counts["bad_url"]}',
                    file=sys.stderr,
                )

            time.sleep(SLEEP_BETWEEN_CALLS)
    finally:
        client.close()
        out_f.close()

    print('--- DONE ---')
    print(f'Total checked: {total}')
    print(f'Matches:       {counts["match"]}')
    print(f'Mismatches:    {counts["mismatch"]}')
    print(f'Errors:        {counts["error"]}')
    print(f'No name:       {counts["no_name"]}')
    print(f'Bad URL:       {counts["bad_url"]}')
    print(f'Report:        {REPORT_PATH}')

    err_pct = (counts['error'] / total * 100) if total else 0
    if err_pct > 5:
        print(f'WARNING: error rate {err_pct:.1f}% > 5% — possible API throttling', file=sys.stderr)

    return 0


if __name__ == '__main__':
    sys.exit(main())
