#!/usr/bin/env python3
"""
Script para importar árvore genealógica do MyHeritage e atualizar círculos

Mapeamento de parentesco para círculos:
- Círculo 1: filhos, esposa, pais, irmãos
- Círculo 2: avós, sogros, cunhados, sobrinhos
- Círculo 3: tios, primos, primos da esposa

Autor: 1ARCH
Data: 2026-03-25
"""

import csv
import os
import sys
import json
import re
from datetime import datetime
from typing import Dict, List, Optional

# Adiciona o diretório app ao path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
from database import get_db

# Mapeamento de parentesco para círculo
PARENTESCO_CIRCULO = {
    # Círculo 1 - Família imediata
    "Seu filho": 1,
    "Sua filha": 1,
    "Sua esposa": 1,
    "Seu pai": 1,
    "Sua mãe": 1,
    "Seu irmäo": 1,
    "Seu irmão": 1,
    "Sua irmã": 1,

    # Círculo 2 - Família próxima
    "O seu avô paterno": 2,
    "A sua avó paterna": 2,
    "O seu avô materno": 2,
    "A sua avó materna": 2,
    "A sua sobrinha": 2,
    "O seu sobrinho": 2,
    "O seu sogro": 2,
    "A sua sogra": 2,
    "A sua cunhada": 2,
    "O seu cunhado": 2,
    "A sua madrasta": 2,

    # Círculo 3 - Família extensa
    "O seu tio": 3,
    "A sua tia": 3,
    "O seu primo": 3,
    "A sua prima": 3,

    # Família da esposa - Círculo 3
    "Avô de sua esposa": 3,
    "Avó de sua esposa": 3,
    "Tio de sua esposa": 3,
    "Tia de sua esposa": 3,
    "Primo de sua esposa": 3,
    "Prima de sua esposa": 3,
    "Sobrinho de sua esposa": 3,
    "Sobrinha de sua esposa": 3,
}

# Tags a serem adicionadas por parentesco
PARENTESCO_TAGS = {
    "Seu filho": ["familia", "filho"],
    "Sua filha": ["familia", "filha"],
    "Sua esposa": ["familia", "esposa"],
    "Seu pai": ["familia", "pai"],
    "Sua mãe": ["familia", "mae"],
    "Seu irmäo": ["familia", "irmao"],
    "Seu irmão": ["familia", "irmao"],
    "Sua irmã": ["familia", "irma"],
    "O seu avô paterno": ["familia", "avo"],
    "A sua avó paterna": ["familia", "avo"],
    "O seu avô materno": ["familia", "avo"],
    "A sua avó materna": ["familia", "avo"],
    "A sua sobrinha": ["familia", "sobrinho"],
    "O seu sobrinho": ["familia", "sobrinho"],
    "O seu sogro": ["familia", "sogro"],
    "A sua sogra": ["familia", "sogra"],
    "A sua cunhada": ["familia", "cunhado"],
    "O seu cunhado": ["familia", "cunhado"],
    "A sua madrasta": ["familia"],
    "O seu tio": ["familia", "tio"],
    "A sua tia": ["familia", "tia"],
    "O seu primo": ["familia", "primo"],
    "A sua prima": ["familia", "prima"],
}


def parse_date(date_str: str) -> Optional[str]:
    """Converte data do formato MyHeritage para ISO."""
    if not date_str or date_str.strip() == "":
        return None

    meses = {
        "janeiro": "01", "fevereiro": "02", "março": "03", "marco": "03",
        "abril": "04", "maio": "05", "junho": "06",
        "julho": "07", "agosto": "08", "setembro": "09",
        "outubro": "10", "novembro": "11", "dezembro": "12"
    }

    match = re.match(r'(\d{1,2}) de (\w+) de (\d{4})', date_str)
    if match:
        dia, mes, ano = match.groups()
        mes_num = meses.get(mes.lower(), "01")
        return f"{ano}-{mes_num}-{dia.zfill(2)}"

    match = re.match(r'(\d{2})/(\d{2})/(\d{4})', date_str)
    if match:
        dia, mes, ano = match.groups()
        return f"{ano}-{mes}-{dia}"

    match = re.match(r'^(\d{4})$', date_str)
    if match:
        return f"{match.group(1)}-01-01"

    return None


def normalize_name(name: str) -> str:
    """Normaliza nome para comparação."""
    name = re.sub(r'^(Dr\.|Dra\.|Engº|Prof\.|Profª)\s*', '', name, flags=re.IGNORECASE)
    name = ' '.join(name.split())
    return name.strip().lower()


def load_family_tree(csv_path: str) -> List[Dict]:
    """Carrega e processa a árvore genealógica do CSV."""
    family = []

    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            parentesco = row.get('Parentesco', '').strip()
            nome = row.get('Nome', '').strip()
            falecimento = row.get('Data de falecimento', '').strip()

            if not nome or not parentesco:
                continue
            if falecimento:
                continue
            if parentesco == "Você":
                continue

            circulo = PARENTESCO_CIRCULO.get(parentesco)
            if not circulo:
                continue

            nascimento = parse_date(row.get('Data de nascimento', ''))
            genero = row.get('Gênero', '')

            family.append({
                'nome': nome,
                'nome_normalizado': normalize_name(nome),
                'parentesco': parentesco,
                'circulo': circulo,
                'nascimento': nascimento,
                'genero': genero,
                'tags': PARENTESCO_TAGS.get(parentesco, ["familia"])
            })

    return family


def find_contact_by_name(conn, nome_normalizado: str) -> Optional[Dict]:
    """Busca contato no banco por nome (fuzzy match)."""
    cursor = conn.cursor()

    # Match exato
    cursor.execute(
        "SELECT id, nome, emails, tags, circulo, circulo_manual, aniversario FROM contacts WHERE LOWER(nome) = %s",
        (nome_normalizado,)
    )
    result = cursor.fetchone()
    if result:
        return dict(result)

    # Match parcial
    cursor.execute(
        "SELECT id, nome, emails, tags, circulo, circulo_manual, aniversario FROM contacts WHERE LOWER(nome) LIKE %s ORDER BY LENGTH(nome) DESC LIMIT 1",
        (f'%{nome_normalizado}%',)
    )
    result = cursor.fetchone()
    if result:
        return dict(result)

    # Match primeiro e último nome
    partes = nome_normalizado.split()
    if len(partes) >= 2:
        primeiro = partes[0]
        ultimo = partes[-1]
        cursor.execute(
            "SELECT id, nome, emails, tags, circulo, circulo_manual, aniversario FROM contacts WHERE LOWER(nome) LIKE %s AND LOWER(nome) LIKE %s ORDER BY LENGTH(nome) DESC LIMIT 1",
            (f'{primeiro}%', f'%{ultimo}')
        )
        result = cursor.fetchone()
        if result:
            return dict(result)

    return None


def update_contact_circulo(conn, contact_id: int, circulo: int, tags: List[str],
                           nascimento: Optional[str] = None) -> bool:
    """Atualiza círculo e tags de um contato."""
    cursor = conn.cursor()

    # Busca tags atuais
    cursor.execute("SELECT tags FROM contacts WHERE id = %s", (contact_id,))
    result = cursor.fetchone()

    current_tags = []
    if result and result.get('tags'):
        try:
            current_tags = json.loads(result['tags']) if isinstance(result['tags'], str) else result['tags']
        except:
            current_tags = []

    # Adiciona novas tags sem duplicar
    new_tags = list(set(current_tags + tags))

    if nascimento:
        cursor.execute("""
            UPDATE contacts
            SET circulo = %s, circulo_manual = true, tags = %s, aniversario = %s, ultimo_calculo_circulo = NOW()
            WHERE id = %s
        """, (circulo, json.dumps(new_tags), nascimento, contact_id))
    else:
        cursor.execute("""
            UPDATE contacts
            SET circulo = %s, circulo_manual = true, tags = %s, ultimo_calculo_circulo = NOW()
            WHERE id = %s
        """, (circulo, json.dumps(new_tags), contact_id))

    conn.commit()
    return cursor.rowcount > 0


def main():
    """Executa a importação da árvore genealógica."""
    csv_path = "/Users/rap/Downloads/MyHeritage de Faria e Almeida Prado Family Tree Lista de pessoas.csv"

    print("=" * 60)
    print("IMPORTAÇÃO DA ÁRVORE GENEALÓGICA MYHERITAGE")
    print("=" * 60)

    print("\n1. Carregando árvore genealógica...")
    family = load_family_tree(csv_path)
    print(f"   Encontrados {len(family)} familiares vivos com parentesco mapeado")

    por_circulo = {}
    for f in family:
        c = f['circulo']
        por_circulo[c] = por_circulo.get(c, 0) + 1

    print("\n   Distribuição por círculo:")
    for c in sorted(por_circulo.keys()):
        print(f"   - Círculo {c}: {por_circulo[c]} pessoas")

    print("\n2. Conectando ao banco de dados...")

    print("\n3. Processando familiares...")
    encontrados = 0
    atualizados = 0
    nao_encontrados = []

    with get_db() as conn:
        for familiar in family:
            contact = find_contact_by_name(conn, familiar['nome_normalizado'])

            if contact:
                encontrados += 1
                old_circulo = contact.get('circulo')

                if old_circulo is None or familiar['circulo'] < old_circulo:
                    update_contact_circulo(
                        conn,
                        contact['id'],
                        familiar['circulo'],
                        familiar['tags'],
                        familiar.get('nascimento')
                    )
                    atualizados += 1
                    print(f"   ✓ {familiar['nome'][:40]:<40} → Círculo {familiar['circulo']} ({familiar['parentesco']})")
                else:
                    print(f"   = {familiar['nome'][:40]:<40} já está no Círculo {old_circulo}")
            else:
                nao_encontrados.append(familiar)

    print("\n" + "=" * 60)
    print("RESUMO")
    print("=" * 60)
    print(f"Total de familiares processados: {len(family)}")
    print(f"Encontrados no banco: {encontrados}")
    print(f"Círculos atualizados: {atualizados}")
    print(f"Não encontrados: {len(nao_encontrados)}")

    if nao_encontrados:
        print("\n4. Familiares não encontrados no banco:")
        for circulo in [1, 2, 3]:
            fam_circulo = [f for f in nao_encontrados if f['circulo'] == circulo]
            if fam_circulo:
                print(f"\n   Círculo {circulo}:")
                for f in fam_circulo[:10]:
                    print(f"   - {f['nome']} ({f['parentesco']})")
                if len(fam_circulo) > 10:
                    print(f"   ... e mais {len(fam_circulo) - 10}")

    print("\n✓ Importação concluída!")
    return atualizados


if __name__ == "__main__":
    main()
