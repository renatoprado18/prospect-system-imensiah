"""
Vercel Serverless Function Entry Point for FastAPI
"""
import sys
import os

# Setup paths
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(ROOT_DIR, 'app')

sys.path.insert(0, ROOT_DIR)
sys.path.insert(0, APP_DIR)

# Set environment
os.environ['DB_PATH'] = '/tmp/prospects.db'

# Now import the app
try:
    from app.main import app
except Exception as e:
    # Fallback minimal app for debugging
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    app = FastAPI()

    @app.get("/")
    def root():
        return JSONResponse({
            "error": str(e),
            "root_dir": ROOT_DIR,
            "app_dir": APP_DIR,
            "sys_path": sys.path[:5]
        })

    @app.get("/health")
    def health():
        return {"status": "fallback", "error": str(e)}
