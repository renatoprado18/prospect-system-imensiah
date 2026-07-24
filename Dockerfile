# intel-api build image.
# Motivo: substituir o Railpack/mise (que baixa o CPython de
# github.com/astral-sh/python-build-standalone e travou os builds em 24/07 com
# 504 Gateway Timeout). python:3.12-slim traz o Python embutido do Docker Hub,
# eliminando essa dependencia de rede no build.
FROM python:3.12-slim

# - PYTHONUNBUFFERED: logs saem na hora (Railway captura stdout).
# - PYTHONDONTWRITEBYTECODE: nao gera .pyc no container.
# - PIP_NO_CACHE_DIR / PIP_DISABLE_PIP_VERSION_CHECK: imagem menor, build mais limpo.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Instala deps primeiro (camada cacheada enquanto requirements.txt nao muda).
# Todas as deps nativas (psycopg2-binary, cryptography<45, Pillow, pillow-heif,
# pydantic-core, lxml via python-docx) tem wheels manylinux2014 x86_64 cp312,
# entao NAO precisa de build-essential / libpq / libjpeg. Se um dia algum pin
# perder o wheel, adicionar aqui um bloco apt-get com as libs de sistema.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copia o restante do repo. O app importa modulos por nome (from services...,
# from database...), entao roda com cwd=app/. Alem disso app/main.py referencia
# ../data e ../scripts em runtime, por isso o repo inteiro precisa estar presente.
COPY . .

# Reproduz o start command do Railpack: `cd app && uvicorn main:app ...`.
WORKDIR /app/app

# Forma shell para expandir $PORT injetado pelo Railway (fallback 8000 local).
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
