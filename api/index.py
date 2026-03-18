"""
Vercel Serverless Function Entry Point for FastAPI
"""
import sys
import os

# Add directories to path
root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
app_dir = os.path.join(root_dir, 'app')
sys.path.insert(0, root_dir)
sys.path.insert(0, app_dir)

# Change working directory
os.chdir(app_dir)

# Import FastAPI app
from app.main import app

# Vercel uses 'app' as handler for ASGI
