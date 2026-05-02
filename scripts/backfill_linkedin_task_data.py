#!/usr/bin/env python3
"""
One-shot backfill: popula linkedin_task_data (sidecar) pra tasks pendentes
de Curtir/Comentar LinkedIn que ainda nao tem row sidecar.

Uso:
    set -a; source .env; set +a
    python3 scripts/backfill_linkedin_task_data.py             # dry-run
    python3 scripts/backfill_linkedin_task_data.py --apply     # aplica

Conecta no Neon via POSTGRES_URL (ou intel local se passar --local).
Reusa _fetch_recent_post + _save_linkedin_task_data do CampaignExecutor pra
manter logica identica ao runtime (incluindo name guard).
"""
import os
import sys
import time

import psycopg2
import psycopg2.extras

sys.path.insert(0, '/Users/rap/prospect-system/app')
from services.campaign_executor import CampaignExecutor  # noqa: E402

SLEEP_BETWEEN_CALLS = 0.35


def main() -> int:
    args = set(sys.argv[1:])
    apply_changes = '--apply' in args
    use_local = '--local' in args

    api_key = (os.getenv('LINKDAPI_KEY') or '').strip()
    if not api_key:
        print('ERROR: LINKDAPI_KEY not set. Run: set -a; source .env; set +a', file=sys.stderr)
        return 2

    if use_local:
        conn = psycopg2.connect(host='localhost', port=5432, dbname='intel',
                                user=os.getenv('USER') or 'rap')
        target = 'local'
    else:
        url = os.getenv('POSTGRES_URL')
        if not url:
            print('ERROR: POSTGRES_URL not set', file=sys.stderr)
            return 2
        conn = psycopg2.connect(url)
        target = 'neon (prod)'

    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute(
        """
        SELECT t.id AS task_id, t.titulo, c.nome AS contact_nome, c.linkedin
        FROM tasks t
        LEFT JOIN contacts c ON c.id = t.contact_id
        WHERE (t.titulo ILIKE 'LinkedIn: Curtir post%%'
               OR t.titulo ILIKE 'LinkedIn: Comentar post%%')
          AND t.status = 'pending'
          AND NOT EXISTS (
              SELECT 1 FROM linkedin_task_data ltd WHERE ltd.task_id = t.id
          )
        ORDER BY t.id
        """
    )
    rows = [dict(r) for r in cur.fetchall()]
    print(f'Backfilling {len(rows)} tasks on {target} '
          f'(apply={apply_changes})', file=sys.stderr)

    executor = CampaignExecutor()
    counts = {'checked': 0, 'filled': 0, 'skipped_no_url': 0,
              'skipped_no_post': 0, 'errors': 0}

    for r in rows:
        counts['checked'] += 1
        if not r['linkedin']:
            counts['skipped_no_url'] += 1
            continue
        try:
            post = executor._fetch_recent_post(
                r['linkedin'], expected_name=r['contact_nome'])
        except Exception as e:
            counts['errors'] += 1
            print(f"  [err] task {r['task_id']} ({r['contact_nome']}): {e}",
                  file=sys.stderr)
            continue

        if not post or not post.get('url'):
            counts['skipped_no_post'] += 1
            print(f"  [skip] task {r['task_id']} ({r['contact_nome']}): no post",
                  file=sys.stderr)
            time.sleep(SLEEP_BETWEEN_CALLS)
            continue

        if apply_changes:
            executor._save_linkedin_task_data(cur, r['task_id'], post)
            conn.commit()
        counts['filled'] += 1
        text_preview = (post.get('text') or '')[:60].replace('\n', ' ')
        print(f"  [ok]  task {r['task_id']} ({r['contact_nome']}): {text_preview}",
              file=sys.stderr)
        time.sleep(SLEEP_BETWEEN_CALLS)

    cur.close()
    conn.close()

    print('--- DONE ---')
    print(f'Target:        {target}')
    print(f'Apply:         {apply_changes}')
    print(f'Checked:       {counts["checked"]}')
    print(f'Filled:        {counts["filled"]}')
    print(f'Skipped (URL): {counts["skipped_no_url"]}')
    print(f'Skipped (post):{counts["skipped_no_post"]}')
    print(f'Errors:        {counts["errors"]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
