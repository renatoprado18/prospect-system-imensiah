"""
Contact Deduplication and Normalization Service

Features:
- Detect duplicates by phone number or email
- Normalize names (fix CAPS LOCK, proper capitalization)
- Merge contacts from personal + professional accounts
- Propagate updates to both Google accounts
"""
import re
import json
from typing import List, Dict, Any, Optional, Tuple
from collections import defaultdict
import unicodedata


# ============== Reference migration on merge ==============
#
# Tabelas com FK para contacts(id). Antes de deletar um contato secundario
# durante o merge, precisamos repontar as referencias pro primario — caso
# contrario as FK CASCADE apagam o historico em cascata (vide incidente
# 16/06/26 com merge do Eduardo Marson: ~17 msgs/PDF/anexos perdidos).
#
# Lista derivada de:
#   SELECT tc.table_name, kcu.column_name, rc.delete_rule
#   FROM information_schema.referential_constraints rc
#   JOIN information_schema.table_constraints tc ON tc.constraint_name = rc.constraint_name
#   JOIN information_schema.key_column_usage kcu ON kcu.constraint_name = rc.constraint_name
#   JOIN information_schema.constraint_column_usage ccu ON ccu.constraint_name = rc.constraint_name
#   WHERE ccu.table_name='contacts' AND ccu.column_name='id';
#
# Inclui CASCADE, SET NULL e NO ACTION — em todos os 3 casos a migracao
# preserva o vinculo do dado historico ao contato primario.

_FK_TABLES_TO_CONTACTS = [
    # ON DELETE CASCADE (sem migracao, dados sao apagados)
    'action_proposals',
    'ai_suggestions',
    'campaign_enrollments',
    'conselhoos_links',
    'contact_briefings',
    'contact_facts',
    'contact_interactions',
    'contact_memories',
    'contact_prospect_link',
    'contact_rodas',
    'contact_snoozes',
    'contact_today_manual',
    'conversations',
    # 074: vinculo pessoa<->lugar COM PAPEL. Sem esta linha o merge apagaria em
    # CASCADE justamente o que a entidade existe pra guardar — "fulano e'
    # proprietario da fazenda" — e o dano seria invisivel: o merge termina 200.
    'locais_contatos',
    'health_predictions',
    'linkedin_enrichment_history',
    'messages',
    'project_members',
    'proposal_block_rules',
    'timeline_summaries',
    'whatsapp_messages',
    # ON DELETE SET NULL (sem migracao, vinculo se perde)
    'calendar_events',
    'cos_draft_responses',
    'delegations',
    'email_triage',
    'group_messages',
    'linkedin_engagement_signals',
    'users',
    'wa_triage',  # faltava (10/07): merge via servico deixava 12+ linhas orfas em SET NULL
    # ON DELETE NO ACTION (sem migracao, DELETE em contacts falha)
    'conselhoos_board_members',
    'reminders',
    'tasks',
    # SEM FK declarada — a lista original foi levantada pelas constraints, entao
    # estas passavam batido: o DELETE nao falha, mas o contact_id fica apontando
    # pra linha inexistente (orfa silenciosa, pior que erro). Auditadas 25/07/26
    # comparando information_schema.columns contra esta lista.
    'analyzer_feedback',
    'tonha_role_contacts',
    # tonia_*: o INTEL nao escreve CONTEUDO nessas tabelas (convencao), mas
    # reparo referencial e outra coisa — sem isto a tonIAH passa a ler um
    # contato fantasma depois de todo merge.
    'tonia_conversations',
    'tonia_media_index',
    # --- auditadas 23/08/26, tres semanas depois da varredura de 25/07 ---
    # A lista envelheceu em 3 semanas: o contrato foi a 168 tabelas e estas 4
    # entraram sem que nada avisasse. As duas primeiras sao o caso grave, e nao
    # por volume — sao o REGISTRO DE UMA DECISAO DO RENATO ("nao funda esta
    # ficha com o Google", "esta empresa nao vale pra esta pessoa"), gravado
    # justamente pra parar de ser reperguntado (migrations 077/078/079). Ambas
    # em ON DELETE CASCADE: o merge apagaria o veto e voltaria a perguntar, e o
    # dano seria invisivel porque o merge termina 200.
    'google_nao_fundir',          # CASCADE · UNIQUE (contact_id, google_rid)
    'contato_empresa_mantida',    # CASCADE · UNIQUE (contact_id, empresa_ignorada)
    'email_drafts',               # SET NULL · perde o vinculo em silencio
    'check_g_ledger',             # SEM FK · 1.393 linhas viravam orfas apontando
                                  # pra id inexistente — pior que erro, porque o
                                  # placar do check-G segue somando sobre lixo
]

# Tabelas com unique constraint composta envolvendo contact_id.
# Antes do UPDATE em massa, precisamos deletar as rows do secundario
# que conflitariam com as do primario (politica: primario vence).
# Valores = colunas que compoem a unique junto com contact_id.
_COMPOSITE_UNIQUE_PARTNERS = {
    'campaign_enrollments': ['campaign_id'],
    'contact_prospect_link': ['prospect_id'],
    'contact_today_manual': ['data'],
    # 074: UNIQUE (local_id, contact_id, papel). Sem isto, mesclar duas fichas
    # que tenham o MESMO papel no MESMO lugar (o caso provavel: duplicata da
    # mesma pessoa) explode o UPDATE em violacao de unique no meio do merge.
    'locais_contatos': ['local_id', 'papel'],
    'project_members': ['project_id'],
    'timeline_summaries': ['cache_hash'],
    # 077/078 (23/08): sem estas duas linhas, acrescentar as tabelas do veto a
    # _FK_TABLES_TO_CONTACTS trocaria um defeito por outro — o UPDATE explodiria
    # em violacao de unique no MEIO do merge, com parte das FKs ja repontadas.
    # A politica "primario vence" e' a certa aqui: se o primario ja tem o mesmo
    # veto (mesmo google_rid), a linha do secundario e' redundante e some sem
    # perda; se o veto e' outro, o UPDATE o reponta e ele sobrevive ao merge.
    'google_nao_fundir': ['google_rid'],
    'contato_empresa_mantida': ['empresa_ignorada'],
}


def _migrate_contact_references(cursor, primary_id: int, other_ids: List[int]) -> None:
    """
    Repontar todas as referencias FK contact_id de other_ids -> primary_id,
    de forma segura para uniques compostas.

    Deve ser chamada ANTES de DELETE FROM contacts. Sem isso, FKs CASCADE
    apagam historico irrecuperavel e FKs NO ACTION fazem o DELETE explodir.

    Trata tambem projects.owner_contact_id (coluna fora do padrao).
    """
    if not other_ids:
        return

    # 1) Tabelas com unique composta — deletar rows do secundario que
    #    duplicariam o primario, antes do UPDATE
    for tbl, partner_cols in _COMPOSITE_UNIQUE_PARTNERS.items():
        partner_join = ' AND '.join(f't1.{c} = t2.{c}' for c in partner_cols)
        cursor.execute(f'''
            DELETE FROM {tbl} t1
            WHERE t1.contact_id = ANY(%s)
              AND EXISTS (
                SELECT 1 FROM {tbl} t2
                WHERE t2.contact_id = %s AND {partner_join}
              )
        ''', (other_ids, primary_id))

    # 2) UPDATE contact_id em todas as tabelas com FK pra contacts(id)
    for tbl in _FK_TABLES_TO_CONTACTS:
        cursor.execute(
            f"UPDATE {tbl} SET contact_id = %s WHERE contact_id = ANY(%s)",
            (primary_id, other_ids)
        )

    # 3) projects.owner_contact_id (coluna fora do padrao)
    cursor.execute(
        "UPDATE projects SET owner_contact_id = %s WHERE owner_contact_id = ANY(%s)",
        (primary_id, other_ids)
    )


# ============== Phone Number Normalization ==============

def normalize_phone(phone: str) -> str:
    """
    Normalize phone number to a standard format.
    Removes all non-digits, handles Brazilian format.
    Returns: digits only, with country code if present
    """
    if not phone:
        return ""

    # Remove all non-digits
    digits = re.sub(r'\D', '', phone)

    # Handle Brazilian numbers
    if digits.startswith('55') and len(digits) >= 12:
        # Already has country code
        return digits
    elif digits.startswith('0') and len(digits) == 11:
        # Remove leading 0, add country code
        return '55' + digits[1:]
    elif len(digits) == 11 and digits[2] == '9':
        # Mobile with DDD (e.g., 11984153337)
        return '55' + digits
    elif len(digits) == 10:
        # Landline with DDD
        return '55' + digits
    elif len(digits) == 9 and digits[0] == '9':
        # Mobile without DDD - can't normalize fully
        return digits
    elif len(digits) == 8:
        # Landline without DDD - can't normalize fully
        return digits

    return digits


def format_phone_display(phone: str) -> str:
    """Format phone for display: +55 (11) 98415-3337"""
    digits = normalize_phone(phone)

    if len(digits) == 13 and digits.startswith('55'):
        # +55 (11) 98415-3337
        return f"+{digits[:2]} ({digits[2:4]}) {digits[4:9]}-{digits[9:]}"
    elif len(digits) == 12 and digits.startswith('55'):
        # +55 (11) 8415-3337 (landline)
        return f"+{digits[:2]} ({digits[2:4]}) {digits[4:8]}-{digits[8:]}"
    elif len(digits) == 11:
        return f"({digits[:2]}) {digits[2:7]}-{digits[7:]}"
    elif len(digits) == 10:
        return f"({digits[:2]}) {digits[2:6]}-{digits[6:]}"

    return phone  # Return original if can't format


# ============== Name Normalization ==============

# Words that should stay lowercase (Portuguese prepositions)
LOWERCASE_WORDS = {'de', 'da', 'do', 'das', 'dos', 'e', 'em', 'a', 'o', 'para'}

# Words that should stay uppercase (acronyms, etc.)
UPPERCASE_WORDS = {'CEO', 'CFO', 'CTO', 'COO', 'CMO', 'VP', 'Jr', 'Sr', 'II', 'III', 'IV'}

# Common name corrections
NAME_CORRECTIONS = {
    'joao': 'João',
    'jose': 'José',
    'maria': 'Maria',
    'paulo': 'Paulo',
    'ana': 'Ana',
    'antonio': 'Antônio',
    'carlos': 'Carlos',
    'francisco': 'Francisco',
    'luiz': 'Luiz',
    'luis': 'Luís',
    'andre': 'André',
    'marcio': 'Márcio',
    'sergio': 'Sérgio',
    'rogerio': 'Rogério',
    'fabio': 'Fábio',
    'flavio': 'Flávio',
    'claudio': 'Cláudio',
    'lucio': 'Lúcio',
    'vinicius': 'Vinícius',
    'thiago': 'Thiago',
    'matheus': 'Matheus',
    'gabriel': 'Gabriel',
    'rafael': 'Rafael',
    'daniel': 'Daniel',
    'lucas': 'Lucas',
    'pedro': 'Pedro',
    'gustavo': 'Gustavo',
    'felipe': 'Felipe',
    'rodrigo': 'Rodrigo',
    'fernando': 'Fernando',
    'renato': 'Renato',
    'ricardo': 'Ricardo',
    'eduardo': 'Eduardo',
    'marcelo': 'Marcelo',
    'leandro': 'Leandro',
    'alexandre': 'Alexandre',
    'roberto': 'Roberto',
    'marcos': 'Marcos',
    'bruno': 'Bruno',
    'diego': 'Diego',
    'caio': 'Caio',
    'henrique': 'Henrique',
    'arthur': 'Arthur',
    'guilherme': 'Guilherme',
    'murilo': 'Murilo',
    'otavio': 'Otávio',
}


def is_all_caps(text: str) -> bool:
    """Check if text is ALL CAPS (ignoring non-letters)"""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    return all(c.isupper() for c in letters)


def is_all_lower(text: str) -> bool:
    """Check if text is all lowercase"""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    return all(c.islower() for c in letters)


def normalize_name(name: str) -> str:
    """
    Normalize a name:
    - Fix ALL CAPS
    - Fix all lowercase
    - Proper capitalization for Portuguese names
    - Handle prepositions (de, da, do, etc.)
    """
    if not name or not name.strip():
        return ""

    name = name.strip()

    # Remove extra whitespace
    name = ' '.join(name.split())

    # Check if needs normalization
    needs_fix = is_all_caps(name) or is_all_lower(name)

    if not needs_fix:
        # Only fix obvious issues
        words = name.split()
        result = []
        for i, word in enumerate(words):
            # Check if this word is ALL CAPS and not an acronym
            if is_all_caps(word) and word.upper() not in UPPERCASE_WORDS and len(word) > 2:
                word = word.capitalize()
            result.append(word)
        return ' '.join(result)

    # Full normalization needed
    words = name.lower().split()
    result = []

    for i, word in enumerate(words):
        # Remove accents for lookup
        word_no_accent = ''.join(
            c for c in unicodedata.normalize('NFD', word)
            if unicodedata.category(c) != 'Mn'
        )

        # Check for known corrections
        if word_no_accent in NAME_CORRECTIONS:
            result.append(NAME_CORRECTIONS[word_no_accent])
        elif word in LOWERCASE_WORDS and i > 0:
            # Prepositions stay lowercase (except at start)
            result.append(word)
        elif word.upper() in UPPERCASE_WORDS:
            result.append(word.upper())
        else:
            # Standard capitalization
            result.append(word.capitalize())

    return ' '.join(result)


def get_name_score(name: str) -> int:
    """
    Score a name's quality (higher = better):
    - Has proper capitalization: +10
    - Has accents where expected: +5
    - Not ALL CAPS: +5
    - Not all lowercase: +5
    - Has both first and last name: +10
    """
    if not name:
        return 0

    score = 0

    # Not ALL CAPS
    if not is_all_caps(name):
        score += 5

    # Not all lowercase
    if not is_all_lower(name):
        score += 5

    # Has multiple words (first + last name)
    words = name.split()
    if len(words) >= 2:
        score += 10

    # Has accents (likely properly formatted)
    if any(ord(c) > 127 for c in name):
        score += 5

    # Proper capitalization pattern
    if words and words[0][0].isupper():
        score += 10

    return score


# ============== Duplicate Detection ==============

def normalize_name_for_dedup(name: str) -> str:
    """Normalize name for duplicate detection"""
    if not name:
        return ""
    # Remove accents
    name = unicodedata.normalize('NFD', name)
    name = ''.join(c for c in name if unicodedata.category(c) != 'Mn')
    # Lowercase and strip
    name = name.lower().strip()
    # Remove common suffixes/prefixes
    name = re.sub(r'\s+(jr\.?|sr\.?|filho|neto|ii|iii|iv)$', '', name, flags=re.IGNORECASE)
    # Remove extra spaces
    name = ' '.join(name.split())
    return name


def find_duplicates(contacts: List[Dict], include_name_duplicates: bool = True) -> Dict[str, List[Dict]]:
    """
    Find duplicate contacts by phone number, email, or name.
    Returns: {normalized_key: [contact1, contact2, ...]}
    """
    # Group by normalized phone
    by_phone = defaultdict(list)
    # Group by email
    by_email = defaultdict(list)
    # Group by name
    by_name = defaultdict(list)

    for contact in contacts:
        contact_id = contact.get('id')

        # Index by phones
        phones = contact.get('telefones', [])
        if isinstance(phones, str):
            try:
                phones = json.loads(phones)
            except:
                phones = []

        for phone_obj in phones:
            if isinstance(phone_obj, dict):
                number = phone_obj.get('number', '')
            else:
                number = str(phone_obj)

            normalized = normalize_phone(number)
            if len(normalized) >= 8:  # Valid phone
                by_phone[normalized].append(contact)

        # Index by emails
        emails = contact.get('emails', [])
        if isinstance(emails, str):
            try:
                emails = json.loads(emails)
            except:
                emails = []

        for email_obj in emails:
            if isinstance(email_obj, dict):
                email = email_obj.get('email', '').lower().strip()
            else:
                email = str(email_obj).lower().strip()

            if email and '@' in email:
                by_email[email].append(contact)

        # Index by name
        if include_name_duplicates:
            name = contact.get('nome', '')
            normalized_name = normalize_name_for_dedup(name)
            if normalized_name and len(normalized_name) >= 3:
                by_name[normalized_name].append(contact)

    # Collect duplicates (more than one contact per key)
    duplicates = {}

    for phone, contacts_list in by_phone.items():
        if len(contacts_list) > 1:
            key = f"phone:{phone}"
            duplicates[key] = contacts_list

    for email, contacts_list in by_email.items():
        if len(contacts_list) > 1:
            key = f"email:{email}"
            # Only add if not already captured by phone
            contact_ids = {c['id'] for c in contacts_list}
            already_captured = False
            for existing_key, existing_contacts in duplicates.items():
                existing_ids = {c['id'] for c in existing_contacts}
                if contact_ids == existing_ids:
                    already_captured = True
                    break

            if not already_captured:
                duplicates[key] = contacts_list

    # Add name-based duplicates
    if include_name_duplicates:
        for name, contacts_list in by_name.items():
            if len(contacts_list) > 1:
                key = f"name:{name}"
                # Only add if not already captured by phone or email
                contact_ids = {c['id'] for c in contacts_list}
                already_captured = False
                for existing_key, existing_contacts in duplicates.items():
                    existing_ids = {c['id'] for c in existing_contacts}
                    if contact_ids == existing_ids:
                        already_captured = True
                        break

                if not already_captured:
                    duplicates[key] = contacts_list

    return duplicates



async def _achar_ou_criar(gc_mod, access_token: str, contact_data: Dict,
                          ignorar_rids: Optional[set] = None) -> Dict:
    """Antes de criar ficha no Google, PROCURA se ela já existe na conta.

    POR QUE (22/08/2026). O caminho acima criava direto sempre que o contato
    mantido não tinha `google_contact_id` PARA AQUELA CONTA — e a coluna é
    escalar enquanto as contas são duas, então numa delas ela é sempre None.
    Efeito: **todo merge fabricava duplicata no Google**. Medido ao fundir
    "Bettina Berman": 2 fichas apagadas e 4 criadas, uma piora líquida.

    É o gerador do passivo que o `verifica_google.py` mede — não o rescaldo dele.
    Quem apaga duplicata por um lado e cria pelo outro fica em disputa consigo
    mesmo, e o mutirão seguinte encontra o mesmo trabalho de novo.

    Busca inconclusiva (None) cai no create, como antes: criar duplicata é menos
    grave que escrever por cima da ficha de outra pessoa.
    """
    telefones = [t.get("number") for t in (contact_data.get("telefones") or [])
                 if isinstance(t, dict) and t.get("number")]
    achado = None
    if hasattr(gc_mod, "buscar_ficha_existente"):
        achado = await gc_mod.buscar_ficha_existente(
            access_token, contact_data.get("nome") or "", telefones)
    # ⚠️ NÃO ESCREVER NA FICHA QUE ESTE MESMO MERGE VAI APAGAR. A busca acha
    # pelo telefone, e o telefone é o mesmo nas fichas do contato absorvido —
    # sem este filtro o merge ATUALIZAVA a ficha condenada e a apagava logo
    # depois, no passo seguinte da mesma função. Medido fundindo "Bel Coelho":
    # `updates` e `deletions` traziam o MESMO resourceName.
    if achado and achado in (ignorar_rids or set()):
        achado = None
    if achado:
        ok = await gc_mod.update_google_contact(access_token, achado, contact_data)
        return {"status": "updated" if ok else "update_falhou", "google_id": achado,
                "achado_por_busca": True}
    novo_id = await gc_mod.create_google_contact(access_token, contact_data)
    return {"status": "created", "google_id": novo_id}


def merge_contacts(contacts: List[Dict]) -> Dict:
    """
    Merge multiple contacts into one.
    Strategy:
    - Use best quality name
    - Combine all phones (deduplicated)
    - Combine all emails (deduplicated)
    - Keep most complete company/cargo
    - Combine tags from both contexts
    - Keep photo from either
    """
    if not contacts:
        return {}

    if len(contacts) == 1:
        return contacts[0]

    # Find best name
    best_name = ""
    best_name_score = -1
    for c in contacts:
        name = c.get('nome', '')
        score = get_name_score(name)
        if score > best_name_score:
            best_name_score = score
            best_name = name

    # If best name is still poor quality, normalize it
    if is_all_caps(best_name) or is_all_lower(best_name):
        best_name = normalize_name(best_name)

    # Collect all phones
    all_phones = {}
    for c in contacts:
        phones = c.get('telefones', [])
        if isinstance(phones, str):
            try:
                phones = json.loads(phones)
            except:
                phones = []

        for phone_obj in phones:
            if isinstance(phone_obj, dict):
                number = phone_obj.get('number', '')
                normalized = normalize_phone(number)
                if normalized and normalized not in all_phones:
                    all_phones[normalized] = phone_obj

    # Collect all emails
    all_emails = {}
    for c in contacts:
        emails = c.get('emails', [])
        if isinstance(emails, str):
            try:
                emails = json.loads(emails)
            except:
                emails = []

        for email_obj in emails:
            if isinstance(email_obj, dict):
                email = email_obj.get('email', '').lower().strip()
                if email and email not in all_emails:
                    all_emails[email] = email_obj

    # Find best company/cargo
    best_empresa = ""
    best_cargo = ""
    for c in contacts:
        empresa = c.get('empresa', '') or ''
        cargo = c.get('cargo', '') or ''
        if len(empresa) > len(best_empresa):
            best_empresa = empresa
        if len(cargo) > len(best_cargo):
            best_cargo = cargo

    # Collect contexts (tags)
    contexts = set()
    for c in contacts:
        ctx = c.get('contexto', '')
        if ctx:
            contexts.add(ctx)

    # Find photo
    foto_url = None
    for c in contacts:
        if c.get('foto_url'):
            foto_url = c['foto_url']
            break

    # Find LinkedIn
    linkedin = None
    for c in contacts:
        if c.get('linkedin'):
            linkedin = c['linkedin']
            break

    # Use first contact as base for IDs
    base = contacts[0]

    # ⚠️ O SOBREVIVENTE HERDA OS resourceName DE TODAS AS FICHAS (fix 22/08/26).
    #
    # As duplicatas típicas desta base são TRÊS fichas da mesma pessoa: uma da
    # conta pessoal, uma da profissional, e uma legada sem vínculo. Até aqui o
    # merge levava só o `google_contact_id` escalar do `base` — ou seja, o
    # sobrevivente saía conhecendo UMA conta. Na outra, `propagate_contact_to_google`
    # não achava alvo e caía em `_achar_ou_criar`; e como a ficha real daquela
    # conta pertence a um contato absorvido, ela estava na lista de `condenados`
    # e a busca era PROIBIDA de encontrá-la. Resultado: apagava a ficha boa e
    # criava outra na mesma conta, com resourceName novo. É o "2 apagadas, 4
    # criadas" da Bettina Berman.
    #
    # Unir os mapas conserta a causa: o sobrevivente passa a conhecer uma ficha
    # por conta e a ATUALIZA em vez de recriar. Quem garante que a ficha adotada
    # não é apagada em seguida é `propagate_merge_to_google` (ver `adotados` lá).
    from services.contact_identity import (
        GOOGLE_IDS_COLUMN, GOOGLE_IDS_KEY, _as_dict, google_ids_map,
    )
    gids_unidos: Dict[str, List[str]] = {}
    for c in contacts:
        for conta, gids in google_ids_map(c).items():
            atual = gids_unidos.setdefault(conta, [])
            for g in gids:
                if g not in atual:          # ordem estável: o do `base` vem antes
                    atual.append(g)
    empresa_dados = dict(_as_dict(base.get(GOOGLE_IDS_COLUMN)))
    if gids_unidos:
        empresa_dados[GOOGLE_IDS_KEY] = gids_unidos

    return {
        'id': base.get('id'),
        'nome': best_name,
        'empresa': best_empresa,
        'cargo': best_cargo,
        'emails': list(all_emails.values()),
        'telefones': list(all_phones.values()),
        'foto_url': foto_url,
        'linkedin': linkedin,
        'contexto': ','.join(sorted(contexts)) if len(contexts) > 1 else (list(contexts)[0] if contexts else ''),
        'google_contact_id': base.get('google_contact_id'),
        # `origem` viaja junto porque é o fallback de conta do contato
        # pré-cascata, tanto no update quanto na deleção.
        'origem': base.get('origem'),
        GOOGLE_IDS_COLUMN: empresa_dados or None,
        'merged_from': [c.get('id') for c in contacts],
        'original_contacts': contacts
    }


# ============== Batch Analysis ==============

def analyze_contacts(contacts: List[Dict]) -> Dict[str, Any]:
    """
    Analyze contacts for issues:
    - Duplicates
    - Names needing normalization
    - Missing phone/email
    - Quality issues
    """
    issues = {
        'duplicates': [],
        'caps_lock_names': [],
        'lowercase_names': [],
        'no_phone': [],
        'no_email': [],
        'no_name': [],
        'total_contacts': len(contacts),
        'issues_count': 0
    }

    # Find duplicates
    duplicates = find_duplicates(contacts)
    for key, dup_contacts in duplicates.items():
        merged = merge_contacts(dup_contacts)
        issues['duplicates'].append({
            'key': key,
            'contacts': dup_contacts,
            'suggested_merge': merged
        })

    # Find name issues
    for contact in contacts:
        name = contact.get('nome', '')

        if not name or not name.strip():
            issues['no_name'].append(contact)
        elif is_all_caps(name):
            issues['caps_lock_names'].append({
                'contact': contact,
                'original': name,
                'suggested': normalize_name(name)
            })
        elif is_all_lower(name):
            issues['lowercase_names'].append({
                'contact': contact,
                'original': name,
                'suggested': normalize_name(name)
            })

        # Check for phone
        phones = contact.get('telefones', [])
        if isinstance(phones, str):
            try:
                phones = json.loads(phones)
            except:
                phones = []
        if not phones:
            issues['no_phone'].append(contact)

        # Check for email
        emails = contact.get('emails', [])
        if isinstance(emails, str):
            try:
                emails = json.loads(emails)
            except:
                emails = []
        if not emails:
            issues['no_email'].append(contact)

    # Count total issues
    issues['issues_count'] = (
        len(issues['duplicates']) +
        len(issues['caps_lock_names']) +
        len(issues['lowercase_names']) +
        len(issues['no_name'])
    )

    return issues


def apply_name_fixes(contacts: List[Dict], db_connection) -> Dict[str, int]:
    """
    Apply name normalizations to contacts in database.
    Returns: {fixed: count, skipped: count}
    """
    cursor = db_connection.cursor()
    stats = {'fixed': 0, 'skipped': 0}

    for contact in contacts:
        name = contact.get('nome', '')
        if not name:
            continue

        if is_all_caps(name) or is_all_lower(name):
            new_name = normalize_name(name)
            if new_name != name:
                cursor.execute(
                    "UPDATE contacts SET nome = %s, atualizado_em = CURRENT_TIMESTAMP WHERE id = %s",
                    (new_name, contact['id'])
                )
                stats['fixed'] += 1
            else:
                stats['skipped'] += 1

    db_connection.commit()
    return stats


def merge_duplicate_contacts(duplicate_group: List[Dict], db_connection) -> Dict:
    """
    Merge a group of duplicate contacts in the database.
    Keeps the first contact, updates it with merged data, deletes others.
    """
    if len(duplicate_group) < 2:
        return {'status': 'skipped', 'reason': 'need at least 2 contacts to merge'}

    merged = merge_contacts(duplicate_group)
    primary_id = merged['id']
    other_ids = [c['id'] for c in duplicate_group if c['id'] != primary_id]

    cursor = db_connection.cursor()

    # Update primary contact
    cursor.execute('''
        UPDATE contacts SET
            nome = %s,
            empresa = %s,
            cargo = %s,
            emails = %s,
            telefones = %s,
            foto_url = COALESCE(%s, foto_url),
            linkedin = COALESCE(%s, linkedin),
            contexto = %s,
            empresa_dados = COALESCE(%s::jsonb, empresa_dados),
            atualizado_em = CURRENT_TIMESTAMP
        WHERE id = %s
    ''', (
        merged['nome'],
        merged['empresa'],
        merged['cargo'],
        json.dumps(merged['emails']),
        json.dumps(merged['telefones']),
        merged['foto_url'],
        merged['linkedin'],
        merged['contexto'],
        # O mapa unido de resourceName tem que ser GRAVADO (fix 22/08/26), não
        # só usado em memória: sem isto o sobrevivente volta pro banco
        # conhecendo UMA conta do Google, e o merge ou o sync seguinte repete o
        # create que o conserto veio fechar. COALESCE porque `merge_contacts`
        # devolve None quando nenhuma ficha do grupo tinha mapa — e aí não se
        # apaga o que já estava na coluna.
        json.dumps(merged.get('empresa_dados')) if merged.get('empresa_dados') else None,
        primary_id
    ))

    # Repontar referencias dos secundarios pro primario ANTES de deletar
    # (sem isso, FKs CASCADE apagam historico irrecuperavel)
    _migrate_contact_references(cursor, primary_id, other_ids)

    # Delete other contacts
    if other_ids:
        cursor.execute(
            "DELETE FROM contacts WHERE id = ANY(%s)",
            (other_ids,)
        )

    db_connection.commit()

    return {
        'status': 'merged',
        'primary_id': primary_id,
        'deleted_ids': other_ids,
        'merged_contact': merged
    }


# ============== Google Account Propagation ==============

async def get_google_accounts(db_connection) -> List[Dict]:
    """Get all connected Google accounts with their tokens"""
    cursor = db_connection.cursor()
    cursor.execute('''
        SELECT id, email, tipo, access_token, refresh_token, sync_token
        FROM google_accounts
        WHERE conectado = true
    ''')
    rows = cursor.fetchall()
    return [dict(row) for row in rows]


async def propagate_contact_to_google(
    contact_data: Dict,
    db_connection,
    google_contacts_module,
    ignorar_rids: Optional[set] = None,
    contas_permitidas: Optional[set] = None,
) -> Dict[str, Any]:
    """
    Propagate a contact update to all connected Google accounts.

    `contas_permitidas`: quando dado, restringe a propagação a essas contas.
    None (default) = comportamento de sempre, todas as conectadas. Só o merge
    passa — ver o comentário no loop.

    If contact has google_contact_id from one account:
    - Update that account
    - Create in other account if contact is marked as 'both' or has both contexts

    Returns: {account_email: {status, google_id}}
    """
    results = {}

    # Get all Google accounts
    accounts = await get_google_accounts(db_connection)

    if not accounts:
        return {'error': 'No Google accounts connected'}

    contexto = contact_data.get('contexto', '')
    existing_google_id = contact_data.get('google_contact_id')
    # Mapa {conta: resourceName} — a mesma pessoa tem um id DIFERENTE em cada
    # conta Google, e a coluna escalar so guarda um deles.
    from services.contact_identity import google_ids_map
    ids_map = google_ids_map(contact_data)

    # Determine which accounts should have this contact
    # If contexto contains both 'personal' and 'professional', sync to both
    should_sync_personal = 'personal' in contexto or contexto == 'both'
    should_sync_professional = 'professional' in contexto or contexto == 'both'

    # If only one context, still sync to that account
    if not should_sync_personal and not should_sync_professional:
        should_sync_personal = True
        should_sync_professional = True

    for account in accounts:
        account_email = account['email']
        # MERGE NÃO INVENTA PRESENÇA (fix 22/08/26). `contas_permitidas` vem
        # preenchido só pelo caminho do merge, com as contas onde o grupo JÁ
        # tinha ficha. Sem isso o fallback acima ("não sei o contexto → manda pra
        # todas") fazia o merge CRIAR a pessoa numa agenda onde ela nunca esteve:
        # 3 dos 120 grupos do ensaio eram só isso — gente que existe apenas na
        # conta pessoal ganhando ficha nova na profissional. Espalhar contato
        # entre agendas é trabalho do sync, não de quem veio desduplicar.
        # Os outros chamadores passam None e mantêm o comportamento de sempre.
        if contas_permitidas is not None and account_email not in contas_permitidas:
            continue
        access_token = account['access_token']
        refresh_token = account['refresh_token']
        account_tipo = account['tipo']  # 'personal' or 'professional'

        # Check if we should sync to this account
        if account_tipo == 'personal' and not should_sync_personal:
            continue
        if account_tipo == 'professional' and not should_sync_professional:
            continue

        try:
            # Refresh token if needed
            try:
                new_tokens = await google_contacts_module.refresh_access_token(refresh_token)
                access_token = new_tokens['access_token']

                # Update token in database
                cursor = db_connection.cursor()
                cursor.execute('''
                    UPDATE google_accounts
                    SET access_token = %s
                    WHERE email = %s
                ''', (access_token, account_email))
                db_connection.commit()
            except:
                pass  # Token might still be valid

            # Qual resourceName vale NESTA conta. O `google_contact_id` escalar
            # pertence a UMA conta — usa-lo na outra fazia o update falhar e o
            # codigo cair no create, ou seja: a propagacao CRIAVA duplicata no
            # Google, o oposto do objetivo. O mapa {conta: resourceName}
            # (contacts.empresa_dados._google_contact_ids) resolve por conta.
            # ids_map agora e {conta: [gids]}. Se a mesma agenda tem 2 fichas
            # desta pessoa, atualiza a PRIMEIRA (ordem estavel). As demais sao o
            # passivo de duplicata do lado do Google, que o Renato decidiu em
            # 27/07 nao limpar agora — atualizar todas aqui equivaleria a uma
            # limpeza implicita, fora do escopo desta propagacao.
            _gids_conta = ids_map.get(account_email) or []
            account_gid = _gids_conta[0] if _gids_conta else None
            if not account_gid and len(ids_map) == 0:
                # Contato pre-cascata: nao tem mapa ainda. Cai no escalar SO se
                # a conta bate com a origem registrada — senao trata como
                # inexistente nesta conta (create), que e o correto.
                if (contact_data.get("origem") or "") == f"google_{account_email}":
                    account_gid = existing_google_id
            existing_google_id_for_account = account_gid

            if existing_google_id_for_account:
                # Try to update existing contact
                success = await google_contacts_module.update_google_contact(
                    access_token,
                    existing_google_id_for_account,
                    contact_data
                )
                if success:
                    results[account_email] = {
                        'status': 'updated',
                        'google_id': existing_google_id_for_account,
                    }
                else:
                    # Contact might not exist in this account, create it
                    results[account_email] = await _achar_ou_criar(
                        google_contacts_module, access_token, contact_data,
                        ignorar_rids)
            else:
                results[account_email] = await _achar_ou_criar(
                    google_contacts_module, access_token, contact_data,
                    ignorar_rids)

        except Exception as e:
            results[account_email] = {'status': 'error', 'error': str(e)}

    return results


async def propagate_name_fix_to_google(
    contact_id: int,
    new_name: str,
    db_connection,
    google_contacts_module
) -> Dict[str, Any]:
    """
    Propagate a name fix to Google accounts.
    """
    cursor = db_connection.cursor()

    # Get contact details
    cursor.execute('''
        SELECT id, nome, empresa, cargo, emails, telefones, google_contact_id, contexto
        FROM contacts WHERE id = %s
    ''', (contact_id,))
    row = cursor.fetchone()

    if not row:
        return {'error': 'Contact not found'}

    contact_data = dict(row)
    contact_data['nome'] = new_name

    # Parse JSON fields
    if isinstance(contact_data.get('emails'), str):
        contact_data['emails'] = json.loads(contact_data['emails'])
    if isinstance(contact_data.get('telefones'), str):
        contact_data['telefones'] = json.loads(contact_data['telefones'])

    return await propagate_contact_to_google(
        contact_data,
        db_connection,
        google_contacts_module
    )


async def propagate_merge_to_google(
    merged_contact: Dict,
    deleted_contacts: List[Dict],
    db_connection,
    google_contacts_module
) -> Dict[str, Any]:
    """
    Propagate a merge operation to Google:
    - Update the surviving contact
    - Delete the removed contacts
    """
    results = {'updates': {}, 'deletions': {}}

    # Os ids que serão apagados logo abaixo, calculados ANTES do update: a busca
    # do `_achar_ou_criar` casa por telefone, e o telefone das fichas absorvidas
    # é o mesmo — sem esta lista, o update escreveria na ficha condenada.
    from services.contact_identity import google_ids_map as _gids
    condenados = set()
    for _d in deleted_contacts:
        for _gs in _gids(_d).values():
            condenados.update(_gs)
        if _d.get('google_contact_id'):
            condenados.add(_d['google_contact_id'])

    # ⚠️ A FICHA ADOTADA NÃO PODE SER APAGADA (fix 22/08/26).
    #
    # Desde que `merge_contacts` faz o sobrevivente herdar os resourceName de
    # todas as fichas do grupo, o alvo do update em cada conta costuma ser um id
    # que veio de uma ficha ABSORVIDA — e portanto está em `condenados`. Sem esta
    # subtração o merge atualizaria a ficha e a apagaria no passo seguinte da
    # mesma função: a pessoa sumiria daquela conta do Google. Pior que o defeito
    # que o conserto veio fechar.
    #
    # `propagate_contact_to_google` usa o PRIMEIRO gid de cada conta — a mesma
    # ordem estável de `merge_contacts`. Sobrevive uma ficha por conta; as demais
    # continuam condenadas, que é o trabalho do mutirão.
    adotados = {gids[0] for gids in _gids(merged_contact).values() if gids}
    if merged_contact.get('google_contact_id'):
        adotados.add(merged_contact['google_contact_id'])
    condenados -= adotados

    # As contas onde este grupo JÁ tinha ficha. O merge propaga só pra elas: ver
    # `contas_permitidas` em `propagate_contact_to_google`. `origem` cobre o
    # contato pré-cascata, que não tem mapa.
    contas_presentes = set(_gids(merged_contact))
    for _c in [merged_contact] + list(deleted_contacts):
        contas_presentes.update(_gids(_c))
        _o = (_c.get('origem') or '')
        if _o.startswith('google_'):
            contas_presentes.add(_o[len('google_'):])

    # Update the merged contact in Google
    update_results = await propagate_contact_to_google(
        merged_contact,
        db_connection,
        google_contacts_module,
        ignorar_rids=condenados,
        # Grupo sem NENHUM vínculo conhecido: não restringe (None), senão o
        # merge de dois contatos que só existem no INTEL não propagaria nada e
        # a mudança sairia calada.
        contas_permitidas=contas_presentes or None,
    )
    results['updates'] = update_results

    # Apaga no Google as fichas absorvidas — cada id NA SUA conta.
    #
    # Antes: loop `for account in accounts` tentando o MESMO id em todas. Como
    # resourceName e por conta, a tentativa na conta errada dava 404 (ruido que
    # esconde erro real) e, pior, a funcao lia so `google_contact_id` (escalar)
    # — um contato com ficha nas duas contas tem DOIS ids e o outro nunca era
    # apagado, deixando a duplicata viva no Google. Como o sync completo traz de
    # volta o que existe no Google, essa metade esquecida RECRIAVA a duplicata
    # no INTEL no dia seguinte (medido 26/07: Manuela e Wanelise voltaram).
    from services.contact_identity import google_ids_map

    accounts = await get_google_accounts(db_connection)
    tokens_por_conta = {a['email']: a['access_token'] for a in accounts}

    for deleted in deleted_contacts:
        # Todos os ids desta ficha, por conta: o mapa novo + o escalar legado
        # (contato pre-cascata, cuja conta de origem vem em `origem`).
        # {conta: [gids]} — a ficha pode ter 2 resourceNames na MESMA agenda.
        # Aqui a ficha do INTEL foi apagada, entao TODAS as representacoes dela
        # no Google devem sair junto; deletar so a primeira deixaria a outra
        # viva e o proximo sync completo a traria de volta — exatamente o
        # mecanismo de recriacao que esta funcao existe pra fechar.
        por_conta = {acc: list(gids) for acc, gids in google_ids_map(deleted).items()}
        escalar = deleted.get('google_contact_id')
        _todos_gids = [g for gids in por_conta.values() for g in gids]
        if escalar and escalar not in _todos_gids:
            origem = (deleted.get('origem') or '')
            conta_origem = origem[len('google_'):] if origem.startswith('google_') else None
            if conta_origem in tokens_por_conta:
                por_conta.setdefault(conta_origem, []).append(escalar)
            elif len(tokens_por_conta) == 1:
                # Uma conta conectada: nao ha ambiguidade.
                por_conta.setdefault(next(iter(tokens_por_conta)), []).append(escalar)
            else:
                # Sem como saber a conta — NAO chuta (apagar na errada e
                # irreversivel do lado do Google).
                results['deletions'][escalar] = {
                    'skipped': 'conta_indeterminada',
                    'contact_id': deleted.get('id'),
                }

        for account_email, google_ids in por_conta.items():
            access_token = tokens_por_conta.get(account_email)
            for google_id in google_ids:
                # A ficha que o sobrevivente adotou nesta conta NÃO se apaga —
                # ela acabou de receber o update. Sem esta guarda o merge
                # atualizaria e apagaria o mesmo resourceName, e a pessoa sumiria
                # daquela agenda. `condenados` sozinho não bastava: ele governa
                # só a BUSCA do `_achar_ou_criar`, não este loop.
                if google_id in adotados:
                    results['deletions'][google_id] = {
                        'account': account_email,
                        'skipped': 'adotada_pelo_sobrevivente',
                        'contact_id': deleted.get('id'),
                    }
                    continue
                if not access_token:
                    results['deletions'][google_id] = {
                        'account': account_email,
                        'skipped': 'conta_nao_conectada',
                    }
                    continue
                try:
                    success = await google_contacts_module.delete_google_contact(
                        access_token,
                        google_id
                    )
                    results['deletions'][google_id] = {
                        'account': account_email,
                        'success': success
                    }
                except Exception as e:
                    results['deletions'][google_id] = {
                        'account': account_email,
                        'error': str(e)
                    }

    return results


async def apply_name_fixes_with_propagation(
    contacts: List[Dict],
    db_connection,
    google_contacts_module
) -> Dict[str, Any]:
    """
    Apply name normalizations and propagate to Google accounts.
    """
    cursor = db_connection.cursor()
    stats = {'fixed': 0, 'skipped': 0, 'google_updates': 0, 'google_errors': 0}

    for contact in contacts:
        name = contact.get('nome', '')
        if not name:
            continue

        if is_all_caps(name) or is_all_lower(name):
            new_name = normalize_name(name)
            if new_name != name:
                # Update local database
                cursor.execute(
                    "UPDATE contacts SET nome = %s, atualizado_em = CURRENT_TIMESTAMP WHERE id = %s",
                    (new_name, contact['id'])
                )
                stats['fixed'] += 1

                # Propagate to Google
                try:
                    google_results = await propagate_name_fix_to_google(
                        contact['id'],
                        new_name,
                        db_connection,
                        google_contacts_module
                    )
                    for account, result in google_results.items():
                        if result.get('status') in ['updated', 'created']:
                            stats['google_updates'] += 1
                        elif result.get('status') == 'error':
                            stats['google_errors'] += 1
                except Exception as e:
                    stats['google_errors'] += 1
            else:
                stats['skipped'] += 1

    db_connection.commit()
    return stats


async def merge_duplicate_contacts_with_propagation(
    duplicate_group: List[Dict],
    db_connection,
    google_contacts_module
) -> Dict[str, Any]:
    """
    Merge duplicate contacts and propagate changes to Google accounts.
    """
    if len(duplicate_group) < 2:
        return {'status': 'skipped', 'reason': 'need at least 2 contacts to merge'}

    merged = merge_contacts(duplicate_group)
    primary_id = merged['id']
    other_ids = [c['id'] for c in duplicate_group if c['id'] != primary_id]
    deleted_contacts = [c for c in duplicate_group if c['id'] != primary_id]

    cursor = db_connection.cursor()

    # Update primary contact in local DB
    cursor.execute('''
        UPDATE contacts SET
            nome = %s,
            empresa = %s,
            cargo = %s,
            emails = %s,
            telefones = %s,
            foto_url = COALESCE(%s, foto_url),
            linkedin = COALESCE(%s, linkedin),
            contexto = %s,
            empresa_dados = COALESCE(%s::jsonb, empresa_dados),
            atualizado_em = CURRENT_TIMESTAMP
        WHERE id = %s
    ''', (
        merged['nome'],
        merged['empresa'],
        merged['cargo'],
        json.dumps(merged['emails']),
        json.dumps(merged['telefones']),
        merged['foto_url'],
        merged['linkedin'],
        merged['contexto'],
        # O mapa unido de resourceName tem que ser GRAVADO (fix 22/08/26), não
        # só usado em memória: sem isto o sobrevivente volta pro banco
        # conhecendo UMA conta do Google, e o merge ou o sync seguinte repete o
        # create que o conserto veio fechar. COALESCE porque `merge_contacts`
        # devolve None quando nenhuma ficha do grupo tinha mapa — e aí não se
        # apaga o que já estava na coluna.
        json.dumps(merged.get('empresa_dados')) if merged.get('empresa_dados') else None,
        primary_id
    ))

    # Repontar referencias dos secundarios pro primario ANTES de deletar
    # (sem isso, FKs CASCADE apagam historico irrecuperavel)
    _migrate_contact_references(cursor, primary_id, other_ids)

    # Delete other contacts from local DB
    if other_ids:
        cursor.execute(
            "DELETE FROM contacts WHERE id = ANY(%s)",
            (other_ids,)
        )

    db_connection.commit()

    # Propagate to Google accounts
    google_results = await propagate_merge_to_google(
        merged,
        deleted_contacts,
        db_connection,
        google_contacts_module
    )

    return {
        'status': 'merged',
        'primary_id': primary_id,
        'deleted_ids': other_ids,
        'merged_contact': merged,
        'google_propagation': google_results
    }
