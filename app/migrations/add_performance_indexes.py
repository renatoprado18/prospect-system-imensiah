"""
Migration: Add Performance Indexes for Editorial/Artigos pages
Run: python app/migrations/add_performance_indexes.py
"""
import os
import sys

# Add parent directory to path
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)
sys.path.insert(0, APP_DIR)

# Load environment
from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT_DIR, '.env'))

from database import get_db


def run_migration():
    """Add performance indexes to editorial_posts table"""
    print("Adding performance indexes...")

    indexes = [
        # Index for article_url filtering (Artigos page)
        """
        CREATE INDEX IF NOT EXISTS idx_editorial_posts_article_url
        ON editorial_posts(article_url) WHERE article_url IS NOT NULL
        """,

        # Index for ai_categoria filtering
        """
        CREATE INDEX IF NOT EXISTS idx_editorial_posts_ai_categoria
        ON editorial_posts(ai_categoria) WHERE ai_categoria IS NOT NULL
        """,

        # Index for ai_score ordering
        """
        CREATE INDEX IF NOT EXISTS idx_editorial_posts_ai_score
        ON editorial_posts(ai_score_relevancia DESC NULLS LAST)
        """,

        # Index for evergreen filtering
        """
        CREATE INDEX IF NOT EXISTS idx_editorial_posts_ai_evergreen
        ON editorial_posts(ai_evergreen) WHERE ai_evergreen = TRUE
        """,

        # Composite index for Artigos page main query
        """
        CREATE INDEX IF NOT EXISTS idx_editorial_posts_artigos
        ON editorial_posts(article_url, ai_score_relevancia DESC NULLS LAST, criado_em DESC)
        WHERE article_url IS NOT NULL
        """,

        # Index for scheduled posts (Editorial page)
        """
        CREATE INDEX IF NOT EXISTS idx_editorial_posts_scheduled
        ON editorial_posts(data_publicacao ASC)
        WHERE status = 'scheduled' AND data_publicacao IS NOT NULL
        """,

        # Index for hot_take_id lookups
        """
        CREATE INDEX IF NOT EXISTS idx_editorial_posts_hot_take
        ON editorial_posts(hot_take_id) WHERE hot_take_id IS NOT NULL
        """,
    ]

    with get_db() as conn:
        cursor = conn.cursor()

        for i, idx_sql in enumerate(indexes, 1):
            try:
                cursor.execute(idx_sql)
                print(f"  [{i}/{len(indexes)}] Index created successfully")
            except Exception as e:
                print(f"  [{i}/{len(indexes)}] Error: {e}")

        # Analyze table to update statistics
        print("\nAnalyzing table for query planner...")
        cursor.execute("ANALYZE editorial_posts")

        conn.commit()

    print("\nMigration completed!")


if __name__ == "__main__":
    run_migration()
