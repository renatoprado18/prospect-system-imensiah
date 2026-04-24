"""
Generate Vallen Clinic April 2026 Board Meeting Ata

Reads the transcription from ConselhoOS, uses Claude to generate the ata
following the exact format of the March 11 model ata, then saves it back.

Usage:
    cd /Users/rap/prospect-system
    python scripts/generate_vallen_ata.py
"""
import os
import sys
import json
import httpx
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime

# Add app dir to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

# Load .env
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CONSELHOOS_DATABASE_URL = os.getenv("CONSELHOOS_DATABASE_URL", "")
CLAUDE_MODEL = "claude-sonnet-4-20250514"

# INTEL database for checking messages
DATABASE_URL = os.getenv("LOCAL_DATABASE_URL") or os.getenv("DATABASE_URL", "")


def get_conselhoos_conn():
    """Connect to ConselhoOS database."""
    if not CONSELHOOS_DATABASE_URL:
        raise ValueError("CONSELHOOS_DATABASE_URL not set in .env")
    return psycopg2.connect(CONSELHOOS_DATABASE_URL, cursor_factory=RealDictCursor)


def get_intel_conn():
    """Connect to INTEL database."""
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL not set in .env")
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def read_model_ata():
    """Read the model ata from /tmp/ata_vallen_modelo.txt."""
    model_path = "/tmp/ata_vallen_modelo.txt"
    if not os.path.exists(model_path):
        raise FileNotFoundError(
            f"Model ata not found at {model_path}. "
            "Copy the March 11 ata there first."
        )
    with open(model_path, "r", encoding="utf-8") as f:
        return f.read()


def fetch_april_meeting():
    """Fetch the April 8 Vallen meeting from ConselhoOS."""
    conn = get_conselhoos_conn()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT r.id, r.titulo, r.data, r.status, r.transcricao,
                   r.transcricao_resumo, r.ata_md, r.pauta_md,
                   e.nome as empresa_nome
            FROM reunioes r
            JOIN empresas e ON e.id = r.empresa_id
            WHERE r.titulo ILIKE '%vallen%'
              AND r.data::date = '2026-04-08'
            LIMIT 1
        """)
        row = cursor.fetchone()
        if not row:
            raise ValueError("April 8 Vallen meeting not found in ConselhoOS")
        return dict(row)
    finally:
        conn.close()


def fetch_raci_items(empresa_id: str):
    """Fetch current RACI items for context."""
    conn = get_conselhoos_conn()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT area, acao, prazo, status, responsavel_r
            FROM raci_itens
            WHERE empresa_id = %s
            ORDER BY prazo ASC NULLS LAST
        """, (empresa_id,))
        return [dict(r) for r in cursor.fetchall()]
    except Exception:
        return []
    finally:
        conn.close()


def fetch_previous_decisions(empresa_id: str):
    """Fetch recent decisions for context."""
    conn = get_conselhoos_conn()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT d.decisao, d.area, r.data
            FROM decisoes d
            JOIN reunioes r ON r.id = d.reuniao_id
            WHERE d.empresa_id = %s
            ORDER BY r.data DESC
            LIMIT 30
        """, (empresa_id,))
        return [dict(r) for r in cursor.fetchall()]
    except Exception:
        return []
    finally:
        conn.close()


def generate_ata_with_claude(transcription: str, model_ata: str,
                              meeting_data: dict, raci_items: list,
                              previous_decisions: list) -> str:
    """Send transcription to Claude to generate the ata."""
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY not set")

    meeting_date = meeting_data.get("data", "")
    if hasattr(meeting_date, "strftime"):
        meeting_date_str = meeting_date.strftime("%d de %B de %Y").replace(
            "January", "Janeiro").replace("February", "Fevereiro").replace(
            "March", "Marco").replace("April", "Abril").replace(
            "May", "Maio").replace("June", "Junho").replace(
            "July", "Julho").replace("August", "Agosto").replace(
            "September", "Setembro").replace("October", "Outubro").replace(
            "November", "Novembro").replace("December", "Dezembro")
    else:
        meeting_date_str = "08 de Abril de 2026"

    pauta = meeting_data.get("pauta_md", "") or ""

    # Format RACI context
    raci_text = ""
    if raci_items:
        raci_lines = []
        for item in raci_items:
            raci_lines.append(
                f"- [{item.get('area', '?')}] {item.get('acao', '?')} | "
                f"Prazo: {item.get('prazo', '?')} | Status: {item.get('status', '?')} | "
                f"Resp: {item.get('responsavel_r', '?')}"
            )
        raci_text = "\n".join(raci_lines)

    # Format previous decisions context
    decisions_text = ""
    if previous_decisions:
        dec_lines = []
        for d in previous_decisions[:15]:
            dec_lines.append(f"- [{d.get('area', '?')}] {d.get('decisao', '?')}")
        decisions_text = "\n".join(dec_lines)

    system_prompt = f"""Voce e um redator especializado em atas de reunioes de conselho de administracao.
Sua tarefa e gerar a ata da reuniao do conselho da Vallen Clinic de {meeting_date_str},
a partir da transcricao integral da reuniao.

FORMATO OBRIGATORIO - Siga EXATAMENTE o formato da ata modelo abaixo. A estrutura, o tom,
o nivel de detalhe e a organizacao devem ser identicos. Adapte apenas o conteudo para o que
foi discutido na reuniao de abril.

=== ATA MODELO (Reuniao de Março 2026 - USE COMO REFERENCIA DE FORMATO) ===
{model_ata}
=== FIM DA ATA MODELO ===

REGRAS CRITICAS:
1. Mantenha EXATAMENTE a mesma estrutura: header, metadata, participantes, secoes numeradas, tabelas financeiras, matriz RACI, pendencias, proximos passos, encerramento
2. O header aparece UMA VEZ no inicio. NAO repita headers nem footers ao longo do documento.
3. Use secoes numeradas como na ata modelo
4. Dentro de cada secao: "O que foi apresentado", "Diagnostico/Gaps", "Decisoes Tomadas"
5. TODAS as tabelas (financeiras, RACI, comparativas) devem usar formato PIPE do markdown:
   | Coluna1 | Coluna2 | Coluna3 |
   |---------|---------|---------|
   | valor1  | valor2  | valor3  |
6. A MATRIZ RACI DEVE ser uma tabela pipe com colunas: Area, Acao/Entrega, Prazo, e uma coluna por pessoa (Thalita, Gui, Amadeo, Renata, Verid., Lara). Valores: R, A, C, I ou vazio.
7. Secao de PENDENCIAS com emojis: 🔴 CRITICO, 🟡 IMPORTANTE, 🟢 GOVERNANCA
8. PROXIMOS PASSOS IMEDIATOS como tabela pipe: | Acao | Responsavel |
9. NUNCA invente dados. Se algo nao ficou claro na transcricao, registre como "a ser confirmado"
10. Mantenha o tom formal mas acessivel da ata modelo
11. NAO inclua linhas tipo "Proxima reuniao de conselho: X | Confidencial" repetidas. Isso so aparece UMA VEZ no final.
12. NAO inclua o titulo "VALLEN CLINIC | Ata do Conselho" repetido. So aparece UMA VEZ no inicio.

CONTEXTO ADICIONAL:
- Pauta prevista: {pauta[:2000] if pauta else 'Nao disponivel'}
- RACI itens anteriores (para acompanhamento): {raci_text[:2000] if raci_text else 'Nenhum'}
- Decisoes anteriores: {decisions_text[:1500] if decisions_text else 'Nenhuma'}

Gere a ata completa em markdown. NAO inclua blocos de codigo (```). Escreva direto o texto da ata."""

    # Use extended thinking or large context for the long transcription
    print(f"  Sending to Claude ({CLAUDE_MODEL})...")
    print(f"  Transcription length: {len(transcription)} chars")

    with httpx.Client(timeout=120.0) as client:
        response = client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 8000,
                "system": system_prompt,
                "messages": [
                    {
                        "role": "user",
                        "content": f"Aqui esta a transcricao integral da reuniao de conselho da Vallen Clinic de {meeting_date_str}. Gere a ata completa seguindo EXATAMENTE o formato da ata modelo.\n\nTRANSCRICAO:\n{transcription}"
                    }
                ],
            },
        )

        if response.status_code != 200:
            raise Exception(f"Claude API error: {response.status_code} - {response.text[:500]}")

        result = response.json()
        content = result.get("content", [])
        text_parts = [block["text"] for block in content if block.get("type") == "text"]
        return "\n".join(text_parts).strip()


def save_ata_to_conselhoos(reuniao_id: str, ata_md: str):
    """Save the generated ata back to ConselhoOS."""
    conn = get_conselhoos_conn()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE reunioes SET ata_md = %s WHERE id = %s
            RETURNING id, titulo
        """, (ata_md, reuniao_id))
        result = cursor.fetchone()
        conn.commit()
        if result:
            print(f"  Ata saved to ConselhoOS: {result['titulo']}")
        else:
            print(f"  WARNING: reuniao {reuniao_id} not found for update")
    finally:
        conn.close()


def check_ata_sent_to_contacts():
    """Check if the ata was sent to Thalita or Viviane via INTEL messages."""
    try:
        conn = get_intel_conn()
        try:
            cursor = conn.cursor()
            # Check messages to Thalita (contact_id 5715)
            cursor.execute("""
                SELECT m.conteudo, m.enviado_em, m.direcao
                FROM messages m
                JOIN conversations cv ON cv.id = m.conversation_id
                WHERE cv.contact_id IN (5715, 6048)
                  AND m.conteudo ILIKE '%ata%'
                ORDER BY m.enviado_em DESC
                LIMIT 10
            """)
            rows = [dict(r) for r in cursor.fetchall()]

            if rows:
                print("\n  Messages mentioning 'ata' found for Thalita/Viviane:")
                for r in rows:
                    direction = "SENT" if r["direcao"] == "outgoing" else "RECEIVED"
                    date_str = r["enviado_em"].strftime("%Y-%m-%d %H:%M") if r["enviado_em"] else "?"
                    content_preview = str(r["conteudo"])[:120]
                    print(f"    [{direction}] {date_str}: {content_preview}")
            else:
                print("\n  No messages mentioning 'ata' found for Thalita (5715) or Viviane (6048)")

            # Also check contact names for context
            cursor.execute("""
                SELECT id, nome FROM contacts WHERE id IN (5715, 6048)
            """)
            contacts = [dict(r) for r in cursor.fetchall()]
            for c in contacts:
                print(f"    Contact #{c['id']}: {c['nome']}")

        finally:
            conn.close()
    except Exception as e:
        print(f"  Could not check INTEL messages: {e}")


def main():
    print("=" * 60)
    print("VALLEN CLINIC - Ata Generation (April 8, 2026)")
    print("=" * 60)

    # Step 1: Read model ata
    print("\n[1/5] Reading model ata...")
    model_ata = read_model_ata()
    print(f"  Model ata loaded: {len(model_ata)} chars")

    # Step 2: Fetch April meeting from ConselhoOS
    print("\n[2/5] Fetching April 8 meeting from ConselhoOS...")
    meeting = fetch_april_meeting()
    print(f"  Meeting: {meeting['titulo']}")
    print(f"  Date: {meeting['data']}")
    print(f"  Status: {meeting['status']}")
    print(f"  Transcription: {len(meeting.get('transcricao', '') or '')} chars")
    print(f"  Existing ata: {len(meeting.get('ata_md', '') or '')} chars")

    transcription = meeting.get("transcricao", "")
    if not transcription:
        print("  ERROR: No transcription found for this meeting!")
        sys.exit(1)

    # Get empresa_id for RACI/decisions context
    conn = get_conselhoos_conn()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT empresa_id FROM reunioes WHERE id = %s
        """, (meeting["id"],))
        empresa_row = cursor.fetchone()
        empresa_id = empresa_row["empresa_id"] if empresa_row else None
    finally:
        conn.close()

    # Fetch RACI and decisions for context
    raci_items = fetch_raci_items(empresa_id) if empresa_id else []
    previous_decisions = fetch_previous_decisions(empresa_id) if empresa_id else []
    print(f"  RACI items for context: {len(raci_items)}")
    print(f"  Previous decisions for context: {len(previous_decisions)}")

    # Step 3: Generate ata with Claude
    print("\n[3/5] Generating ata with Claude...")
    ata_md = generate_ata_with_claude(
        transcription=transcription,
        model_ata=model_ata,
        meeting_data=meeting,
        raci_items=raci_items,
        previous_decisions=previous_decisions,
    )
    print(f"  Generated ata: {len(ata_md)} chars")

    # Preview first 500 chars
    print(f"\n  --- ATA PREVIEW ---")
    print(f"  {ata_md[:500]}")
    print(f"  --- END PREVIEW ---")

    # Step 4: Save to ConselhoOS
    print("\n[4/5] Saving ata to ConselhoOS...")
    save_ata_to_conselhoos(meeting["id"], ata_md)

    # Step 5: Check if ata was sent to Thalita/Viviane
    print("\n[5/5] Checking if ata was shared with Thalita/Viviane...")
    check_ata_sent_to_contacts()

    # Save local copy
    local_path = "/tmp/ata_vallen_abril_2026.md"
    with open(local_path, "w", encoding="utf-8") as f:
        f.write(ata_md)
    print(f"\n  Local copy saved to: {local_path}")

    print("\n" + "=" * 60)
    print("DONE!")
    print("=" * 60)


if __name__ == "__main__":
    main()
