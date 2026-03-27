"""
Vercel Serverless Function Entry Point for FastAPI
"""
import sys
import os
import secrets

# Setup paths
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(ROOT_DIR, 'app')

sys.path.insert(0, ROOT_DIR)
sys.path.insert(0, APP_DIR)

# Set environment
os.environ['DB_PATH'] = '/tmp/prospects.db'
os.environ.setdefault('SECRET_KEY', os.getenv('SECRET_KEY', secrets.token_hex(32)))
os.environ.setdefault('BASE_URL', 'https://intel.almeida-prado.com')

# Define app at module level first (Vercel requires top-level app)
app = None

# Now try to import the real app
try:
    from app.main import app as main_app
    app = main_app
except Exception as import_error:
    # Fallback minimal app for debugging
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    app = FastAPI()

    @app.get("/")
    def root():
        return JSONResponse({
            "error": str(import_error),
            "root_dir": ROOT_DIR,
            "app_dir": APP_DIR,
            "sys_path": sys.path[:5]
        })

    @app.get("/health")
    def health():
        return {"status": "fallback", "error": str(import_error)}

# Ensure app is always defined
if app is None:
    from fastapi import FastAPI
    app = FastAPI()

    @app.get("/")
    def error_root():
        return {"error": "App failed to initialize"}
