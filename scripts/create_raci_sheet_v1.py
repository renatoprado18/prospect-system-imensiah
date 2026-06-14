"""
Matriz RACI v1 (14/06/26) — atualizada com correções do Renato:
- DEV🆕 → DAP (Renato Dansieri, filho, AI Engineer imensIAH) — já é família
- SDR🆕 → AS expandida + TO + GS (combina humano + IA, sem novo agente)
- JR🆕 → JP (Dr. João Piccino, família)
- CS🆕 mantém pendente (gatilho 2º cliente)
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
OLD_SHEET_ID = "1gmDTaO624Kau20OWO-qaUObkJDkSbI8hi2H8F9BHSLA"


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

    # Cabeçalho
    w.writerow(["Matriz RACI v1 — Equipe Renato Almeida Prado (14/06/2026)"])
    w.writerow([])
    w.writerow(["Convenção: R = executa | A = decide / responde | C = consulta | I = informa | (vazio) = não envolvido"])
    w.writerow(["🆕 = agente / função proposta pendente"])
    w.writerow(["v1: incorpora correções 14/06 — DAP e JP entram como família, SDR fracional removido (AS+IA cobre), CS mantém pendente"])
    w.writerow([])

    # Agentes
    w.writerow(["AGENTES"])
    w.writerow(["Código", "Nome", "Tipo", "Status", "Custo aprox/mês", "Especialidade"])
    agentes = [
        ("RE", "Renato Almeida Prado", "Humano (você)", "✅ core", "—", "decisão final, conselho, C-level, voz/identidade"),
        ("DAP", "Renato Dansieri (filho)", "Humano — família", "✅ atual", "(arranjo familiar)", "Dev backend SaaS imensIAH (AI Engineer)"),
        ("AS", "Andressa Santos", "Humano (paga/hora)", "✅ atual — expandir papel", "variável", "contábil, ECD, conciliações, agora + qualificação fria SDR"),
        ("PR", "Priscila", "Humano (paga/hora)", "✅ atual", "variável", "regularizações empresas (CNPJ, fiscal, MEs)"),
        ("JP", "Dr. João Piccino", "Humano — família/advisor", "✅ atual", "ad-hoc (família)", "advisor jurídico estratégico (contratos, NDAs)"),
        ("CC", "Claude Code", "IA — sessão sob demanda", "✅ atual", "$200/mês (Pro)", "engineering INTEL, dossiês profundos, análise sob contexto"),
        ("TO", "Tonha (CoS Patrol)", "IA — 24/7 cron", "✅ atual", "~$15-30/mês (Anthropic API)", "monitoramento WA/email/calendar, propostas curtas, Auto"),
        ("GS", "Genspark API", "IA — sob demanda (CLI gsk)", "✅ atual", "incluso Plus ($25)", "image gen, social monitoring (X), crawler, doc analysis"),
        ("CG", "ChatGPT Plus", "IA — sob demanda", "✅ atual", "$20/mês", "2a opinião, voz mobile, DALL-E"),
        ("CL", "Claw (Genspark)", "IA — agente cross-app", "⏳ avaliação 14d", "incluso Plus + créditos", "Slack/Teams, controle desktop, drafts no tom"),
        ("CS🆕", "Customer Success fracional", "Humano PJ", "🆕 pendente — gatilho 2º cliente", "$1-2k/mês quando entrar", "ops Vallen + futuros pagantes ConselhoOS"),
    ]
    for a in agentes:
        w.writerow(list(a))
    w.writerow([])
    w.writerow([])

    # Matriz RACI
    w.writerow(["MATRIZ RACI"])
    header = ["#", "Atividade", "RE", "DAP", "AS", "PR", "JP", "CC", "TO", "GS", "CG", "CL", "CS🆕"]
    w.writerow(header)

    matrix = [
        # cada tupla: (idx, atividade, RE, DAP, AS, PR, JP, CC, TO, GS, CG, CL, CS)
        ("1", "Vendas / Pipeline B2B (imensIAH + ConselhoOS)",
         "A", "I", "R (qualificação+follow-up)", "", "C (contratos)", "C", "C (research+score)", "C (X, crawler)", "", "", ""),
        ("2", "Demo + proposta técnica imensIAH",
         "A", "C", "", "", "C (NDA)", "C", "", "C", "", "", ""),
        ("3", "Desenvolvimento produto SaaS (imensIAH + ConselhoOS)",
         "A", "R", "", "", "", "C", "", "", "", "", ""),
        ("4", "INTEL — engineering + automação",
         "A", "C", "", "", "", "R", "I", "C", "", "C", ""),
        ("5", "Atas / RACI / Follow-up RACI (ConselhoOS + Vallen)",
         "A", "", "C", "", "", "C", "R", "", "", "I", "C"),
        ("6", "Editorial — texto + imagem + publicação",
         "A", "", "", "", "", "C", "C", "R (img)", "C", "", ""),
        ("7", "Editorial — comentários / engajamento LinkedIn",
         "R/A", "", "", "", "", "C", "C", "C", "", "", ""),
        ("8", "Análise estratégica / portfolio review",
         "A", "C", "", "", "C", "R", "C", "C", "C", "", ""),
        ("9", "Monitoramento sinais 24/7 (msgs / email / calendar / X)",
         "A", "", "", "", "", "", "R", "C (social X)", "", "C", ""),
        ("10", "Comunicação outbound — drafts para cliente",
         "A", "", "C", "", "", "C", "R rascunha", "C", "", "C", "C"),
        ("11", "Back-office contábil / financeiro (ECD, balanços)",
         "A", "", "R", "", "", "I", "I", "", "", "", ""),
        ("12", "Regularizações jurídicas operacionais (CNPJ, fiscal)",
         "A", "", "C", "R", "C", "", "I", "", "", "", ""),
        ("13", "Jurídico estratégico (contratos, NDAs, advisory)",
         "A", "", "", "C", "R", "C", "I", "", "C", "", ""),
        ("14", "Customer Success Vallen + futuros pagantes",
         "A", "", "C", "", "", "C", "I", "", "", "", "R (quando 2º cliente)"),
        ("15", "Capital relacional (Villela, Tanaka, networking)",
         "R/A", "", "", "", "", "C (briefing)", "C (sinais)", "C (X profile)", "", "", ""),
        ("16", "Memory / knowledge management",
         "A", "", "", "", "", "C", "I", "C (sb-git)", "", "", ""),
    ]
    for row in matrix:
        w.writerow(list(row))
    w.writerow([])
    w.writerow([])

    # Análise crítica das 4 mudanças
    w.writerow(["ANÁLISE CRÍTICA — mudanças v0→v1"])
    w.writerow(["Mudança", "Impacto", "Risco / Atenção"])
    analise = [
        ("DEV→DAP (filho)",
         "Cobre #3 (Desenvolvimento produto SaaS). Família = compromisso forte, sem custo cash.",
         "Banda DAP — quantas horas/sem? Se for <10h/sem, motor imensIAH segue gargalo. Acordo de cadência precisa ser explícito."),
        ("SDR🆕 removido, AS expandida + TO+GS",
         "Cobre #1 (Vendas) sem novo PJ. Andressa qualifica leads frio sob roteiro, Tonha scoreia via X/LinkedIn, Genspark crawler enriquece.",
         "Andressa hoje é paga/hora pra contábil. Expandir pra SDR vai adicionar 5-10h/sem dela. Confirmar disponibilidade dela + treinamento em roteiro de qualificação. Não escalável a 130 empresas Assespro sem virar full-time."),
        ("JR→JP (família)",
         "Cobre #13 (jurídico estratégico). Sem custo recorrente.",
         "JP é família + advisor — relação social pode dificultar dizer não a pedidos urgentes. Estabelecer cadência (mensal? por demanda?) ajuda. Verificar conflito de interesse caso tenha relação com clientes."),
        ("CS🆕 mantém pendente",
         "Gatilho: 2º cliente pagante ConselhoOS. Hoje Renato cobre Vallen sozinho.",
         "Se demora muito 2º cliente, ok. Se entrar rápido sem CS pronto, Renato vira gargalo. Começar a mapear candidato em 30-60d antes de precisar."),
    ]
    for a in analise:
        w.writerow(list(a))
    w.writerow([])
    w.writerow([])

    # Frentes
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


async def delete_old_sheet(token: str, file_id: str):
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
    filename = "Matriz RACI v1 — Equipe Renato (14-06-2026)"
    result = await upload_as_sheet(token, csv_content, filename)
    print(f"\n✓ v1 criado: {result.get('webViewLink')}")

    # Deleta v0
    ok = await delete_old_sheet(token, OLD_SHEET_ID)
    print(f"  v0 deletado: {ok}")


if __name__ == "__main__":
    asyncio.run(main())
