"""Backfill options pra propostas linkedin_job_change pendentes sem botoes.

Motivado por feedback Renato 07/05/2026: cards "mudou de empresa" no
dashboard apareciam sem acoes acionaveis. Fix em main.py::step_linkedin_enrichment
adiciona options nas novas, este script repara as ja criadas.

Uso: python3 scripts/backfill_job_change_options.py
     (le DATABASE_URL do .env via load_dotenv)
"""
import json
import os
import re
import sys
from urllib.parse import quote

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

# Permite override pra rodar contra Neon: DATABASE_URL_OVERRIDE=...
db_url = os.getenv("DATABASE_URL_OVERRIDE") or os.getenv("DATABASE_URL")
if not db_url:
    print("ERRO: DATABASE_URL nao definida")
    sys.exit(1)

conn = psycopg2.connect(db_url, cursor_factory=psycopg2.extras.RealDictCursor)
cursor = conn.cursor()

cursor.execute("""
    SELECT ap.id, ap.contact_id, ap.title, ap.action_params,
           c.nome, c.linkedin, c.telefones
    FROM action_proposals ap
    LEFT JOIN contacts c ON c.id = ap.contact_id
    WHERE ap.action_type = 'linkedin_job_change'
      AND ap.status = 'pending'
      AND (ap.options IS NULL OR ap.options::text IN ('[]', 'null'))
""")
rows = cursor.fetchall()
print(f"Encontrei {len(rows)} propostas linkedin_job_change pendentes sem options")

updated = 0
for r in rows:
    contact_id = r["contact_id"]
    nome = r.get("nome") or ""
    linkedin_url = (r.get("linkedin") or "").strip() or None
    tels = r.get("telefones") or []
    params = r.get("action_params") or {}
    jc = params.get("job_change") or {}
    new_company = jc.get("new_company") or ""
    change_type = "mudou de empresa" if jc.get("type") == "job_change" else "foi promovido(a)"

    phone = None
    if isinstance(tels, list) and tels:
        wa_pref = next((t for t in tels if isinstance(t, dict) and t.get("whatsapp")), None)
        tel_obj = wa_pref or tels[0]
        raw = tel_obj.get("number") if isinstance(tel_obj, dict) else None
        if raw:
            digits = re.sub(r"\D", "", raw)
            if digits:
                if not digits.startswith("55") and len(digits) <= 11:
                    digits = "55" + digits
                phone = digits

    options = []
    if contact_id:
        options.append({"id": "view_contact", "label": "Ver contato",
                        "action": "navigate", "url": f"/contatos/{contact_id}"})
    if linkedin_url:
        options.append({"id": "open_linkedin", "label": "LinkedIn",
                        "action": "navigate", "url": linkedin_url})
    if phone and nome:
        msg = f"Oi {nome.split()[0]}, vi que voce {change_type}"
        if new_company:
            msg += f" - agora na {new_company}"
        msg += ". Parabens!"
        options.append({"id": "send_whatsapp", "label": "WhatsApp",
                        "action": "navigate",
                        "url": f"https://wa.me/{phone}?text={quote(msg)}"})
    options.append({"id": "ignore", "label": "Ignorar", "action": "dismiss"})

    cursor.execute(
        "UPDATE action_proposals SET options = %s WHERE id = %s",
        (json.dumps(options), r["id"]),
    )
    updated += 1
    print(f"  #{r['id']} {nome[:40]:40s} -> {len(options)} opcoes")

conn.commit()
print(f"\nOK: atualizei {updated} propostas")
cursor.close()
conn.close()
