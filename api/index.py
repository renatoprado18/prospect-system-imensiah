"""
Vercel Serverless Function Entry Point
"""
import sys
import os

# Add app directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

from main import app

# Vercel handler
handler = app
