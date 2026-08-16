#!/usr/bin/env python3
"""
PDCA da camada CoS — mede o que subiu e diz o que ajustar.

POR QUE EXISTE. Em 31/07 subimos a triagem horária com três parâmetros que foram
CHUTE fundamentado, não medição: debounce 90 min, teto diário 18, 3 frentes por
rodada. Sem PDCA eles viram folclore — e este sistema já tem histórico disso: o
teto de 4.096 chars da Evolution foi convenção do nosso código que virou "fato"
em commit, doc e memória, e recusava o RACI real da Vallen até alguém medir o
canal.

O QUE FAZ ESTE ARQUIVO SER PDCA E NÃO PAINEL. Cada bloco traz uma REGRA DE
DECISÃO explícita — se o número passar de X, faça Y. Painel sem regra é
observação; regra sem número é opinião. E o bloco final força o registro do
ajuste feito, porque sem isso não dá pra saber se o ajuste seguinte melhorou ou
piorou (foi assim que o "71% sai por fora" sobreviveu meses).

O QUE ELE NÃO MEDE, e é honesto dizer: QUALIDADE do julgamento. Nenhuma query
sabe se o portão de hoje estava certo. Isso é o placar manual — acertou / cobrou
à toa / deixou passar — que só o Renato preenche. O bloco 5 mostra o que existe
e lembra que o resto é dele.

Uso:  ./pdca.py            # últimos 7 dias
      ./pdca.py --dias 14
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from run import (_conn, DEBOUNCE_MIN, TETO_DIARIO, MAX_POR_RODADA,  # noqa: E402
                 HORA_INICIO, HORA_FIM)

VERDE, AMARELO, VERMELHO = "🟢", "🟡", "🔴"
# A janela do agente e definida em BRT (HORA_INICIO/HORA_FIM), entao "mesmo dia"
# tem que ser medido em BRT — em UTC a virada cai as 21h locais e cortaria a
# ultima rodada do dia pro lado errado.
BRT = timezone(timedelta(hours=-3))


def _h(titulo: str) -> None:
    print(f"\n{'─' * 72}\n{titulo}\n{'─' * 72}")


def carregar(ro_url: str, dias: int) -> list[dict]:
    with _conn(ro_url) as c, c.cursor() as cur:
        cur.execute("""
            SELECT id, run_at, payload
            FROM cos_daily_review
            WHERE run_at > NOW() - make_interval(days => %s)
            ORDER BY run_at
        """, (dias,))
        linhas = []
        for r in cur.fetchall():
            p = r["payload"]
            linhas.append({"id": r["id"], "run_at": r["run_at"],
                           "p": p if isinstance(p, dict) else json.loads(p)})
        return linhas


def bloco_consumo(runs: list[dict]) -> None:
    _h("1. CONSUMO — cabe na capacidade do Max?")
    ag = [r for r in runs if (r["p"].get("motor") == "agente_local")]
    if not ag:
        print("   sem rodadas do agente na janela.")
        return
    por_dia: dict = {}
    for r in ag:
        d = (r["run_at"] - timedelta(hours=3)).date()
        t = r["p"].get("triagem") or {}
        e = por_dia.setdefault(d, {"julg": 0, "usd": 0.0, "rodadas": 0})
        e["julg"] += int(t.get("julgadas_agora") or 0)
        e["usd"] += float(r["p"].get("custo_usd") or 0)
        e["rodadas"] += 1
    print(f"   {'dia':<12}{'rodadas':>9}{'julgamentos':>14}{'US$ nocional':>15}{'% do teto':>12}")
    estourou = 0
    for d in sorted(por_dia):
        e = por_dia[d]
        pct = 100 * e["julg"] / TETO_DIARIO
        if pct >= 90:
            estourou += 1
        print(f"   {d!s:<12}{e['rodadas']:>9}{e['julg']:>14}{e['usd']:>15.2f}{pct:>11.0f}%")
    print(f"\n   REGRA: teto atual = {TETO_DIARIO} julgamentos/dia.")
    if estourou >= 3:
        print(f"   {VERMELHO} {estourou} dias em ≥90% do teto. O teto está cortando julgamento real —")
        print("      subir TETO_DIARIO, OU baixar as frentes por rodada pra espalhar melhor.")
    elif estourou:
        print(f"   {AMARELO} {estourou} dia(s) em ≥90%. Observar; ainda não é padrão.")
    else:
        media = sum(e["julg"] for e in por_dia.values()) / max(len(por_dia), 1)
        print(f"   {VERDE} média {media:.1f}/dia, folga confortável. "
              f"Se ficar <30% do teto por uma semana, o teto está frouxo e o gargalo é outro.")


def bloco_triagem(runs: list[dict]) -> None:
    _h("2. TRIAGEM — a hora a hora está achando trabalho ou girando à toa?")
    hor = [r for r in runs if (r["p"].get("triagem") or {}).get("modo") == "horario"]
    if not hor:
        print("   nenhuma rodada em modo horário ainda (a 1ª é 07:12).")
        return
    vazias = sum(1 for r in hor if not (r["p"]["triagem"].get("julgadas_agora")))
    total = len(hor)
    mot: dict = {}
    # Rodadas gravadas ANTES da correção de 31/07 à noite somavam os dois cortes
    # em `cortada_pelo_teto`. Elas não têm `_elegiveis`, e é assim que as
    # reconhecemos. Contá-las junto faria este bloco acusar o teto pelo que o
    # limite por rodada fez — o erro que a correção existe pra impedir. A régua
    # também é métrica: quando parte da janela não é comparável, diga.
    legado = 0
    for r in hor:
        nj = r["p"]["triagem"].get("nao_julgadas") or {}
        if "_elegiveis" not in nj:
            legado += 1
            continue
        for k, v in nj.items():
            if not k.startswith("_") and isinstance(v, int):
                mot[k] = mot.get(k, 0) + v
    print(f"   rodadas: {total} · sem nada a julgar: {vazias} ({100*vazias/total:.0f}%)")
    if legado:
        print(f"   ⚠️  {legado} de {total} rodadas são ANTERIORES à correção de 31/07 e não")
        print("      separam 'teto diário' de 'limite por rodada' — ficaram FORA da soma")
        print("      abaixo. O diagnóstico só cobre as rodadas comparáveis.")
    if not mot:
        print("   nenhuma rodada comparável ainda — volte depois da próxima rodada.")
        return
    print("   por que as frentes NÃO foram julgadas (somado):")
    for k, v in sorted(mot.items(), key=lambda kv: -kv[1]):
        print(f"      {k:<22} {v}")
    print(f"\n   REGRA: rodada vazia é BARATA (só SQL) — vazio não é desperdício.")
    # DOIS gargalos diferentes, DUAS decisões diferentes. Até 31/07 os dois
    # vinham somados em `cortada_pelo_teto` e este bloco mandava "ver bloco 1"
    # (teto diário) mesmo quando o que barrou foi a fatia da rodada — subir o
    # teto não teria movido o número. Diagnóstico que aponta o parâmetro errado
    # é pior que nenhum: dá a sensação de ter medido.
    por_teto = mot.get("cortada_pelo_teto", 0)
    por_rodada = mot.get("cortada_pelo_limite_rodada", 0)
    if total and vazias / total > 0.8:
        print(f"   {AMARELO} >80% vazias. Não custa, mas 14 disparos/dia pra 2 úteis sugere")
        print("      espaçar (de 2 em 2h) sem perder nada. Decisão de elegância, não de custo.")
    if por_teto:
        print(f"   {VERMELHO} {por_teto} frente(s) ficaram de fora POR TETO DIÁRIO —")
        print(f"      o orçamento do dia ({TETO_DIARIO}) acabou antes da janela. É julgamento")
        print("      que não aconteceu no dia. AÇÃO: subir TETO_DIARIO. Ver bloco 1.")
    if por_rodada:
        print(f"   {AMARELO} {por_rodada} frente(s) esperaram a PRÓXIMA RODADA —")
        print(f"      a fatia por rodada ({MAX_POR_RODADA}) encheu, mas o orçamento do dia tinha folga.")
        print("      Não é julgamento perdido, é ADIADO ~1h: aparece como atraso no bloco 4,")
        print("      não como buraco. AÇÃO (só se o bloco 4 estiver fora do alvo):")
        print("      subir MAX_POR_RODADA — mexer no TETO_DIARIO aqui não muda nada.")
    if not (por_teto or por_rodada) and not (total and vazias / total > 0.8):
        print(f"   {VERDE} a triagem está separando sinal de silêncio sem estourar nada.")


def bloco_debounce(runs: list[dict]) -> None:
    _h(f"3. DEBOUNCE ({DEBOUNCE_MIN} min) — está protegendo ou atrasando?")
    hor = [r for r in runs if (r["p"].get("triagem") or {}).get("modo") == "horario"]
    barradas: dict = {}
    for r in hor:
        for pid in ((r["p"]["triagem"].get("nao_julgadas") or {}).get("_debounce_ids") or []):
            barradas[pid] = barradas.get(pid, 0) + 1
    if not barradas:
        print(f"   {VERDE} ninguém foi barrado. O debounce não está mordendo — ou o dia foi calmo.")
    else:
        print("   frentes barradas por estarem 'quentes demais':")
        for pid, n in sorted(barradas.items(), key=lambda kv: -kv[1])[:8]:
            print(f"      #{pid:<5} barrada {n}×")
        pior = max(barradas.values())
        print(f"\n   REGRA: barrar 1-2× é o desenho funcionando (conversa ao vivo).")
        if pior >= 5:
            print(f"   {VERMELHO} uma frente foi barrada {pior}× — ela passou o dia 'quente' e o")
            print("      portão dela ficou velho justamente no dia em que mais andava.")
            print("      BAIXAR DEBOUNCE_MIN, ou dar exceção pra frente de prioridade ≥8.")
        else:
            print(f"   {VERDE} máximo {pior}× — está segurando repetição sem cegar frente ativa.")


def bloco_frescor(runs: list[dict]) -> None:
    _h("4. FRESCOR DO PORTÃO — o KPI da mudança de hoje")
    print("   Quanto tempo, em média, uma frente fica sem ser re-julgada depois de")
    print("   se mexer. É a idade da foto — a queixa que originou tudo.")
    julg: dict = {}
    for r in runs:
        for f in (r["p"].get("frentes") or []):
            ts = (f.get("_meta") or {}).get("julgado_em")
            if ts and not (f.get("_meta") or {}).get("herdado"):
                julg.setdefault(f["project_id"], set()).add(ts)
    # Gap que ATRAVESSA a madrugada nao mede atraso do sistema, mede a janela
    # 7-21h funcionando. Sem separar, o KPI ficava vermelho todo dia por
    # construcao: em 01/08, 10 dos 14 intervalos cruzavam a noite e puxavam a
    # mediana de 2,1h pra 18,0h — o mesmo numero que no dia anterior estava
    # verde, so porque ali ainda nao havia virada de dia na amostra. Um KPI que
    # piora sozinho as 21h manda mexer em parametro que nao tem culpa (foi
    # exatamente o erro de rotulo que o bloco 2 passou a evitar em 31/07).
    gaps, gaps_noite = [], []
    for pid, ts in julg.items():
        marcos = sorted(datetime.fromisoformat(t.replace("Z", "+00:00")) for t in ts)
        for a, b in zip(marcos, marcos[1:]):
            h = (b - a).total_seconds() / 3600
            if a.astimezone(BRT).date() != b.astimezone(BRT).date():
                gaps_noite.append(h)
            else:
                gaps.append(h)
    if not gaps and gaps_noite:
        print(f"\n   {len(gaps_noite)} intervalo(s) medidos, TODOS atravessando a madrugada")
        print("   (janela 7-21h). Nao da pra falar de frescor com isso — volte depois")
        print("   de duas rodadas do mesmo dia.")
        return
    if not gaps:
        print("\n   ainda não há dois julgamentos da mesma frente pra comparar.")
        print("   BASELINE conhecido: 1×/dia = até 24h de idade. Em 31/07 a resposta")
        print("   à Glaucia (13:54) levou 2h22 pra chegar ao portão.")
        return
    gaps.sort()
    med = gaps[len(gaps) // 2]
    print(f"\n   intervalos DENTRO do mesmo dia: {len(gaps)} · mediana {med:.1f}h · pior {max(gaps):.1f}h")
    if gaps_noite:
        _n = sorted(gaps_noite)[len(gaps_noite) // 2]
        print(f"   (+{len(gaps_noite)} atravessando a madrugada, mediana {_n:.1f}h — "
              f"e a janela 7-21h, nao atraso; fora da conta)")
    print(f"   REGRA: alvo <2h (o portão acompanha o dia). Baseline diário era ~24h.")
    if med <= 2:
        print(f"   {VERDE} o portão está acompanhando o dia.")
    elif med <= 4:
        print(f"   {AMARELO} mediana {med:.1f}h — melhor que diário, longe de 'ao vivo'.")
    else:
        print(f"   {VERMELHO} mediana {med:.1f}h. Algo está segurando: teto, debounce ou máquina dormindo.")


def bloco_divergencia(runs: list[dict]) -> None:
    _h("5. QUALIDADE — os dois motores discordam? (e o que NÃO dá pra medir aqui)")
    api = [r for r in runs if not r["p"].get("motor")]
    ag = [r for r in runs if r["p"].get("motor") == "agente_local"]
    print(f"   rodadas: API {len(api)} · agente {len(ag)}")
    if api and ag:
        ua, ug = api[-1], ag[-1]
        pa = {h["project_id"] for h in (ua["p"]["placar"]["hoje"] or [])}
        pg = {h["project_id"] for h in (ug["p"]["placar"]["hoje"] or [])}
        print(f"   último portão API    ({ua['run_at']:%d/%m %H:%M}): {sorted(pa)}")
        print(f"   último portão agente ({ug['run_at']:%d/%m %H:%M}): {sorted(pg)}")
        print(f"   em comum: {sorted(pa & pg) or 'nenhum'}")
        print("\n   ⚠️ Divergência NÃO é erro de um dos dois. Em 31/07 os dois acertaram a")
        print("      Vallen: a API às 11:49 (Glaucia tinha acabado de pedir ligação) e o")
        print("      agente às 14:11 (o Renato já tinha respondido). Eram 2h22 de relógio.")
        print("      Antes de culpar um motor, comparar os HORÁRIOS das duas rodadas.")
    bloco_precisao(runs)


def bloco_precisao(runs: list[dict]) -> None:
    """Precisão medida do portão, por parâmetro vigente.

    Até 04/08/2026 este PDCA só media VOLUME — frente barrada, teto estourado —
    porque era o que saía de query. A calibração de 03/08 foi decidida assim, e
    o 1º placar de qualidade mostrou a precisão caindo de 70% pra 33%.
    Otimizamos o observável e pioramos o que importa. Agora o veredito persiste
    em `cos_portao_veredito` e entra aqui ao lado do volume.

    A REGRA DE DECISÃO abaixo foi escrita ANTES de olhar a próxima medição —
    senão vira alvo desenhado em volta do tiro.
    """
    _h("5b. PRECISÃO — dos portões abertos, quantos prestavam?")
    ro = (os.getenv("COS_RO_URL") or "").strip()
    try:
        with _conn(ro) as c, c.cursor() as cur:
            cur.execute("""
                SELECT debounce_min, teto_diario, count(*) AS n,
                       count(*) FILTER (WHERE veredito = 'certa')  AS certas,
                       count(*) FILTER (WHERE veredito = 'errada') AS erradas,
                       count(*) FILTER (WHERE veredito = 'passou') AS passou,
                       min(run_date) AS de, max(run_date) AS ate
                FROM cos_portao_veredito
                GROUP BY 1, 2 ORDER BY max(run_date) DESC
            """)
            linhas = cur.fetchall()
            cur.execute("""
                SELECT frente, count(*) AS n,
                       count(*) FILTER (WHERE veredito = 'errada') AS erradas
                FROM cos_portao_veredito GROUP BY 1
                HAVING count(*) FILTER (WHERE veredito = 'errada') >= 2
                ORDER BY 3 DESC, 2 DESC
            """)
            reincidentes = cur.fetchall()
            # O PORQUÊ, que é onde o aprendizado mora (07/08). O placar diz
            # QUANTOS erraram; só o comentário do Renato diz POR QUE — e sem
            # isso o PDCA sabe que a precisão caiu mas não o que mudar. Os
            # erros vêm primeiro: é onde a correção tem retorno.
            cur.execute("""
                SELECT run_date, frente, veredito, nota
                  FROM cos_portao_veredito
                 WHERE nota IS NOT NULL AND btrim(nota) <> ''
                 ORDER BY (veredito <> 'certa') DESC, run_date DESC
                 LIMIT 12
            """)
            comentarios = cur.fetchall()
    except Exception as e:
        print(f"   (sem leitura da tabela: {e})")
        return

    if not linhas:
        print("   Nenhum veredito ainda. O placar é HTML em ~/cockpit/placar.html —")
        print("   o Renato marca, copia e a sessão grava. Sem isto, este PDCA volta a")
        print("   otimizar só volume, que foi como a precisão caiu sem ninguém ver.")
        return

    # `_conn` usa RealDictCursor — as linhas são dicts, não tuplas. Desempacotar
    # posicionalmente aqui devolvia as CHAVES e estourava no primeiro cálculo.
    print("   deb/teto   portões  certas  erradas  passou  precisão   janela")
    for r in linhas:
        n, certas = r["n"], r["certas"]
        pct = 100.0 * certas / n if n else 0
        cor = VERDE if pct >= 70 else (AMARELO if pct >= 55 else VERMELHO)
        # `debounce_min`/`teto_diario` podem ser NULL: o `--gravar` do
        # `sistema.py` não os preenchia, e formatar None com `:3d` derrubava o
        # bloco INTEIRO — o placar de precisão, que é o gate da decisão dos
        # cockpits, não saía desde 06/08. Um relatório que morre no primeiro
        # None não mede nada; calibração desconhecida vira "?/?" e a linha vive.
        calib = (f"{r['debounce_min']:3d}/{r['teto_diario']:<3d}"
                 if r["debounce_min"] is not None and r["teto_diario"] is not None
                 else "  ?/?  ")
        print(f"   {calib}    {n:5d}   {certas:5d}"
              f"  {r['erradas']:6d}  {r['passou']:5d}"
              f"   {cor} {pct:3.0f}%   {r['de']:%d/%m}-{r['ate']:%d/%m}")

    if reincidentes:
        print("\n   FRENTES QUE ERRARAM 2+ VEZES (candidatas a debounce próprio ou saída da fila):")
        for r in reincidentes:
            print(f"      {r['frente'][:44]:46s} {r['erradas']} erro(s) em {r['n']}")

    if comentarios:
        print("\n   O PORQUÊ — o que o Renato escreveu ao marcar (erros primeiro):")
        for r in comentarios:
            marca = {"certa": "✓", "errada": "✗", "passou": "○"}.get(r["veredito"], "·")
            print(f"      {marca} {r['run_date']:%d/%m} {r['frente'][:34]:36s} {r['nota'][:96]}")
        print("      ↑ é daqui que sai o ajuste. Contagem diz QUANTO; isto diz O QUÊ.")
    else:
        print("\n   (nenhum comentário ainda — os vereditos têm rótulo mas não têm motivo.")
        print("    O campo está no `sistema.html`, embaixo de cada portão; sem ele o PDCA")
        print("    enxerga a precisão cair sem saber o que mudar.)")

    print("\n   REGRA DE DECISÃO (escrita antes da próxima medição):")
    print("      precisão < 55% em ≥8 portões  → REVERTER o último ajuste de parâmetro.")
    print("      55-70%                        → manter e medir mais uma semana.")
    print("      ≥70% em ≥15 portões           → é o piso do caminho A; abre o gate.")
    print("      frente com 2+ erros           → debounce próprio ou tirar da fila.")
    print("\n   ⚠️ 'passou' (falso negativo) é SUB-CONTADO por construção: o Renato julga")
    print("      a lista que a camada mostrou, e o que ela não mostrou não aparece pra")
    print("      ser julgado. Ler como 'dos portões abertos, quantos prestavam' — nunca")
    print("      como 'a camada viu tudo que importava'.")
    print("   ⚠️ NÃO automatizar este veredito. Medido em 04/08: o proxy 'houve movimento")
    print("      na frente?' não prediz (certa 5 × errada 2 com movimento; certa 4 ×")
    print("      errada 5 sem). 4 portões certos não tiveram ação — estavam certos e o")
    print("      Renato só não agiu ainda.")


def bloco_cadastro(runs: list[dict]) -> None:
    """A camada escreveu — e o que foi RECUSADO, com o motivo.

    POR QUE ESTE BLOCO EXISTE. O placar do cadastro passou a ser gravado no
    payload em 11/08 justamente para poder ser lido aqui. Sem leitor ele seria
    dado morto ([[feedback_consumidor_morto_wiring]]): no primeiro dia com
    escrita ligada, 7 das 20 propostas foram recusadas e o motivo só existia no
    stderr da máquina — dava pra contar, não pra consertar.

    A TAXA DE RECUSA É O NÚMERO DA RETRO, não o de escritas. Escrita que passa
    diz que o desenho funciona; recusa repetida diz ONDE ele não funciona — e
    recusa concentrada numa operação é sinal de contrato mal escrito, não de
    agente ruim. Foi assim que `atualizar_fase_frente` se revelou inaplicável.
    """
    _h("6. CADASTRO — a camada escreveu, e o que foi recusado")
    com = [r for r in runs if r["p"].get("cadastro")]
    if not com:
        # `None` em todas ≠ "não escreveu nada": pode ser rodada sem COS_RW_URL,
        # que é o modo degradado. Dizer "0 escritas" aqui esconderia isso.
        print("   nenhuma rodada da janela registrou placar de cadastro")
        print("   (payload sem a chave `cadastro` = rodou antes de 11/08 ou sem COS_RW_URL)")
        return

    esc = sum(c["escritas"] for c in (r["p"]["cadastro"] for r in com))
    duv = sum(c["duvidas"] for c in (r["p"]["cadastro"] for r in com))
    rec = sum(c["recusadas"] for c in (r["p"]["cadastro"] for r in com))
    abertas = sum(c.get("perguntas_abertas") or 0 for c in (r["p"]["cadastro"] for r in com))
    total = esc + duv + rec
    print(f"   {len(com)} rodada(s) com escrita ligada · {total} propostas")
    print(f"   escritas {esc} · dúvidas {duv} (→ {abertas} perguntas) · recusadas {rec}"
          + (f" ({rec / total:.0%})" if total else ""))

    if duv == 0 and esc:
        print("\n   ⚠️ ZERO dúvidas com escrita acontecendo. Ou todo julgamento passou de")
        print("      0,75, ou o piso está mal calibrado — ele é chute fundamentado, não")
        print("      medição, e esta é a retro que existe pra decidir isso.")

    # COLISÃO NA MESMA RODADA (16/08). Duas frentes propondo escrita sobre o
    # mesmo registro: o runner agora fica com a de maior confiança e a divergente
    # vira pergunta. Medir aqui porque o conserto do EXCESSO não é o dedup — é o
    # prompt: se o número subir, são as frentes que estão se sobrepondo, e o
    # dedup só estaria escondendo isso bem.
    sup = sum(c.get("suprimidas") or 0 for c in (r["p"]["cadastro"] for r in com))
    if sup:
        colisoes: dict[str, int] = {}
        divergentes = 0
        for r in com:
            for x in r["p"]["cadastro"].get("colisoes") or []:
                colisoes[f"{x.get('operacao')}#{x.get('registro_id')}"] = \
                    colisoes.get(f"{x.get('operacao')}#{x.get('registro_id')}", 0) + 1
                divergentes += 1 if x.get("divergente") else 0
        print(f"\n   colisões suprimidas {sup} ({divergentes} com conteúdo divergente → viraram pergunta)")
        for alvo, n in sorted(colisoes.items(), key=lambda kv: -kv[1])[:5]:
            print(f"     {n:3d}× {alvo}")
        print("   Alvo repetido na lista é sinal de frentes se sobrepondo no prompt.")

    por_op: dict[str, list[str]] = {}
    for r in com:
        for x in r["p"]["cadastro"].get("recusas") or []:
            por_op.setdefault(x.get("operacao") or "?", []).append(x.get("erro") or "sem motivo")
    if por_op:
        print("\n   recusas por operação (motivo da mais recente):")
        for op, erros in sorted(por_op.items(), key=lambda kv: -len(kv[1])):
            print(f"     {len(erros):3d}× {op}")
            print(f"          {erros[-1][:150]}")
        print("\n   Recusa concentrada numa operação é contrato mal escrito, não agente")
        print("   ruim: se o prompt não ensina o campo, o modelo não tem como preenchê-lo.")


def bloco_board_hunt(ro_url: str) -> None:
    """O convite a criar frente chega a projeto que nunca será frente (#999801).

    POR QUE ESTE BLOCO EXISTE. O `b82d05f` resolveu o bloqueio certo — o agente
    nunca consultava `board_hunt_frentes` — injetando a linha da frente no
    prompt. Mas o bloco injetado é o MESMO texto para projeto SEM frente, e a
    ressalva ("se for originação de conselho de verdade") é prosa dentro do
    parágrafo que insiste. Prosa não é contrato ([[feedback_prompt_nao_le_comentario]]).

    O CUSTO É ASSIMÉTRICO, e é por isso que se mede em vez de confiar: uma
    frente inventada entra em `board_hunt_frentes`, que é a tabela de maior
    autoridade da originação — o board hunt é a renda. Errar para menos custa
    uma frente registrada à mão; errar para mais suja a fonte que decide.

    A REGRA DE DECISÃO FOI FIXADA ANTES DA MEDIÇÃO, na própria #999801: havendo
    QUALQUER frente criada a partir de projeto que não é originação, o bloco
    injetado ganha duas variantes. Medido em 13/08: 34 projetos não-originação
    receberam o convite em 1.524 julgamentos e nasceram ZERO frentes indevidas —
    as duas criadas (Orbiz, sem projeto; Motiva, projeto #64 de originação) são
    legítimas. Por isso a condicional NÃO foi implementada: mudar a regra depois
    de ver o número é o viés que fixá-la antes existe para evitar
    ([[feedback_otimizar_o_mensuravel_erra_o_alvo]]).

    O que este bloco faz é impedir que esse "zero" vire conclusão permanente.
    Exposição sem incidente não é segurança — é sorte ainda não gasta.
    """
    _h("6b. BOARD HUNT — o convite indevido virou frente inventada?")
    with _conn(ro_url) as c, c.cursor() as cur:
        cur.execute("SELECT count(*) n FROM projects WHERE status='ativo'")
        ativos = cur.fetchone()["n"]
        # ⚠️ `ativo`, não `active`. O primeiro script desta medição usou o inglês,
        # casou zero linhas e devolveu "0 projetos ativos" sem erro nenhum
        # ([[feedback_filtro_vocabulario_errado_falha_calado]]).
        cur.execute("""SELECT count(*) n FROM projects
                        WHERE status='ativo' AND lower(nome) LIKE 'originação conselho%'""")
        orig = cur.fetchone()["n"]
        cur.execute("""SELECT w.registro_id, w.valor_novo, w.confianca, w.criado_em,
                              p.nome AS projeto
                         FROM agent_writes w
                         LEFT JOIN projects p
                           ON p.id = (w.valor_novo->>'project_id')::int
                        WHERE w.operacao = 'criar_frente_board_hunt'
                        ORDER BY w.criado_em""")
        criadas = cur.fetchall()

    print(f"   {ativos} projetos ativos · {orig} são de originação → "
          f"**{ativos - orig} recebem o convite sem serem candidatos**")
    if not criadas:
        print("   nenhuma frente criada pela camada até agora.")
        return

    indevidas = []
    for x in criadas:
        vn = x["valor_novo"]
        if isinstance(vn, str):
            vn = json.loads(vn)
        proj = x["projeto"]
        # Sem `project_id` NÃO é falso positivo: a frente pode nascer de uma
        # conversa solta (foi o caso da Orbiz/Pretola, que existia no mundo e
        # não no banco — o defeito que a operação foi criada pra resolver).
        legitima = proj is None or str(proj).lower().startswith("originação conselho")
        marca = "✅" if legitima else "❌ INDEVIDA"
        print(f"     {marca} {str(vn.get('nome'))[:38]:40} ← {str(proj or 'sem projeto')[:38]}")
        if not legitima:
            indevidas.append(vn.get("nome"))

    if indevidas:
        print(f"\n   🔴 {len(indevidas)} frente(s) nasceram de projeto que não é originação.")
        print("      A regra fixada na #999801 dispara AGORA: o bloco injetado precisa de")
        print("      duas variantes — 'esta frente tem fase X, compare' para quem tem frente,")
        print("      e nada para os outros. Custa uma condicional.")
    else:
        print("\n   Nenhuma indevida. O convite chega a quem não deveria, e o modelo tem")
        print("   recusado — mas isso é resultado observado, não garantia de desenho.")


def bloco_ajustes() -> None:
    _h("7. AJUSTES — o que faz disto um ciclo, e não um relatório")
    print(f"   Parâmetros hoje:  DEBOUNCE_MIN={DEBOUNCE_MIN} · TETO_DIARIO={TETO_DIARIO} · "
          f"MAX_POR_RODADA={MAX_POR_RODADA} · janela {HORA_INICIO}-{HORA_FIM}h BRT")
    print("   Histórico: os 3 primeiros nasceram CHUTE em 31/07. Na noite do mesmo dia,")
    print("   TETO_DIARIO 18→28 e MAX_POR_RODADA 3→5 pelo 1º dia de dados (ver backlog).")
    print("   03/08: teto 28→36 e debounce 90→60, por VOLUME.")
    print("   04/08: debounce 60→90 REVERTIDO — o 1º placar mostrou precisão 70%→33%")
    print("          (04/08 fechou 0/3). Teto mantido em 36 de propósito: mexer nos dois")
    print("          ao mesmo tempo torna impossível saber qual moveu o resultado.")
    print("\n   A LIÇÃO que fecha o ciclo: até 04/08 este PDCA só media volume, e foi por")
    print("   volume que a calibração de 03/08 foi decidida. Ajuste guiado só pelo que")
    print("   é fácil medir anda para o lado errado com confiança. Agora todo ajuste de")
    print("   parâmetro tem que olhar o bloco 5b junto — volume diz se CABE, precisão")
    print("   diz se PRESTA.")
    print("\n   Ao mexer em qualquer um, registre no `project_dev_backlog` em UMA linha:")
    print("      data · parâmetro · de → para · o número que motivou · o que se espera")
    print("   Sem isso não dá pra saber se o ajuste SEGUINTE melhorou ou piorou — foi")
    print("   assim que o '71% sai por fora' sobreviveu meses sendo falso.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dias", type=int, default=7)
    a = ap.parse_args()
    ro = (os.getenv("COS_RO_URL") or "").strip()
    if not ro:
        sys.exit("COS_RO_URL ausente — `set -a; . ~/.cos-agent/env; set +a`")
    runs = carregar(ro, a.dias)
    agora = datetime.now(timezone.utc) - timedelta(hours=3)
    print(f"\n╔═ PDCA · camada CoS · últimos {a.dias} dias · {agora:%d/%m %H:%M} BRT ═╗")
    print(f"   {len(runs)} rodadas na janela")
    bloco_consumo(runs)
    bloco_triagem(runs)
    bloco_debounce(runs)
    bloco_frescor(runs)
    bloco_divergencia(runs)
    bloco_cadastro(runs)
    bloco_board_hunt(ro)
    bloco_ajustes()
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
