"""
Backfill linkedin_activity_urn pra posts publicados antigos.
Chama collect_metrics_for_post(id) que ja persiste URN automaticamente
via _persist_activity_urn().
"""
import asyncio
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

from dotenv import load_dotenv
load_dotenv()

from services.editorial_metrics_collector import collect_metrics_for_post
from database import get_db


async def main():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, article_title, data_publicado
            FROM editorial_posts
            WHERE status='published'
              AND data_publicado IS NOT NULL
              AND linkedin_activity_urn IS NULL
            ORDER BY data_publicado DESC
        """)
        posts = [dict(r) for r in cursor.fetchall()]

    print(f"Encontrados {len(posts)} posts pra backfill")
    print()

    results = {'ok': 0, 'fail_match': 0, 'fail_other': 0, 'errors': []}
    for i, p in enumerate(posts, 1):
        pid = p['id']
        title = (p.get('article_title') or '')[:50]
        try:
            r = await collect_metrics_for_post(pid)
            if r.get('success'):
                results['ok'] += 1
                print(f"  [{i}/{len(posts)}] post {pid} OK -- {title}")
            else:
                err = r.get('error', 'unknown')
                if 'match' in str(err).lower() or 'not found' in str(err).lower():
                    results['fail_match'] += 1
                else:
                    results['fail_other'] += 1
                    results['errors'].append((pid, err))
                print(f"  [{i}/{len(posts)}] post {pid} FAIL -- {err} -- {title}")
        except Exception as e:
            results['fail_other'] += 1
            results['errors'].append((pid, str(e)))
            print(f"  [{i}/{len(posts)}] post {pid} EXC -- {e} -- {title}")

    print()
    print("=== RESULTADO ===")
    print(f"OK:           {results['ok']}")
    print(f"Fail (match): {results['fail_match']} (post nao encontrado no LinkedIn -- provavelmente deletado/draft antigo)")
    print(f"Fail (outro): {results['fail_other']}")
    if results['errors']:
        print()
        print("Erros nao-match:")
        for pid, err in results['errors'][:10]:
            print(f"  post {pid}: {err[:120]}")

    # Confirmar URNs salvos
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COUNT(*) AS total,
                   COUNT(linkedin_activity_urn) AS com_urn
            FROM editorial_posts WHERE status='published'
        """)
        row = dict(cursor.fetchone())
        print()
        print(f"Posts publicados: {row['total']} total, {row['com_urn']} com URN ({100*row['com_urn']/max(row['total'],1):.0f}%)")


if __name__ == '__main__':
    asyncio.run(main())
