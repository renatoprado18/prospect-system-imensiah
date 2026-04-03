"""
Servico de Deteccao de Duplicados

Identifica contatos duplicados usando:
- Fuzzy matching de nomes
- Correspondencia exata de emails
- Normalizacao e comparacao de telefones

Autor: INTEL
Data: 2026-03-25
"""

from datetime import datetime
from typing import Dict, List, Optional, Tuple, Set
import json
import re
import logging
from collections import defaultdict

from database import get_db

logger = logging.getLogger(__name__)


# ============== UTILITARIOS ==============

def normalize_name(name: str) -> str:
    """Normaliza nome para comparacao."""
    if not name:
        return ""
    # Lowercase, remove acentos basicos, strip
    name = name.lower().strip()
    # Remove caracteres especiais
    name = re.sub(r'[^\w\s]', '', name)
    # Remove espacos extras
    name = re.sub(r'\s+', ' ', name)
    return name


def normalize_phone(phone: str) -> str:
    """Normaliza telefone para comparacao (apenas digitos)."""
    if not phone:
        return ""
    # Remove tudo exceto digitos
    digits = re.sub(r'\D', '', str(phone))
    # Remove codigo do pais se presente (55 para Brasil)
    if len(digits) > 11 and digits.startswith('55'):
        digits = digits[2:]
    # Remove zero inicial de DDD se presente
    if len(digits) == 11 and digits[2] == '9':
        return digits  # Celular com DDD
    if len(digits) == 10:
        return digits  # Fixo com DDD
    if len(digits) == 9:
        return digits  # Celular sem DDD
    if len(digits) == 8:
        return digits  # Fixo sem DDD
    return digits


def extract_emails(emails_data) -> Set[str]:
    """Extrai emails normalizados de diferentes formatos."""
    emails = set()
    if not emails_data:
        return emails

    email_list = []
    if isinstance(emails_data, str):
        try:
            email_list = json.loads(emails_data)
        except:
            email_list = [{"email": emails_data}]
    elif isinstance(emails_data, list):
        email_list = emails_data

    for item in email_list:
        if isinstance(item, dict):
            email = item.get("email", "")
        else:
            email = str(item)
        if email:
            emails.add(email.lower().strip())

    return emails


def extract_phones(phones_data) -> Set[str]:
    """Extrai telefones normalizados de diferentes formatos."""
    phones = set()
    if not phones_data:
        return phones

    phone_list = []
    if isinstance(phones_data, str):
        try:
            phone_list = json.loads(phones_data)
        except:
            phone_list = [{"number": phones_data}]
    elif isinstance(phones_data, list):
        phone_list = phones_data

    for item in phone_list:
        if isinstance(item, dict):
            phone = item.get("number", "") or item.get("phone", "")
        else:
            phone = str(item)
        if phone:
            normalized = normalize_phone(phone)
            if len(normalized) >= 8:  # Minimo para ser valido
                phones.add(normalized)

    return phones


def levenshtein_distance(s1: str, s2: str) -> int:
    """Calcula distancia de Levenshtein entre duas strings."""
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)

    if len(s2) == 0:
        return len(s1)

    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]


def similarity_ratio(s1: str, s2: str) -> float:
    """Calcula similaridade entre 0 e 1."""
    if not s1 or not s2:
        return 0.0
    if s1 == s2:
        return 1.0

    distance = levenshtein_distance(s1, s2)
    max_len = max(len(s1), len(s2))
    return 1 - (distance / max_len)


def name_similarity(name1: str, name2: str) -> float:
    """
    Calcula similaridade de nomes considerando:
    - Similaridade geral
    - Primeiros nomes iguais
    - Sobrenomes iguais
    """
    n1 = normalize_name(name1)
    n2 = normalize_name(name2)

    if not n1 or not n2:
        return 0.0

    # Similaridade direta
    direct_sim = similarity_ratio(n1, n2)

    # Comparar partes do nome
    parts1 = n1.split()
    parts2 = n2.split()

    if not parts1 or not parts2:
        return direct_sim

    # Primeiro nome igual?
    first_match = 1.0 if parts1[0] == parts2[0] else similarity_ratio(parts1[0], parts2[0])

    # Ultimo nome igual?
    last_match = 1.0 if parts1[-1] == parts2[-1] else similarity_ratio(parts1[-1], parts2[-1])

    # Combinar scores
    # Se primeiro e ultimo nomes sao muito similares, e forte indicador
    if first_match > 0.8 and last_match > 0.8:
        return max(direct_sim, (first_match + last_match) / 2)

    return direct_sim


def calculate_duplicate_score(contact1: Dict, contact2: Dict) -> Tuple[float, List[str]]:
    """
    Calcula score de duplicidade entre dois contatos.

    Returns:
        Tuple[score, reasons]:
            - score: 0.0 a 1.0 (1.0 = definitivamente duplicado)
            - reasons: lista de motivos
    """
    score = 0.0
    reasons = []

    # 1. Email exato (match forte)
    emails1 = extract_emails(contact1.get("emails"))
    emails2 = extract_emails(contact2.get("emails"))
    common_emails = emails1 & emails2
    if common_emails:
        score += 0.5  # Email igual e muito forte
        reasons.append(f"Email igual: {list(common_emails)[0]}")

    # 2. Telefone normalizado igual (match forte)
    phones1 = extract_phones(contact1.get("telefones"))
    phones2 = extract_phones(contact2.get("telefones"))
    common_phones = phones1 & phones2
    if common_phones:
        score += 0.4
        reasons.append(f"Telefone igual: {list(common_phones)[0]}")

    # 3. Nome similar
    name_sim = name_similarity(
        contact1.get("nome", ""),
        contact2.get("nome", "")
    )
    if name_sim >= 0.9:
        score += 0.3
        reasons.append(f"Nome muito similar ({name_sim:.0%})")
    elif name_sim >= 0.75:
        score += 0.2
        reasons.append(f"Nome similar ({name_sim:.0%})")
    elif name_sim >= 0.6:
        score += 0.1
        reasons.append(f"Nome parcialmente similar ({name_sim:.0%})")

    # 4. Empresa igual (bonus se outros criterios ja batem)
    emp1 = normalize_name(contact1.get("empresa", ""))
    emp2 = normalize_name(contact2.get("empresa", ""))
    if emp1 and emp2 and emp1 == emp2 and score > 0:
        score += 0.1
        reasons.append(f"Empresa igual: {contact1.get('empresa')}")

    # Cap at 1.0
    score = min(1.0, score)

    return score, reasons


# ============== FUNCOES PRINCIPAIS ==============

def encontrar_duplicados(
    threshold: float = 0.5,
    limit: int = 100,
    offset: int = 0
) -> Dict:
    """
    Encontra possiveis contatos duplicados.

    Args:
        threshold: Score minimo para considerar duplicado (0.0-1.0)
        limit: Numero maximo de pares a retornar
        offset: Offset para paginacao

    Returns:
        Dict com lista de pares duplicados e metadados
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Estrategia: primeiro encontra candidatos por email/telefone/nome
        # para evitar comparar todos com todos (O(n^2))

        # 1. Buscar todos os contatos com dados relevantes
        cursor.execute("""
            SELECT id, nome, empresa, cargo, emails, telefones,
                   foto_url, tags, score, criado_em
            FROM contacts
            WHERE nome IS NOT NULL AND nome != ''
            ORDER BY id
        """)
        contacts = [dict(row) for row in cursor.fetchall()]

    # 2. Criar indices para busca rapida
    email_index = defaultdict(list)  # email -> [contact_ids]
    phone_index = defaultdict(list)  # phone -> [contact_ids]
    name_prefix_index = defaultdict(list)  # first 3 chars of name -> [contact_ids]

    for contact in contacts:
        cid = contact["id"]

        # Index por email
        for email in extract_emails(contact.get("emails")):
            email_index[email].append(cid)

        # Index por telefone
        for phone in extract_phones(contact.get("telefones")):
            phone_index[phone].append(cid)

        # Index por prefixo do nome (para fuzzy match)
        name = normalize_name(contact.get("nome", ""))
        if len(name) >= 3:
            name_prefix_index[name[:3]].append(cid)

    # 3. Encontrar candidatos (pares que compartilham algo)
    contact_map = {c["id"]: c for c in contacts}
    candidates = set()  # Set de tuples (menor_id, maior_id)

    # Candidatos por email
    for email, cids in email_index.items():
        if len(cids) > 1:
            for i in range(len(cids)):
                for j in range(i + 1, len(cids)):
                    pair = (min(cids[i], cids[j]), max(cids[i], cids[j]))
                    candidates.add(pair)

    # Candidatos por telefone
    for phone, cids in phone_index.items():
        if len(cids) > 1:
            for i in range(len(cids)):
                for j in range(i + 1, len(cids)):
                    pair = (min(cids[i], cids[j]), max(cids[i], cids[j]))
                    candidates.add(pair)

    # Candidatos por nome similar (mesmo prefixo)
    for prefix, cids in name_prefix_index.items():
        if len(cids) > 1 and len(cids) <= 50:  # Evita grupos muito grandes
            for i in range(len(cids)):
                for j in range(i + 1, len(cids)):
                    pair = (min(cids[i], cids[j]), max(cids[i], cids[j]))
                    candidates.add(pair)

    # 4. Calcular score para cada candidato
    duplicates = []
    for id1, id2 in candidates:
        contact1 = contact_map.get(id1)
        contact2 = contact_map.get(id2)

        if not contact1 or not contact2:
            continue

        score, reasons = calculate_duplicate_score(contact1, contact2)

        if score >= threshold:
            duplicates.append({
                "contact1": {
                    "id": contact1["id"],
                    "nome": contact1.get("nome"),
                    "empresa": contact1.get("empresa"),
                    "emails": contact1.get("emails"),
                    "telefones": contact1.get("telefones")
                },
                "contact2": {
                    "id": contact2["id"],
                    "nome": contact2.get("nome"),
                    "empresa": contact2.get("empresa"),
                    "emails": contact2.get("emails"),
                    "telefones": contact2.get("telefones")
                },
                "score": round(score, 2),
                "reasons": reasons
            })

    # Ordenar por score (maior primeiro)
    duplicates.sort(key=lambda x: x["score"], reverse=True)

    # Aplicar paginacao
    total = len(duplicates)
    duplicates = duplicates[offset:offset + limit]

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "threshold": threshold,
        "duplicates": duplicates
    }


def merge_contatos(keep_id: int, merge_id: int, field_choices: Optional[Dict] = None) -> Dict:
    """
    Merge dois contatos, mantendo dados mais completos ou respeitando escolhas do usuario.

    O contato merge_id sera excluido apos transferir dados para keep_id.

    Args:
        keep_id: ID do contato a manter
        merge_id: ID do contato a ser mergeado
        field_choices: Opcional. Dict mapeando campo -> contact_id ou 'combine'
            Ex: {"nome": 123, "emails": "combine", "empresa": 456}
            Se nao fornecido, usa logica automatica (comportamento atual)

    Returns:
        Dict com resultado do merge
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Buscar ambos os contatos
        cursor.execute("""
            SELECT * FROM contacts WHERE id IN (%s, %s)
        """, (keep_id, merge_id))

        rows = cursor.fetchall()
        if len(rows) != 2:
            return {"error": "Um ou ambos os contatos nao encontrados"}

        contacts = {row["id"]: dict(row) for row in rows}
        keep = contacts.get(keep_id)
        merge = contacts.get(merge_id)

        if not keep or not merge:
            return {"error": "Contatos nao encontrados"}

        # Campos a mesclar
        updates = {}
        merged_fields = []

        # Helper para decidir valor de um campo
        def get_field_value(field, keep_val, merge_val, is_combine_field=False):
            """Decide qual valor usar para um campo."""
            if field_choices and field in field_choices:
                choice = field_choices[field]
                if choice == "combine" and is_combine_field:
                    return "combine"  # Flag especial para combinar
                elif choice == keep_id:
                    return keep_val
                elif choice == merge_id:
                    return merge_val
                else:
                    # Se o choice nao for nem keep_id nem merge_id, assume keep
                    return keep_val
            # Se nao tem field_choices ou o campo nao esta nele, usa logica automatica
            return None  # Sinaliza para usar logica automatica

        # Para cada campo texto, respeitar escolha do usuario ou manter o mais completo
        text_fields = ["nome", "empresa", "cargo", "linkedin", "foto_url", "apelido",
                       "contexto", "aniversario", "circulo", "manual_notes"]
        for field in text_fields:
            keep_val = keep.get(field)
            merge_val = merge.get(field)

            chosen = get_field_value(field, keep_val, merge_val)
            if chosen is not None:
                # Usuario escolheu
                if chosen != keep_val and chosen:
                    updates[field] = chosen
                    merged_fields.append(field)
            else:
                # Logica automatica: manter valor mais completo
                if not keep_val and merge_val:
                    updates[field] = merge_val
                    merged_fields.append(field)

        # Mesclar emails
        keep_emails = extract_emails(keep.get("emails"))
        merge_emails = extract_emails(merge.get("emails"))

        email_choice = get_field_value("emails", keep.get("emails"), merge.get("emails"), is_combine_field=True)
        if email_choice == "combine" or email_choice is None:
            # Combinar todos
            all_emails = keep_emails | merge_emails
            if len(all_emails) > len(keep_emails):
                email_list = [{"email": e} for e in all_emails]
                updates["emails"] = json.dumps(email_list)
                merged_fields.append("emails")
        elif email_choice:
            # Usuario escolheu um especifico
            updates["emails"] = json.dumps(json.loads(email_choice) if isinstance(email_choice, str) else email_choice)
            merged_fields.append("emails")

        # Mesclar telefones
        keep_phones = set()
        merge_phones = set()
        try:
            kp = json.loads(keep.get("telefones") or "[]")
            keep_phones = set(json.dumps(p) for p in kp)
        except:
            pass
        try:
            mp = json.loads(merge.get("telefones") or "[]")
            merge_phones = set(json.dumps(p) for p in mp)
        except:
            pass

        phone_choice = get_field_value("telefones", keep.get("telefones"), merge.get("telefones"), is_combine_field=True)
        if phone_choice == "combine" or phone_choice is None:
            # Combinar todos
            all_phones = keep_phones | merge_phones
            if len(all_phones) > len(keep_phones):
                phone_list = [json.loads(p) for p in all_phones]
                updates["telefones"] = json.dumps(phone_list)
                merged_fields.append("telefones")
        elif phone_choice:
            # Usuario escolheu um especifico
            updates["telefones"] = json.dumps(json.loads(phone_choice) if isinstance(phone_choice, str) else phone_choice)
            merged_fields.append("telefones")

        # Mesclar tags (sempre combina por padrao)
        keep_tags = set()
        merge_tags = set()
        try:
            kt = json.loads(keep.get("tags") or "[]")
            keep_tags = set(kt)
        except:
            pass
        try:
            mt = json.loads(merge.get("tags") or "[]")
            merge_tags = set(mt)
        except:
            pass

        all_tags = keep_tags | merge_tags
        if len(all_tags) > len(keep_tags):
            updates["tags"] = json.dumps(list(all_tags))
            merged_fields.append("tags")

        # Mesclar enderecos e relacionamentos (novos campos)
        for json_field in ["enderecos", "relacionamentos", "datas_importantes"]:
            keep_data = []
            merge_data = []
            try:
                keep_data = json.loads(keep.get(json_field) or "[]")
            except:
                pass
            try:
                merge_data = json.loads(merge.get(json_field) or "[]")
            except:
                pass

            field_choice = get_field_value(json_field, keep.get(json_field), merge.get(json_field), is_combine_field=True)
            if field_choice == "combine" or field_choice is None:
                # Combinar (evitar duplicatas por comparacao serializada)
                keep_set = set(json.dumps(d, sort_keys=True) for d in keep_data)
                merge_set = set(json.dumps(d, sort_keys=True) for d in merge_data)
                all_data = keep_set | merge_set
                if len(all_data) > len(keep_set):
                    updates[json_field] = json.dumps([json.loads(d) for d in all_data])
                    merged_fields.append(json_field)
            elif field_choice:
                # Usuario escolheu um especifico
                updates[json_field] = json.dumps(json.loads(field_choice) if isinstance(field_choice, str) else field_choice)
                merged_fields.append(json_field)

        # Manter maior score (se nao especificado)
        score_choice = get_field_value("score", keep.get("score"), merge.get("score"))
        if score_choice is not None and score_choice != keep.get("score"):
            updates["score"] = score_choice
            merged_fields.append("score")
        elif score_choice is None and (merge.get("score") or 0) > (keep.get("score") or 0):
            updates["score"] = merge.get("score")
            merged_fields.append("score")

        # Manter total_interacoes somado
        keep_int = keep.get("total_interacoes") or 0
        merge_int = merge.get("total_interacoes") or 0
        if merge_int > 0:
            updates["total_interacoes"] = keep_int + merge_int
            merged_fields.append("total_interacoes")

        # Manter data mais antiga de criacao
        if merge.get("criado_em") and keep.get("criado_em"):
            if merge["criado_em"] < keep["criado_em"]:
                updates["criado_em"] = merge["criado_em"]
                merged_fields.append("criado_em")

        # Atualizar contato a manter
        if updates:
            set_clause = ", ".join([f"{k} = %s" for k in updates.keys()])
            values = list(updates.values()) + [keep_id]
            cursor.execute(f"""
                UPDATE contacts SET {set_clause} WHERE id = %s
            """, values)

        # Transferir mensagens
        try:
            cursor.execute("""
                UPDATE messages SET contact_id = %s WHERE contact_id = %s
            """, (keep_id, merge_id))
            messages_transferred = cursor.rowcount
        except:
            messages_transferred = 0

        # Transferir conversas
        try:
            cursor.execute("""
                UPDATE conversations SET contact_id = %s WHERE contact_id = %s
            """, (keep_id, merge_id))
            conversations_transferred = cursor.rowcount
        except:
            conversations_transferred = 0

        # Transferir tasks
        try:
            cursor.execute("""
                UPDATE tasks SET contact_id = %s WHERE contact_id = %s
            """, (keep_id, merge_id))
            tasks_transferred = cursor.rowcount
        except:
            tasks_transferred = 0

        # Excluir contato mergeado
        cursor.execute("DELETE FROM contacts WHERE id = %s", (merge_id,))

        logger.info(f"Merge: {merge_id} -> {keep_id}, campos: {merged_fields}")

        return {
            "success": True,
            "keep_id": keep_id,
            "merged_id": merge_id,
            "merged_fields": merged_fields,
            "messages_transferred": messages_transferred,
            "conversations_transferred": conversations_transferred,
            "tasks_transferred": tasks_transferred
        }


def get_duplicate_statistics() -> Dict:
    """
    Retorna estatisticas sobre duplicados no sistema.

    Returns:
        Dict com contagens e distribuicao
    """
    result = encontrar_duplicados(threshold=0.5, limit=1000)

    # Agrupar por faixa de score
    by_score = {
        "alto (>0.8)": 0,
        "medio (0.6-0.8)": 0,
        "baixo (0.5-0.6)": 0
    }

    for dup in result["duplicates"]:
        score = dup["score"]
        if score > 0.8:
            by_score["alto (>0.8)"] += 1
        elif score > 0.6:
            by_score["medio (0.6-0.8)"] += 1
        else:
            by_score["baixo (0.5-0.6)"] += 1

    return {
        "total_possiveis_duplicados": result["total"],
        "por_confianca": by_score,
        "top_5": result["duplicates"][:5]
    }
