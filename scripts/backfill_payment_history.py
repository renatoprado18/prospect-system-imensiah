#!/usr/bin/env python3
"""
Backfill payment history pra planilha Google Sheets 'Historico'.

Roda Abril/2026 e Maio/2026 (ordem cronologica) com os dados ja conhecidos.

PRE-REQUISITO: User precisa ter reautenticado Google OAuth depois do deploy
do scope 'spreadsheets'. Se rodar antes, vai dar PermissionError (401/403).

Como rodar:
    cd /Users/rap/prospect-system
    python3 scripts/backfill_payment_history.py

Idempotencia: NAO e idempotente. Se rodar 2x, vai duplicar linhas.
Se precisar refazer, apague as linhas antigas na planilha antes.
"""
import asyncio
import os
import sys

# Adiciona app/ ao path pra imports funcionarem
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

# Carrega .env (precisa GOOGLE_CLIENT_ID/SECRET pra refresh do OAuth token)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except ImportError:
    pass

from services.payment_history import register_to_sheet  # noqa: E402


ABRIL_2026 = {
    "month": 4,
    "year": 2026,
    "expenses": [
        {
            "nome": "Cartao de Credito Nubank",
            "valor": 3452.33,
            "vencimento_dia": 6,
            "boleto": "26090347869773322916194800000005414080000345233",
        },
        {
            "nome": "Cartao de Credito Mercado Pago",
            "valor": 956.43,
            "vencimento_dia": 6,
            "boleto": "37690001040010531540201000387553214110000095643",
        },
        {
            "nome": "Psicologa",
            "valor": 0,
            "vencimento_dia": 6,
            "pix": "criscabud@uol.com.br",
        },
        {
            "nome": "Condominio Genebra 197",
            "valor": 654.13,
            "vencimento_dia": 10,
            "boleto": "23793.39308 90026.773672 72000.195403 5 14120000065413",
        },
        {
            "nome": "Aluguel Genebra 197",
            "valor": 2596.43,
            "vencimento_dia": 8,
            "boleto": "34191.09099 51759.230678 03922.160001 1 14100000259643",
        },
        {
            "nome": "ENEL Genebra 197",
            "valor": 82.06,
            "vencimento_dia": 26,
            "boleto": "836400000003820600481007941076847511002572587323",
        },
    ],
}

MAIO_2026 = {
    "month": 5,
    "year": 2026,
    "expenses": [
        {
            "nome": "Nubank",
            "valor": 2115.13,
            "vencimento_dia": 4,
            "pix": "26090369146975012736243700000003114360000211513",
        },
        {
            "nome": "Mercado Pago",
            "valor": 1380.55,
            "vencimento_dia": 4,
            "pix": "37690001040010531540201003913769714430000138055",
        },
        {
            "nome": "Aluguel",
            "valor": 2596.43,
            "vencimento_dia": 8,
            "pix": "34191.09008 03079.340679 03922.160001 1 14400000259643",
        },
        {
            "nome": "Condominio",
            "valor": 815.86,
            "vencimento_dia": 10,
            "pix": "23793.39308 90026.800558 64000.195400 4 14420000081586",
        },
        {
            "nome": "ENEL",
            "valor": 132.81,
            "vencimento_dia": 4,
            "pix": "836800000017328100481006521876242814002572587323",
        },
        {
            "nome": "Psicologa",
            "valor": 0,
            "vencimento_dia": 6,
            "nota": "Nao fiz sessoes",
        },
    ],
}


async def main():
    print("=" * 60)
    print("Backfill Payment History — Abril + Maio 2026")
    print("=" * 60)

    print("\n[1/2] Registrando ABRIL/2026...")
    try:
        r1 = await register_to_sheet(**ABRIL_2026)
        print(f"  OK — {r1.get('rows_added')} linhas adicionadas")
        print(f"  URL: {r1.get('sheet_url')}")
    except PermissionError as e:
        print(f"  ERRO DE PERMISSAO: {e}")
        print("  Reautentique Google OAuth (precisa do scope spreadsheets)")
        return
    except Exception as e:
        print(f"  ERRO: {e}")
        return

    print("\n[2/2] Registrando MAIO/2026...")
    try:
        r2 = await register_to_sheet(**MAIO_2026)
        print(f"  OK — {r2.get('rows_added')} linhas adicionadas")
        print(f"  URL: {r2.get('sheet_url')}")
    except Exception as e:
        print(f"  ERRO: {e}")
        return

    print("\n" + "=" * 60)
    print("Backfill concluido com sucesso!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
