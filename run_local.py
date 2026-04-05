#!/usr/bin/env python3
"""
Script para rodar o INTEL localmente para desenvolvimento.

Uso:
    python run_local.py

Requisitos:
    1. Arquivo .env configurado (já existe)
    2. Adicionar URIs de redirecionamento no Google Cloud Console:
       - http://localhost:8000/auth/google/callback
       - http://localhost:8000/api/google/callback
       - http://localhost:8000/api/gmail/callback
"""
import os
import sys

# Add app directory to path
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(ROOT_DIR, 'app')
sys.path.insert(0, ROOT_DIR)
sys.path.insert(0, APP_DIR)

# Load environment from .env
from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT_DIR, '.env'))

# FORCE localhost for local development (override production BASE_URL)
os.environ['BASE_URL'] = 'http://localhost:8000'

def main():
    import uvicorn

    # Check for required env vars
    required_vars = ['GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET', 'DATABASE_URL']
    missing = [v for v in required_vars if not os.getenv(v)]

    if missing:
        print("AVISO: Variáveis de ambiente faltando:")
        for v in missing:
            print(f"  - {v}")
        print("\nConfigure no arquivo .env antes de usar todas as funcionalidades.")
        print()

    base_url = os.getenv('BASE_URL', 'http://localhost:8000')
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║                    INTEL - Desenvolvimento Local              ║
╠══════════════════════════════════════════════════════════════╣
║  URL: {base_url:<52} ║
║                                                              ║
║  Para Google OAuth funcionar localmente, adicione no         ║
║  Google Cloud Console > APIs & Services > Credentials:       ║
║                                                              ║
║  URIs de redirecionamento autorizados:                       ║
║  • http://localhost:8000/auth/google/callback                ║
║  • http://localhost:8000/api/google/callback                 ║
║  • http://localhost:8000/api/gmail/callback                  ║
╚══════════════════════════════════════════════════════════════╝
    """)

    # Run server (reload disabled for stability)
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=False
    )

if __name__ == "__main__":
    main()
