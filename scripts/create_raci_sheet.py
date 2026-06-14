"""
Cria Google Sheet com a Matriz RACI (Equipe Renato — 14/06/26).
Sobe pra raiz do Drive Profissional (renato@almeida-prado.com).
"""
from __future__ import annotations

import asyncio
import csv
import io
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

from database import get_db  # noqa
from integrations.google_contacts import refresh_access_token  # noqa


GOOGLE_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
GOOGLE_DRIVE = "https://www.googleapis.com/drive/v3/files"


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
    """Constroi CSV multi-secao. Google Sheets aceita CSV simples; secoes
    viram blocos numa unica aba (visivel mas nao ideal). Pra ter abas separadas
    seria precisa Sheets API. Comprometido: 1 aba com tudo + blocos visiveis."""
    buf = io.StringIO()
    w = csv.writer(buf)

    # ========== Cabeçalho ==========
    w.writerow(["Matriz RACI — Equipe Renato Almeida Prado (14/06/2026)"])
    w.writerow([])
    w.writerow(["Convenção: R = executa | A = decide / responde | C = consulta | I = informa | (vazio) = não envolvido"])
    w.writerow(["🆕 = agente / função proposta (ainda não contratado)"])
    w.writerow([])

    # ========== Bloco 1: Agentes ==========
    w.writerow(["AGENTES"])
    w.writerow(["Código", "Nome", "Tipo", "Status", "Custo aprox/mês", "Especialidade"])
    agentes = [
        ("RE", "Renato", "Humano", "✅ core", "—", "decisão final, conselho, C-level, voz/identidade"),
        ("AS", "Andressa Santos", "Humano (paga/hora)", "✅ atual", "variável (hora cobrada)", "contábil, ECD, conciliações, ops backoffice"),
        ("PR", "Priscila", "Humano (paga/hora)", "✅ atual", "variável (hora cobrada)", "regularizações empresas (CNPJ, fiscal)"),
        ("CC", "Claude Code", "IA — sessão", "✅ atual", "$200/mês (Pro)", "engineering INTEL, dossiês profundos, análise sob contexto"),
        ("TO", "Tonha (CoS Patrol)", "IA — 24/7 cron", "✅ atual", "~$15-30/mês (Anthropic API)", "monitoramento WA/email/calendar, propostas curtas, ações Auto"),
        ("GS", "Genspark API", "IA — sob demanda", "✅ atual", "incluso Plus ($25)", "image gen, social monitoring (X), crawler, document analysis"),
        ("CG", "ChatGPT Plus", "IA — sob demanda", "✅ atual", "$20/mês", "2a opinião, voz mobile, DALL-E"),
        ("CL", "Claw (Genspark)", "IA — agente", "⏳ avaliação", "incluso Plus + créditos", "cross-app (Slack/Teams), controle desktop, drafts no tom"),
        ("SDR🆕", "SDR / BDR fracional", "Humano PJ", "🆕 proposto", "$1-2k/mês (~20h)", "qualificação fria 130 Assespro + ConselhoOS 2º cliente"),
        ("DEV🆕", "Dev backend SaaS fracional", "Humano PJ", "🆕 proposto", "$3-5k/mês (~40h)", "motor técnico imensIAH (evolução produto)"),
        ("CS🆕", "Customer Success fracional", "Humano PJ", "🆕 proposto (gatilho 2º cliente)", "$1-2k/mês", "ops Vallen + futuros pagantes ConselhoOS"),
        ("JR🆕", "Advisor jurídico", "Humano ad-hoc", "🆕 proposto", "$200-500/uso", "contratos, NDAs, advisory (não regularização)"),
    ]
    for a in agentes:
        w.writerow(list(a))
    w.writerow([])
    w.writerow([])

    # ========== Bloco 2: Matriz RACI ==========
    w.writerow(["MATRIZ RACI"])
    header = ["#", "Atividade", "RE", "AS", "PR", "CC", "TO", "GS", "CG", "CL", "SDR🆕", "DEV🆕", "CS🆕", "JR🆕"]
    w.writerow(header)

    matrix = [
        ("1", "Vendas / Pipeline B2B (imensIAH + ConselhoOS)",
         "A", "C", "", "C", "I", "C", "", "", "R", "", "", "C"),
        ("2", "Demo + proposta técnica imensIAH",
         "A", "", "", "C", "", "C", "", "", "C", "C", "", ""),
        ("3", "Desenvolvimento produto SaaS (imensIAH + ConselhoOS)",
         "A", "", "", "C", "", "", "", "", "", "R", "", ""),
        ("4", "INTEL — engineering + automação",
         "A", "", "", "R", "I", "C", "", "C", "", "", "", ""),
        ("5", "Atas / RACI / Follow-up RACI (ConselhoOS + Vallen)",
         "A", "C", "", "C", "R", "", "", "I", "", "", "C", ""),
        ("6", "Editorial — texto + imagem + publicação",
         "A", "", "", "C", "C", "R (img)", "C", "", "", "", "", ""),
        ("7", "Editorial — comentários / engajamento LinkedIn",
         "R/A", "", "", "C", "C", "C", "", "", "", "", "", ""),
        ("8", "Análise estratégica / portfolio review",
         "A", "", "", "R", "C", "C", "C", "", "", "", "", "C"),
        ("9", "Monitoramento sinais 24/7 (msgs / email / calendar / X)",
         "A", "", "", "", "R", "C (social)", "", "C", "", "", "", ""),
        ("10", "Comunicação outbound — drafts para cliente",
         "A", "C", "", "C", "R rascunha", "C", "", "C", "C", "", "C", ""),
        ("11", "Back-office contábil / financeiro (ECD, balanços)",
         "A", "R", "", "I", "I", "", "", "", "", "", "", ""),
        ("12", "Regularizações jurídicas operacionais (CNPJ, fiscal)",
         "A", "C", "R", "", "I", "", "", "", "", "", "", "C"),
        ("13", "Jurídico estratégico (contratos, NDAs, advisory)",
         "A", "", "C", "C", "I", "", "C", "", "", "", "", "R"),
        ("14", "Customer Success Vallen + futuros pagantes",
         "A", "C", "", "C", "I", "", "", "", "", "", "R (2º cliente)", ""),
        ("15", "Capital relacional (Villela, Tanaka, networking)",
         "R/A", "", "", "C (briefing)", "C (sinais)", "C (X)", "", "", "", "", "", ""),
        ("16", "Memory / knowledge management",
         "A", "", "", "C", "I", "C (sb-git)", "", "", "", "", "", ""),
    ]
    for row in matrix:
        w.writerow(list(row))
    w.writerow([])
    w.writerow([])

    # ========== Bloco 3: Propostas de adição ==========
    w.writerow(["PROPOSTAS DE ADIÇÃO — ordem sugerida"])
    w.writerow(["#", "Função", "Por quê", "Custo/mês", "Quando contratar", "Cobre atividade #"])
    propostas = [
        ("1", "Dev backend SaaS fracional",
         "imensIAH não tem motor de evolução técnica. CC cobre INTEL mas SaaS é outro escopo.",
         "$3-5k", "Imediato", "3"),
        ("2", "SDR / BDR fracional",
         "Renato sozinho não cobre 130 empresas Assespro + ConselhoOS 2º cliente.",
         "$1-2k", "Imediato", "1"),
        ("3", "Advisor jurídico ad-hoc",
         "Contratos imensIAH/ConselhoOS + NDAs Wadhwani sem revisão profissional.",
         "$200-500/uso", "Próximo contrato", "13"),
        ("4", "CS fracional",
         "Vallen é único cliente. Quando 2º entrar, vira gargalo.",
         "$1-2k", "Gatilho: 2º cliente ConselhoOS", "14"),
    ]
    for p in propostas:
        w.writerow(list(p))
    w.writerow([])
    w.writerow([])

    # ========== Bloco 4: Frentes (contexto) ==========
    w.writerow(["FRENTES ESTRATÉGICAS (contexto, peso ratificado 11/06)"])
    w.writerow(["#", "Frente", "Peso", "Status atual"])
    frentes = [
        ("1", "imensIAH (Assespro canal, ICP founder PME)", "30%", "aposta principal — drift na decisão tier gratuito"),
        ("2", "ConselhoOS (Vallen pagante + Wadhwani canal)", "20%", "gargalo monetização — 1 cliente pagante"),
        ("3", "Wadhwani Foundation", "—", "canal indireto — decisão Venture Partner pendente"),
        ("4", "Vallen Clinic", "15%", "operacional ativo — RACI weekly + pós-meeting"),
        ("5", "Despertar + Villela (capital relacional)", "5%", "exposição Itaúsa — drift baixo engajamento"),
        ("—", "Alba Consultoria", "—", "gate kill 30/09 — 1 ação/mês exigida"),
    ]
    for f in frentes:
        w.writerow(list(f))

    return buf.getvalue()


async def upload_as_sheet(token: str, csv_content: str, filename: str) -> dict:
    """Upload CSV com conversion automatica pra Google Sheet."""
    import httpx
    metadata = {
        "name": filename,
        "mimeType": "application/vnd.google-apps.spreadsheet",  # converte CSV → Sheet
    }

    boundary = "----raci_upload_boundary"
    body = (
        f"--{boundary}\r\n"
        f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{__import__('json').dumps(metadata)}\r\n"
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


async def main():
    csv_content = build_csv()
    token = await get_token_professional()
    filename = "Matriz RACI — Equipe Renato (v0 — 14-06-2026)"
    result = await upload_as_sheet(token, csv_content, filename)
    print("\n✓ Sheet criado!")
    print(f"  name: {result['name']}")
    print(f"  id:   {result['id']}")
    print(f"  url:  {result.get('webViewLink')}")


if __name__ == "__main__":
    asyncio.run(main())
