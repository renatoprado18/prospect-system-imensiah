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

def find_duplicates(contacts: List[Dict]) -> Dict[str, List[Dict]]:
    """
    Find duplicate contacts by phone number or email.
    Returns: {normalized_key: [contact1, contact2, ...]}
    """
    # Group by normalized phone
    by_phone = defaultdict(list)
    # Group by email
    by_email = defaultdict(list)

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

    return duplicates


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
        primary_id
    ))

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
    google_contacts_module
) -> Dict[str, Any]:
    """
    Propagate a contact update to all connected Google accounts.

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

            # Check if contact exists in this Google account
            # For now, we use google_contact_id which is account-specific
            # In the future, we'd need a mapping table for multi-account IDs

            if existing_google_id:
                # Try to update existing contact
                success = await google_contacts_module.update_google_contact(
                    access_token,
                    existing_google_id,
                    contact_data
                )
                if success:
                    results[account_email] = {'status': 'updated', 'google_id': existing_google_id}
                else:
                    # Contact might not exist in this account, create it
                    new_id = await google_contacts_module.create_google_contact(
                        access_token,
                        contact_data
                    )
                    results[account_email] = {'status': 'created', 'google_id': new_id}
            else:
                # Create new contact
                new_id = await google_contacts_module.create_google_contact(
                    access_token,
                    contact_data
                )
                results[account_email] = {'status': 'created', 'google_id': new_id}

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

    # Update the merged contact in Google
    update_results = await propagate_contact_to_google(
        merged_contact,
        db_connection,
        google_contacts_module
    )
    results['updates'] = update_results

    # Delete the other contacts from Google
    accounts = await get_google_accounts(db_connection)

    for deleted in deleted_contacts:
        google_id = deleted.get('google_contact_id')
        if not google_id:
            continue

        for account in accounts:
            try:
                access_token = account['access_token']
                success = await google_contacts_module.delete_google_contact(
                    access_token,
                    google_id
                )
                results['deletions'][google_id] = {
                    'account': account['email'],
                    'success': success
                }
            except Exception as e:
                results['deletions'][google_id] = {
                    'account': account['email'],
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
        primary_id
    ))

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
