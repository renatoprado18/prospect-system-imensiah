#!/usr/bin/env python3
"""
Processa reunião presencial (Plano B — sem diarização).

Entrada: transcrição em texto (vinda do bot WA INTEL ou ferramenta externa) +
ID da reunião no ConselhoOS.

Pipeline:
1. Carrega contexto da reunião + pessoas da empresa (ConselhoOS DB)
2. Carrega RACI atual da empresa (ConselhoOS DB)
3. Claude identifica falantes na transcrição usando lista de pessoas como dica
4. Claude gera ata_md no padrão da Vallen
5. Claude extrai sugestões de updates RACI a partir das decisões
6. Salva ata_md no ConselhoOS
7. Imprime relatório de sugestões pra revisão manual

Uso:
    python3 scripts/processar_reuniao_presencial.py \\
        --transcricao /path/to/transcript.txt \\
        --reuniao-id 13e33d01-1f47-4c19-8beb-fb5abcb7df1c \\
        [--dry-run]

Requer:
- ANTHROPIC_API_KEY no .env
- CONSELHOOS_DATABASE_URL no .env
"""
import argparse
import json
import os
import sys
from datetime import datetime

import psycopg2
from psycopg2.extras import RealDictCursor
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv('/Users/rap/prospect-system/.env')

CLAUDE_MODEL = "claude-opus-4-7"


def conselhoos_conn():
    return psycopg2.connect(
        os.environ['CONSELHOOS_DATABASE_URL'].strip(),
        cursor_factory=RealDictCursor,
    )


def load_context(reuniao_id):
    with conselhoos_conn() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT r.*, e.nome AS empresa_nome, e.id AS empresa_id "
            "FROM reunioes r JOIN empresas e ON e.id = r.empresa_id "
            "WHERE r.id = %s",
            (reuniao_id,),
        )
        reuniao = c.fetchone()
        if not reuniao:
            sys.exit(f"❌ reuniao_id {reuniao_id} não encontrada")

        c.execute(
            "SELECT nome, cargo, papel FROM pessoas WHERE empresa_id=%s AND ativo=true",
            (reuniao['empresa_id'],),
        )
        pessoas = [dict(r) for r in c.fetchall()]

        c.execute(
            "SELECT id, area, acao, prazo, status, responsavel_r, responsavel_a, notas "
            "FROM raci_itens WHERE empresa_id=%s AND status != 'concluido' "
            "ORDER BY prazo NULLS LAST",
            (reuniao['empresa_id'],),
        )
        raci = [dict(r) for r in c.fetchall()]

        return reuniao, pessoas, raci


def claude_call(client, system, user, max_tokens=8000):
    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return resp.content[0].text


def identificar_falantes(client, transcricao, pessoas, reuniao):
    sys_prompt = (
        "Você está preparando ata de reunião de conselho. Recebe transcrição "
        "bruta sem diarização e a lista de participantes esperados. Sua tarefa: "
        "reescrever a transcrição atribuindo cada bloco de fala à pessoa mais "
        "provável, usando: (1) menções diretas de nome; (2) papel/cargo na "
        "reunião; (3) contexto temático (financeiro -> CFO, marketing -> "
        "marketing). Marque com '[?]' antes do nome quando confidence baixa. "
        "Preserve o texto original — apenas adicione anotações de falante "
        "no formato 'Nome:' no início de cada bloco. Não invente conteúdo."
    )
    user = (
        f"REUNIÃO: {reuniao['titulo']} ({reuniao['data']})\n\n"
        f"PARTICIPANTES ESPERADOS:\n"
        + "\n".join(f"- {p['nome']} ({p['cargo']}/{p['papel']})" for p in pessoas)
        + f"\n\nTRANSCRIÇÃO BRUTA:\n{transcricao}\n\n"
        "Retorne a transcrição diarizada (texto puro, sem markdown extra)."
    )
    return claude_call(client, sys_prompt, user, max_tokens=16000)


def gerar_ata(client, transcricao_diarizada, reuniao, pessoas):
    sys_prompt = (
        "Você gera atas formais de reunião de conselho seguindo padrão "
        "profissional. Estrutura: cabeçalho, contexto/abertura, blocos "
        "temáticos com decisões, RACI atualizada, pendências, próximos passos. "
        "Use markdown com headings ## e ###. Cite participantes pelo nome. "
        "Não invente — só registre o que está na transcrição."
    )
    user = (
        f"REUNIÃO: {reuniao['titulo']}\nDATA: {reuniao['data']}\n"
        f"PARTICIPANTES: {', '.join(p['nome'] for p in pessoas)}\n\n"
        f"TRANSCRIÇÃO DIARIZADA:\n{transcricao_diarizada}\n\n"
        "Gere a ata em markdown."
    )
    return claude_call(client, sys_prompt, user, max_tokens=12000)


def extrair_updates_raci(client, transcricao_diarizada, raci_atual):
    sys_prompt = (
        "Você analisa transcrição de reunião e identifica updates a aplicar "
        "nos itens RACI existentes. Para cada update, retorne JSON: "
        "{raci_item_id, suggested_status, suggested_nota, suggested_prazo, "
        "confidence (0-1), excerpt}. status ∈ {pendente, em_andamento, "
        "concluido}. Inclua apenas itens claramente discutidos. Não invente. "
        "Retorne array JSON puro, sem markdown."
    )
    raci_summary = "\n".join(
        f"- id={r['id']} | {r['area']}: {r['acao']} | prazo={r['prazo']} | "
        f"status={r['status']} | R={r['responsavel_r']}"
        for r in raci_atual
    )
    user = (
        f"RACI ATUAL DA EMPRESA:\n{raci_summary}\n\n"
        f"TRANSCRIÇÃO:\n{transcricao_diarizada}\n\n"
        "Retorne array JSON com updates sugeridos."
    )
    raw = claude_call(client, sys_prompt, user, max_tokens=4000)
    # Strip possible code fences
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print("⚠️  Claude não retornou JSON válido — saída bruta:", file=sys.stderr)
        print(raw, file=sys.stderr)
        return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--transcricao", required=True, help="path do .txt com transcrição")
    ap.add_argument("--reuniao-id", required=True, help="UUID da reunião no ConselhoOS")
    ap.add_argument("--dry-run", action="store_true", help="não grava no DB")
    args = ap.parse_args()

    with open(args.transcricao) as f:
        transcricao = f.read()
    if len(transcricao) < 200:
        sys.exit("❌ transcrição muito curta — não parece reunião")

    print(f"📂 Transcrição: {len(transcricao):,} chars")

    reuniao, pessoas, raci = load_context(args.reuniao_id)
    print(f"🏢 Empresa: {reuniao['empresa_nome']}")
    print(f"📅 Reunião: {reuniao['titulo']} ({reuniao['data']})")
    print(f"👥 {len(pessoas)} participantes esperados")
    print(f"📋 {len(raci)} itens RACI ativos\n")

    client = Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'].strip())

    print("🤖 [1/3] Identificando falantes...")
    diarizada = identificar_falantes(client, transcricao, pessoas, reuniao)
    out_diar = f"/tmp/reuniao_{args.reuniao_id[:8]}_diarizada.txt"
    with open(out_diar, "w") as f:
        f.write(diarizada)
    print(f"   ✓ {out_diar}\n")

    print("🤖 [2/3] Gerando ata...")
    ata_md = gerar_ata(client, diarizada, reuniao, pessoas)
    out_ata = f"/tmp/reuniao_{args.reuniao_id[:8]}_ata.md"
    with open(out_ata, "w") as f:
        f.write(ata_md)
    print(f"   ✓ {out_ata} ({len(ata_md):,} chars)\n")

    print("🤖 [3/3] Extraindo updates RACI sugeridos...")
    updates = extrair_updates_raci(client, diarizada, raci)
    out_updates = f"/tmp/reuniao_{args.reuniao_id[:8]}_raci_updates.json"
    with open(out_updates, "w") as f:
        json.dump(updates, f, indent=2, default=str)
    print(f"   ✓ {len(updates)} updates sugeridos -> {out_updates}\n")

    if updates:
        print("Updates sugeridos:")
        for u in updates:
            conf_emoji = "🟢" if u.get('confidence', 0) >= 0.8 else (
                "🟡" if u.get('confidence', 0) >= 0.5 else "🔴")
            item = next((r for r in raci if str(r['id']) == str(u['raci_item_id'])), None)
            acao_curta = (item['acao'][:60] + '...') if item and len(item['acao']) > 60 else (item['acao'] if item else '?')
            print(f"  {conf_emoji} [{u.get('confidence', 0):.0%}] {acao_curta}")
            print(f"     -> status: {u.get('suggested_status')}")
            print(f"     -> nota: {u.get('suggested_nota', '')[:100]}")
            print()

    if args.dry_run:
        print("🏁 DRY RUN — nada gravado. Revisar os arquivos em /tmp e rodar sem --dry-run.")
        return

    print("💾 Gravando ata_md no ConselhoOS...")
    with conselhoos_conn() as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE reunioes SET ata_md=%s, updated_at=NOW() WHERE id=%s",
            (ata_md, args.reuniao_id),
        )
        conn.commit()
    print("   ✓ reunioes.ata_md atualizada\n")

    print(f"\n✅ FEITO. Próximos passos:")
    print(f"   1. Revisar ata em {out_ata}")
    print(f"   2. Revisar updates RACI em {out_updates}")
    print(f"   3. Para aplicar updates RACI ao banco, peça pra Claude com o JSON")
    print(f"   4. Gerar .docx via INTEL endpoint /api/ata/generate-docx (opcional)")


if __name__ == "__main__":
    main()
