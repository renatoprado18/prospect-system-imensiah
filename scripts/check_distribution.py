#!/usr/bin/env python3
"""Check current circle distribution."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

from database import get_db

with get_db() as conn:
    cursor = conn.cursor()

    # Distribution by circle
    cursor.execute("""
        SELECT COALESCE(circulo, 5) as circulo, COUNT(*) as total
        FROM contacts
        GROUP BY COALESCE(circulo, 5)
        ORDER BY circulo
    """)

    print("Current distribution:")
    for row in cursor.fetchall():
        print(f"  Circle {row['circulo']}: {row['total']}")

    # Check tag counts
    cursor.execute("""
        SELECT tags FROM contacts WHERE tags IS NOT NULL AND tags != '[]'
    """)

    import json
    tag_counts = {}
    for row in cursor.fetchall():
        try:
            tags = json.loads(row["tags"] or "[]")
            for tag in tags:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
        except:
            pass

    print("\nTop tags:")
    for tag, count in sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)[:10]:
        print(f"  {tag}: {count}")
