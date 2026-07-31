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
from run import _conn, DEBOUNCE_MIN, TETO_DIARIO, HORA_INICIO, HORA_FIM  # noqa: E402

VERDE, AMARELO, VERMELHO = "🟢", "🟡", "🔴"


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
    for r in hor:
        for k, v in ((r["p"]["triagem"].get("nao_julgadas") or {})).items():
            if not k.startswith("_") and isinstance(v, int):
                mot[k] = mot.get(k, 0) + v
    print(f"   rodadas: {total} · sem nada a julgar: {vazias} ({100*vazias/total:.0f}%)")
    print("   por que as frentes NÃO foram julgadas (somado):")
    for k, v in sorted(mot.items(), key=lambda kv: -kv[1]):
        print(f"      {k:<22} {v}")
    print(f"\n   REGRA: rodada vazia é BARATA (só SQL) — vazio não é desperdício.")
    if total and vazias / total > 0.8:
        print(f"   {AMARELO} >80% vazias. Não custa, mas 14 disparos/dia pra 2 úteis sugere")
        print("      espaçar (de 2 em 2h) sem perder nada. Decisão de elegância, não de custo.")
    elif mot.get("cortada_pelo_teto", 0) > 0:
        print(f"   {VERMELHO} {mot['cortada_pelo_teto']} frente(s) ficaram de fora POR TETO —")
        print("      isso é julgamento que deixou de acontecer. Ver bloco 1.")
    else:
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
    gaps = []
    for pid, ts in julg.items():
        marcos = sorted(datetime.fromisoformat(t.replace("Z", "+00:00")) for t in ts)
        gaps += [(b - a).total_seconds() / 3600 for a, b in zip(marcos, marcos[1:])]
    if not gaps:
        print("\n   ainda não há dois julgamentos da mesma frente pra comparar.")
        print("   BASELINE conhecido: 1×/dia = até 24h de idade. Em 31/07 a resposta")
        print("   à Glaucia (13:54) levou 2h22 pra chegar ao portão.")
        return
    gaps.sort()
    med = gaps[len(gaps) // 2]
    print(f"\n   intervalos medidos: {len(gaps)} · mediana {med:.1f}h · pior {max(gaps):.1f}h")
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
    print("\n   REGRA: qualidade não sai de query. O placar é manual e só o Renato preenche:")
    print("      acertou · cobrou à toa · deixou passar")
    print("   Parcial registrado em 30/07: 2 certas, 1 errada (a acusação sobre o")
    print("   cadastro da Dra. Wanelise, que estava correto).")


def bloco_ajustes() -> None:
    _h("6. AJUSTES — o que faz disto um ciclo, e não um relatório")
    print(f"   Parâmetros hoje:  DEBOUNCE_MIN={DEBOUNCE_MIN} · TETO_DIARIO={TETO_DIARIO} · "
          f"janela {HORA_INICIO}-{HORA_FIM}h BRT")
    print("   Todos foram CHUTE fundamentado em 31/07, não medição.")
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
    bloco_ajustes()
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
