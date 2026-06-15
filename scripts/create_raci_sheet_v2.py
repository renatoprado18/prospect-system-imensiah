"""
Matriz RACI v2 (14-15/06/26) — incorpora:
- Refinamentos humanos: DAP (filho), JP (família), AS expandida, CS pendente
- CONSELHEIRO multi-conselho (4) + CUSTOMER SUCCESS multi-cliente
- Swarm de 9 specialists IA (1 deployed: CONSELHEIRO, 8 pendentes)
- Genspark API como ferramenta canalizadora
- Status de implementação por agente
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

from database import get_db  # noqa
from integrations.google_contacts import refresh_access_token  # noqa


GOOGLE_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
GOOGLE_DRIVE = "https://www.googleapis.com/drive/v3/files"
OLD_SHEET_ID_V1 = "1KnHVHijTd188_tlWqIqZ_xSbPbc1UopAjKd40TgZqQk"


async def get_token_professional() -> str:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, refresh_token FROM google_accounts WHERE tipo='professional' AND conectado=TRUE LIMIT 1"
        )
        row = cur.fetchone()
    tokens = await refresh_access_token(row["refresh_token"])
    return tokens["access_token"]


def build_csv() -> str:
    buf = io.StringIO()
    w = csv.writer(buf)

    w.writerow(["Matriz RACI v2 — Equipe Renato Almeida Prado (15/06/2026)"])
    w.writerow([])
    w.writerow(["v2: incorpora CoS Swarm de 9 specialists IA + ferramentas (Genspark API) + humanos refinados"])
    w.writerow(["Convenção: R = executa | A = decide / responde | C = consulta | I = informa | (vazio) = não envolvido"])
    w.writerow(["🆕 = proposto pendente | 🤖 = agente IA | 👤 = humano | 🔧 = ferramenta"])
    w.writerow([])

    # AGENTES HUMANOS
    w.writerow(["AGENTES HUMANOS"])
    w.writerow(["Código", "Nome", "Status", "Especialidade"])
    humanos = [
        ("RE", "Renato Almeida Prado", "✅ core", "decisão final, conselho, C-level, voz/identidade"),
        ("DAP", "Renato Dansieri (filho)", "✅ atual", "Dev backend SaaS imensIAH (AI Engineer)"),
        ("AS", "Andressa Santos", "✅ atual — papel expandido", "contábil/ECD + qualificação fria SDR + outreach"),
        ("PR", "Priscila", "✅ atual", "regularizações empresas (CNPJ, fiscal)"),
        ("JP", "Dr. João Piccino", "✅ atual (família ad-hoc)", "advisor jurídico estratégico (contratos, NDAs)"),
        ("CS🆕", "Customer Success fracional", "🆕 pendente (gatilho 2º cliente ConselhoOS)", "ops multi-cliente"),
    ]
    for x in humanos:
        w.writerow(list(x))
    w.writerow([])

    # AGENTES IA — SWARM
    w.writerow(["AGENTES IA — CoS Swarm (visão 14/06)"])
    w.writerow(["Código", "Nome", "Status", "Frequência", "Política autonomia", "Custo/mês"])
    ias = [
        ("CC", "Claude Code", "✅ atual", "sob demanda", "humano-guiado", "$200 (Code Pro)"),
        ("TO", "Tonha (CoS Patrol — orquestradora)", "✅ deployed", "30min cron", "Auto leitura/análise; Propor outbound", "$15-30"),
        ("CON", "CoS Conselheiro", "✅ **deployed v0.1 14/06**", "24h cron 9h BRT", "Auto RACI c/ evidência; Propor pauta/dossiê", "$15-20"),
        ("SAL🆕", "CoS Sales", "🆕 pendente", "12h cron", "Propor outbound sempre", "$5-10"),
        ("EDI🆕", "CoS Editorial", "🆕 pendente", "24h cron", "Propor publicação", "$10-15"),
        ("CUS🆕", "CoS Customer Success", "🆕 pendente", "24h cron", "Auto monitorar; Propor outreach", "$5-8"),
        ("RES🆕", "CoS Research", "🆕 pendente", "6h cron", "Auto leitura social", "$5"),
        ("POR🆕", "CoS Portfolio", "🆕 pendente", "24h cron", "Propor kill/snooze", "$3"),
        ("FIN🆕", "CoS Financial", "🆕 pendente", "12h cron", "Auto detectar receita; Propor pagar", "$3"),
        ("MEM🆕", "CoS Memory Curator", "🆕 pendente", "24h cron", "Auto save memory", "$5"),
        ("NET🆕", "CoS Network", "🆕 pendente", "7d cron", "Propor outreach", "$3"),
        ("CG", "ChatGPT Plus", "✅ atual", "sob demanda", "humano-guiado", "$20"),
        ("CL", "Claw (Genspark)", "⏳ avaliação 14d", "cross-app", "Propor (em teste)", "incluso GS"),
    ]
    for x in ias:
        w.writerow(list(x))
    w.writerow([])

    # FERRAMENTAS (channels/APIs usadas pelos agentes)
    w.writerow(["FERRAMENTAS / CANAIS (consumidas pelos agentes)"])
    w.writerow(["Código", "Nome", "O que provê"])
    ferramentas = [
        ("GS", "Genspark API (gsk CLI)", "image gen, x_get_user_tweets, summarize_large_document, phone_call (90+ tools)"),
        ("EVO", "Evolution API (Hetzner)", "WhatsApp send/receive bot 0192 (instance intel-bot-v2)"),
        ("GMAIL", "Gmail API (2 contas)", "incoming + outbound sync — fecha action blindness"),
        ("GCAL", "Google Calendar API", "eventos, RSVP, conflitos"),
        ("GDRIVE", "Google Drive API", "docs, ConselhoOS, backup Genspark"),
        ("FATH", "Fathom webhook", "atas + RACI parser automático"),
        ("COS", "ConselhoOS DB", "raci_itens, reunioes, decisoes (DB separado)"),
        ("LDIN", "LinkdAPI Hobby", "LinkedIn signals (jobchange, headline, posts)"),
    ]
    for x in ferramentas:
        w.writerow(list(x))
    w.writerow([])
    w.writerow([])

    # MATRIZ RACI
    w.writerow(["MATRIZ RACI — atividade × agente"])
    # Cabeçalho compacto
    header = ["#", "Atividade",
              "RE", "DAP", "AS", "PR", "JP",  # humanos
              "CC", "TO", "CON", "SAL🆕", "EDI🆕", "CUS🆕", "RES🆕", "POR🆕", "FIN🆕", "MEM🆕", "NET🆕",  # IAs
              "CG", "GS",  # external
              ]
    w.writerow(header)

    # Cada linha: tupla com #, atividade, e R/A/C/I (ou vazio) na ordem do header
    matrix = [
        # 1. Vendas B2B
        ("1", "Vendas / Pipeline B2B (imensIAH + ConselhoOS)",
         "A", "I", "R", "", "C",
         "C", "I", "", "R", "", "", "C", "", "", "", "",
         "", "C"),
        # 2. Demo + proposta técnica
        ("2", "Demo + proposta técnica imensIAH",
         "A", "C", "", "", "C",
         "C", "", "", "C", "", "", "", "", "", "", "",
         "", "C"),
        # 3. Dev produto SaaS
        ("3", "Desenvolvimento produto SaaS",
         "A", "R", "", "", "",
         "C", "", "", "", "", "", "", "", "", "", "",
         "", ""),
        # 4. INTEL eng
        ("4", "INTEL — engineering + automação",
         "A", "C", "", "", "",
         "R", "I", "", "", "", "", "", "", "", "", "",
         "", "C"),
        # 5. Atas/RACI 4 conselhos
        ("5", "Atas / RACI / pauta / dossiê (4 conselhos)",
         "A", "", "C", "", "",
         "C", "I", "R", "", "", "C", "", "", "", "I", "",
         "", "C (docs)"),
        # 6. Editorial texto+img+publica
        ("6", "Editorial — texto + imagem + publicação",
         "A", "", "", "", "",
         "C", "C", "", "", "R", "", "", "", "", "I", "",
         "C", "R (img)"),
        # 7. Editorial comentários
        ("7", "Editorial — comentários + engajamento",
         "R", "", "", "", "",
         "C", "C", "", "", "C", "", "", "", "", "", "",
         "", "C"),
        # 8. Portfolio review
        ("8", "Análise estratégica / portfolio review",
         "A", "C", "", "", "C",
         "R", "I", "C", "", "", "", "", "R", "", "C", "",
         "C", "C"),
        # 9. Monitoramento 24/7
        ("9", "Monitoramento sinais 24/7 (msgs/email/cal/X)",
         "A", "", "", "", "",
         "", "R (orq)", "C (RACI)", "I", "I", "I", "C (X)", "I", "I", "I", "C",
         "", "C (X/crawler)"),
        # 10. Outbound drafts cliente
        ("10", "Comunicação outbound — drafts cliente",
         "A", "", "C", "", "",
         "C", "R rascunha", "C", "C", "C", "C", "", "", "", "I", "",
         "", "C"),
        # 11. Back-office contábil
        ("11", "Back-office contábil / financeiro",
         "A", "", "R", "", "",
         "I", "I", "", "", "", "", "", "", "C", "I", "",
         "", ""),
        # 12. Regularizações
        ("12", "Regularizações jurídicas operacionais",
         "A", "", "C", "R", "C",
         "", "I", "", "", "", "", "", "", "", "", "",
         "", ""),
        # 13. Jurídico estratégico
        ("13", "Jurídico estratégico (contratos, NDAs)",
         "A", "", "", "C", "R",
         "C", "I", "C", "", "", "", "", "", "", "I", "",
         "C", ""),
        # 14. CS multi-cliente
        ("14", "Customer Success multi-cliente",
         "A", "", "C", "", "",
         "C", "I", "C", "", "", "R", "", "", "", "I", "",
         "", ""),
        # 15. Capital relacional
        ("15", "Capital relacional (Villela, Tanaka, etc)",
         "R", "", "", "", "",
         "C (briefing)", "C (sinais)", "C", "", "", "", "C (X)", "", "", "I", "R",
         "", "C (X profile)"),
        # 16. Memory / knowledge mgmt
        ("16", "Memory / knowledge management",
         "A", "", "", "", "",
         "C", "I", "C", "", "", "", "", "", "", "R", "",
         "", "C (sb-git)"),
    ]
    for row in matrix:
        w.writerow(list(row))
    w.writerow([])
    w.writerow([])

    # PROPOSTAS DE ADIÇÃO (humanos + IAs ainda pendentes)
    w.writerow(["PRÓXIMAS ADIÇÕES — ordem sugerida"])
    w.writerow(["#", "Item", "Tipo", "Por quê", "Custo/mês", "Gatilho"])
    propostas = [
        ("1", "CoS Portfolio 🆕", "IA", "Alto ROI, zero risco — só leitura+alertas drift projetos", "$3", "Imediato"),
        ("2", "CoS Editorial 🆕", "IA", "Substitui editorial_pdca + gera imagem via gsk img", "$10-15", "Imediato"),
        ("3", "CoS Research 🆕", "IA", "Alto valor estratégico (Villela/Tanaka tweets) zero risco", "$5", "Imediato"),
        ("4", "CoS Customer Success 🆕", "IA", "Multi-cliente — pega drift Vallen/Wadhwani antes de virar gargalo", "$5-8", "Imediato"),
        ("5", "CoS Sales 🆕", "IA", "Pipeline 130 Assespro — qualifica + rasc outreach pra AS executar", "$5-10", "Quando AS treinada"),
        ("6", "CoS Financial 🆕", "IA", "Auto-detect receita + alerta faturas", "$3", "Médio prazo"),
        ("7", "CoS Memory Curator 🆕", "IA", "Meta-agente — mantém memories + cos_config", "$5", "Médio prazo"),
        ("8", "CoS Network 🆕", "IA", "Reativação contatos C1-C2 dormentes", "$3", "Médio prazo"),
        ("9", "Customer Success fracional 🆕", "Humano PJ", "Vallen é único pagante. Quando 2º entrar, vira gargalo.", "$1-2k", "Gatilho: 2º cliente ConselhoOS"),
    ]
    for x in propostas:
        w.writerow(list(x))
    w.writerow([])

    # Frentes
    w.writerow(["FRENTES ESTRATÉGICAS (peso ratificado 11/06)"])
    w.writerow(["#", "Frente", "Peso", "Status atual"])
    frentes = [
        ("1", "imensIAH (Assespro canal, ICP founder PME)", "30%", "aposta principal — drift tier gratuito"),
        ("2", "ConselhoOS (Vallen + Wadhwani)", "20%", "gargalo monetização — 1 cliente pagante"),
        ("3", "Wadhwani Foundation", "—", "canal indireto — decisão Venture Partner pendente"),
        ("4", "Vallen Clinic direto", "15%", "operacional ativo — CONSELHEIRO cobrindo"),
        ("5", "Despertar + Villela", "5%", "drift baixo engajamento — 80d sem reunião"),
        ("—", "Alba Consultoria", "—", "gate kill 30/09 — reunião 16/06 cancelada Sandra"),
    ]
    for x in frentes:
        w.writerow(list(x))

    return buf.getvalue()


async def upload_as_sheet(token: str, csv_content: str, filename: str) -> dict:
    import httpx
    metadata = {
        "name": filename,
        "mimeType": "application/vnd.google-apps.spreadsheet",
    }
    boundary = "----raci_upload_boundary"
    body = (
        f"--{boundary}\r\n"
        f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(metadata)}\r\n"
        f"--{boundary}\r\n"
        f"Content-Type: text/csv\r\n\r\n"
        f"{csv_content}\r\n"
        f"--{boundary}--"
    ).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": f"multipart/related; boundary={boundary}",
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{GOOGLE_UPLOAD}?uploadType=multipart&fields=id,name,webViewLink",
            headers=headers,
            content=body,
        )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"upload falhou HTTP {resp.status_code}: {resp.text[:500]}")
    return resp.json()


async def delete_old(token: str, file_id: str):
    import httpx
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.delete(
            f"{GOOGLE_DRIVE}/{file_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
    return resp.status_code in (200, 204)


async def main():
    token = await get_token_professional()
    csv_content = build_csv()
    result = await upload_as_sheet(token, csv_content, "Matriz RACI v2 — Equipe Renato (15-06-2026)")
    print(f"\n✓ v2 criado: {result.get('webViewLink')}")
    ok = await delete_old(token, OLD_SHEET_ID_V1)
    print(f"  v1 deletado: {ok}")


if __name__ == "__main__":
    asyncio.run(main())
