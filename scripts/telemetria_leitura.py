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

════════════════════════════════════════════════════════════════════════════
ADENDO 14/08/26 — OS DOIS NÚMEROS VIRARAM COMPLEMENTARES, E NENHUM SOZINHO
DECIDE A PODA. Duas coisas mudaram no mesmo dia e mudam como este script se lê:

1. **A partição em 2 níveis.** O índice deixou de ser um arquivo só: `MEMORY.md`
   é o nível 1 (carregado em toda sessão) e `MEMORY_NIVEL2.md` é o nível 2 (não
   carregado, alcançado por `search_memories`). Enquanto este script olhava só o
   `MEMORY.md`, as 187 entradas que desceram apareceriam como "abertas SEM estar
   no índice" — o denominador quebrava calado no dia seguinte à partição.

2. **O recall voltou a rodar, e forte.** O parágrafo acima ("congelado desde
   10/08") deixou de valer: medido em 14/08, há 100 recalls em 10 memórias NO
   MESMO DIA, via `intel`, mais 7 via `mcp` em 13/08. Total acumulado: 235.

Daí a correção de rumo: **cada nível tem o seu medidor, e é erro usar um pelo
outro.** Memória de nível 1 é ABERTA (Read) — mede-se aqui, pelo JSONL. Memória
de nível 2 não é aberta nunca: ela é RECUPERADA por busca — e quem mede isso é
justamente o `recall_count` da 070. Decidir a poda do nível 2 pela telemetria de
leitura apagaria memória que a busca está entregando todo dia.

Isto NÃO revoga [[feedback_medir_o_consumidor_certo]] — confirma a regra e troca
a resposta, porque o consumidor mudou. Antes da partição havia um consumidor
(a sessão lendo o índice) e o recall media o errado. Agora há dois consumidores
distintos, e cada um tem o seu número.
════════════════════════════════════════════════════════════════════════════

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
INDEX_N2 = os.path.join(MEM_DIR, "MEMORY_NIVEL2.md")


def _slugs(caminho):
    """Entradas de um índice. Ausente devolve conjunto vazio — o nível 2 pode
    não existir num checkout anterior a 14/08 e isso não é erro."""
    if not os.path.exists(caminho):
        return set()
    with open(caminho, encoding="utf-8") as f:
        return set(re.findall(r'\]\(([a-zA-Z0-9_]+)\.md\)', f.read()))


def recall_por_nivel(n1, n2):
    """Lê o `recall_count` da 070 e o distribui pelos dois níveis.

    READ-ONLY e best-effort: sem banco, o relatório de leitura continua íntegro e
    o bloco de recall diz que não foi medido. O que NÃO se faz é omitir a seção em
    silêncio — seria o script certificando uma cobertura que não checou
    ([[feedback_guarda_abstencao_vira_fabrica]]).
    """
    try:
        import psycopg2
    except ImportError:
        return None, "psycopg2 indisponível"
    env = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    url = None
    if os.path.exists(env):
        for linha in open(env, encoding="utf-8"):
            if linha.strip().startswith("DATABASE_URL="):
                url = linha.strip().split("=", 1)[1].strip().strip('"').strip("'")
                break
    if not url:
        return None, "DATABASE_URL ausente no .env"
    try:
        conn = psycopg2.connect(url, connect_timeout=8)
    except Exception as e:  # rede fora, credencial trocada — nunca derruba o resto
        return None, f"sem conexão ({str(e).strip().splitlines()[0][:48]})"

    dados = {"N1": [0, 0], "N2": [0, 0], "hist": [0, 0], "fora": [0, 0]}
    ult, vias = None, collections.Counter()
    with conn, conn.cursor() as cur:
        cur.execute("""SELECT tags::text, recall_count, ultimo_recall_em, ultimo_recall_via
                         FROM system_memories WHERE recall_count > 0""")
        for tags, rc, quando, via in cur.fetchall():
            m = re.search(r'([A-Za-z0-9_]+)\.md', tags or "")
            if not m:
                continue
            slug = m.group(1)
            nivel = ("N1" if slug in n1 else "N2" if slug in n2
                     else "hist" if slug.endswith("_historico") else "fora")
            dados[nivel][0] += 1
            dados[nivel][1] += rc
            if quando and (ult is None or quando > ult):
                ult = quando
            vias[via or "?"] += 1
    conn.close()
    return {"niveis": dados, "ultimo": ult, "vias": vias}, None


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
    # `indexados` = nível 1. É o denominador CERTO desta telemetria: ela mede
    # abertura de arquivo, e só o nível 1 é carregado por leitura. O nível 2 entra
    # separado, e é medido pelo recall — ver o adendo de 14/08 no topo.
    n1 = _slugs(INDEX)
    n2 = _slugs(INDEX_N2)
    indexados = n1
    no_disco = {fn[:-3] for fn in os.listdir(MEM_DIR)
                if fn.endswith(".md") and fn not in ("MEMORY.md", "MEMORY_NIVEL2.md")}

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
    print(f"  denominador: {len(indexados)} no nível 1 · {len(n2)} no nível 2 · {len(no_disco)} no disco")
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
    # Aberto e não está no nível 1 tem DOIS significados muito diferentes, e
    # colapsá-los era o painel mentindo: memória do nível 2 está indexada, só não
    # é carregada. Chamar isso de "fora do índice" faria a próxima sessão procurar
    # um ponteiro perdido que não existe.
    do_n2 = sorted((lidos - indexados) & n2)
    orfas = sorted(lidos - indexados - n2)
    if do_n2:
        print(f"  · {len(do_n2)} abertas que moram no NÍVEL 2 — normal: desceram e seguem")
        print(f"    alcançáveis. Abertura frequente aqui é sinal de candidata a SUBIR:")
        for s in do_n2[:10]:
            print(f"      {s} ({por_arquivo[s]}×)")
    if orfas:
        print(f"  · {len(orfas)} abertas SEM estar em índice nenhum (chegaram por [[link]] ou caminho direto):")
        for s in orfas[:10]:
            print(f"      {s} ({por_arquivo[s]}×)")

    if por_arquivo:
        print(f"\n  ─ Mais abertos ─")
        for slug, n in por_arquivo.most_common(15):
            marca = ("" if slug in indexados
                     else "  [nível 2]" if slug in n2
                     else "  [fora dos dois índices]")
            print(f"    {n:>3}×  {slug}{marca}")

    print("\n  ⚠️ 'Nunca aberto' NÃO é 'inútil'. Memória cujo hook no índice já basta")
    print("     (ex.: voice_cortesia — 'grato, nunca obrigado') tem leitura zero e está")
    print("     funcionando. Este número move pro 2º nível; nunca apaga.")

    # ─ Nível 2: outro consumidor, outro medidor ─────────────────────────────
    rec, erro = recall_por_nivel(n1, n2)
    print("\n  ─ Nível 2 — recuperação por busca (migration 070) ─")
    if erro:
        print(f"    ⚠️ NÃO MEDIDO: {erro}.")
        print("    Sem este bloco a poda do nível 2 fica sem evidência — não decidir só")
        print("    pela leitura, que mede o nível 1.")
        return 0

    d = rec["niveis"]
    print(f"    {d['N2'][0]:>3} das {len(n2)} memórias do nível 2 já foram recuperadas"
          f" ({d['N2'][1]} recalls)")
    print(f"    {d['N1'][0]:>3} das {len(n1)} do nível 1 também aparecem em busca"
          f" ({d['N1'][1]} recalls)")
    if d["hist"][0]:
        print(f"    {d['hist'][0]:>3} arquivos *_historico — {d['hist'][1]} recalls, fora de índice"
              f" por desenho")
    if d["fora"][0]:
        print(f"    {d['fora'][0]:>3} fora dos dois índices ({d['fora'][1]} recalls)")
    if rec["ultimo"]:
        print(f"    último recall: {rec['ultimo']:%d/%m %H:%M} · por via: "
              + " · ".join(f"{k}={v}" for k, v in rec["vias"].most_common()))

    print("\n    ⚠️ Este é o número que decide a PODA do nível 2, não o de leitura acima.")
    print("       Memória do nível 2 não é aberta por Read — é recuperada por busca.")
    print("       Recall > 0 é prova de uso; recall 0 é candidatura a poda, e ainda")
    print("       assim só depois da janela combinada. Ver [[feedback_medir_o_consumidor_certo]].")
    return 0


if __name__ == "__main__":
    sys.exit(main())
