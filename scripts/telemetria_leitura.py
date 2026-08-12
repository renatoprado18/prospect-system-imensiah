#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""Telemetria de LEITURA da memória — que memo é de fato aberto numa sessão.

READ-ONLY: não altera arquivo nem banco. Lê o JSONL do hook e compara com a FONTE
(o `MEMORY.md` e o diretório de memória).

POR QUE ESTE EXISTE (12/08/26, #999768). Decidir o que sai do índice exigia saber
o que é usado, e o único número que havia — a telemetria de recall da migration
070 — mede OUTRO consumidor: a busca em `system_memories`, feita pela camada. A
abertura de sessão não passa por lá; ela lê o `MEMORY.md` como ARQUIVO, inteiro,
toda vez. E aquele número estava congelado: 209 recalls, todos numa janela de 16
segundos em 10/08, porque o único produtor (`cos-daily-review` → `frente_review`)
virou cron condicional no dia seguinte e nunca mais rodou.

⚠️ O VIÉS, e ele decide como o número pode ser usado. Memória cujo HOOK NO ÍNDICE
já basta nunca é aberta: `feedback_voice_cortesia` entrega *"grato, nunca
obrigado"* na própria linha — leitura zero para sempre, e funcionando. **"Nunca
aberto" não é "inútil": pode ser "resolvido no índice".** Por isso este número
decide o que DESCE para um índice de 2º nível (encontrável sob demanda), NUNCA o
que é apagado. Ver [[feedback_medidor_que_nao_mede_a_si_mesmo]].

Também não mede: leitura por `search_memories` (isso é a 070), leitura pela tonIAH,
e qualquer sessão anterior a 12/08/26 — o contador nasceu aqui.

Uso:  ./telemetria_leitura.py            # relatório
      ./telemetria_leitura.py --quiet    # só o cabeçalho (abertura de sessão)
"""
import argparse
import collections
import datetime
import json
import os
import re
import sys

JSONL = os.path.expanduser("~/.claude/memory_reads.jsonl")
MEM_DIR = "/Users/rap/.claude/projects/-Users-rap-prospect-system/memory"
INDEX = os.path.join(MEM_DIR, "MEMORY.md")


def carregar():
    """Eventos do hook. Linha corrompida não derruba o relatório — conta separado:
    silenciar o descarte esconderia o denominador."""
    eventos, corrompidas = [], 0
    if os.path.exists(JSONL):
        with open(JSONL, encoding="utf-8") as f:
            for linha in f:
                linha = linha.strip()
                if not linha:
                    continue
                try:
                    eventos.append(json.loads(linha))
                except json.JSONDecodeError:
                    corrompidas += 1
    return eventos, corrompidas


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    eventos, corrompidas = carregar()
    with open(INDEX, encoding="utf-8") as f:
        index_text = f.read()
    indexados = set(re.findall(r'\]\(([a-zA-Z0-9_]+)\.md\)', index_text))
    no_disco = {fn[:-3] for fn in os.listdir(MEM_DIR)
                if fn.endswith(".md") and fn != "MEMORY.md"}

    por_arquivo = collections.Counter(e["arquivo"][:-3] for e in eventos if e.get("arquivo"))
    sessoes = {e.get("sessao") for e in eventos}
    lidos = set(por_arquivo)

    if eventos:
        datas = sorted(e["ts"] for e in eventos if e.get("ts"))
        desde, ate = datas[0][:10], datas[-1][:10]
        dias = (datetime.date.fromisoformat(ate) - datetime.date.fromisoformat(desde)).days + 1
    else:
        desde = ate = "—"
        dias = 0

    print("╔═ TELEMETRIA DE LEITURA DA MEMÓRIA ═╗")
    print(f"  {len(eventos)} leituras · {len(lidos)} memos distintos · "
          f"{len(sessoes - {None})} sessões · {desde} → {ate} ({dias}d)")
    print(f"  denominador: {len(indexados)} no índice · {len(no_disco)} no disco")
    if corrompidas:
        print(f"  ⚠️ {corrompidas} linhas ilegíveis no JSONL (ignoradas)")

    if dias < 14:
        print(f"  🟡 AMOSTRA CURTA ({dias}d) — número ainda não decide nada. Alvo: 14-21d.")

    if a.quiet:
        return 0

    nunca_lidos = sorted(indexados - lidos)
    print(f"\n  ─ Cobertura ─")
    print(f"  · {len(indexados & lidos)} de {len(indexados)} linhas do índice já levaram a uma abertura")
    print(f"  · {len(nunca_lidos)} nunca abertas → candidatas a DESCER pro 2º nível, não a sumir")
    fora = sorted(lidos - indexados)
    if fora:
        print(f"  · {len(fora)} abertas SEM estar no índice (chegaram por [[link]] ou caminho direto):")
        for s in fora[:10]:
            print(f"      {s} ({por_arquivo[s]}×)")

    if por_arquivo:
        print(f"\n  ─ Mais abertos ─")
        for slug, n in por_arquivo.most_common(15):
            marca = "" if slug in indexados else "  [fora do índice]"
            print(f"    {n:>3}×  {slug}{marca}")

    print("\n  ⚠️ 'Nunca aberto' NÃO é 'inútil'. Memória cujo hook no índice já basta")
    print("     (ex.: voice_cortesia — 'grato, nunca obrigado') tem leitura zero e está")
    print("     funcionando. Este número move pro 2º nível; nunca apaga.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
