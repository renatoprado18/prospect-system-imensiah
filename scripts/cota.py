#!/usr/bin/env python3
"""Medidor de cota do plano Claude (Max 5x) — lê os .jsonl das sessões locais.

POR QUE EXISTE (25/09/26): a Anthropic não expõe "quanto da cota você já usou"
por API. O único sinal é bater o teto e o Claude Code parar — que foi o que
aconteceu em 20/09 (522M de tokens numa janela de 5h) e motivou o upgrade
reflexo pro Max 20x, dobrando a conta de R$ 550 pra R$ 1.100. Um número visível
durante o dia é o que transforma "descobri no estouro" em "sabia antes".
Ver [[project_auditoria_custo_25_09]].

O QUE MEDE. O limite do Max é por JANELA ROLANTE DE 5 HORAS, não por dia — por
isso a medida principal aqui é a janela, não o total diário. Dois dias com o
mesmo total se comportam de forma oposta: 200M espalhados em 12h não encostam
no teto; os mesmos 200M em 3h chegam perto. O total diário e o semanal entram
como contexto, não como alarme.

O TETO É EMPÍRICO, não documentado. Calibrado em UM estouro observado (20/09,
522M) mais um dia que quase certamente bateu (08/09, 503M). Trate como ordem de
grandeza: a faixa amarela existe justamente porque o número exato é incerto. Se
você estourar num valor bem diferente de 500M, corrija TETO_5H aqui — o medidor
que não se recalibra vira decoração.

CUSTO ZERO DE TOKENS: é aritmética sobre arquivos locais, nenhuma chamada de
LLM. O cache incremental (só lê o que cresceu desde a última vez) existe porque
isto roda na statusline a cada render, sobre ~340 arquivos que somam centenas
de MB — reler tudo travaria a digitação.

Uso:
    python3 scripts/cota.py            # uma linha, pra statusline
    python3 scripts/cota.py --full     # relatório com histórico
    python3 scripts/cota.py --json     # pra outro programa consumir
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Teto empírico da janela de 5h no Max 5x (ver docstring). O Pro é 1/5 disto e
# o Max 20x, 4x — se o plano mudar, é esta linha que muda.
TETO_5H = 500_000_000
JANELA_S = 5 * 3600

# Faixas: verde até 50% (dia normal — a mediana medida é ~200M, 40%), amarelo
# até 80% (dá pra terminar o que está fazendo), vermelho acima (pare de abrir
# frente nova). Os cortes são largos de propósito: o teto é estimado.
VERDE, AMARELO = 0.50, 0.80

BRT = ZoneInfo("America/Sao_Paulo")
PROJETOS = os.path.expanduser("~/.claude/projects")
ESTADO = os.path.expanduser("~/.claude/cota_state.json")

# Retenção do cache de eventos: a janela é de 5h, mas guardamos 9h pra que o
# --full mostre a curva recente e pra tolerar relógio/timezone torto sem perder
# evento que ainda conta.
RETENCAO_S = 9 * 3600

# Janela do histórico diário — só usada na primeira execução (backfill) e para
# podar `days`. Ver a nota em atualiza().
HISTORICO_S = 30 * 86400


def _carrega_estado():
    try:
        with open(ESTADO) as f:
            e = json.load(f)
        # Um estado de versão antiga (ou corrompido) não pode derrubar a
        # statusline nem, pior, reportar número errado com cara de certo.
        if e.get("v") != 1:
            raise ValueError("versão diferente")
        return e
    except Exception:
        return {"v": 1, "files": {}, "events": [], "days": {}}


def _salva_estado(e):
    try:
        tmp = ESTADO + ".tmp"
        with open(tmp, "w") as f:
            json.dump(e, f)
        os.replace(tmp, ESTADO)  # atômico: statusline nunca lê meio arquivo
    except Exception:
        pass  # telemetria não pode quebrar o chamador


def _tokens(usage):
    """Tudo que o plano contabiliza. Cache read domina o volume (~83% do medido
    em 7 dias), mas somar só ele subestimaria dias de output pesado."""
    return (
        int(usage.get("input_tokens") or 0)
        + int(usage.get("output_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0)
    )


def atualiza(estado, permitir_backfill=False):
    """Lê só o que cresceu em cada .jsonl desde a última execução.

    BACKFILL NA PRIMEIRA VEZ. A janela de 5h só precisa de arquivos tocados nas
    últimas 9h, mas o histórico diário precisa de 30. Sem esta distinção o
    relatório saía subcontado com cara de completo — 20/09 aparecia como 386M
    quando a medição independente deu 522M de pico. Um número parcial
    apresentado como total é pior que não mostrar número nenhum, então a
    primeira execução varre 30 dias e as seguintes só o delta.
    """
    agora = time.time()
    # O backfill custa ~4,7s e a statusline roda a cada render — então ele NUNCA
    # acontece ali. Quem o dispara é `--full`/`--backfill`, que o usuário chamou
    # e pode esperar. A métrica principal (janela de 5h) não depende dele: 9h de
    # arquivos bastam. O que depende é o histórico diário, e enquanto ele não
    # rodou o número de 7 dias é OMITIDO em vez de sair subcontado — ver
    # `calcula`. Um "7d" parcial com cara de total é o defeito que isto evita.
    faz_backfill = permitir_backfill and not estado.get("backfilled")
    corte_mtime = agora - (HISTORICO_S if faz_backfill else RETENCAO_S)
    vistos = set()

    try:
        entradas = os.scandir(PROJETOS)
    except OSError:
        return estado

    for proj in entradas:
        if not proj.is_dir():
            continue
        try:
            arquivos = os.scandir(proj.path)
        except OSError:
            continue
        for arq in arquivos:
            if not arq.name.endswith(".jsonl"):
                continue
            try:
                st = arq.stat()
            except OSError:
                continue
            # Arquivo parado há mais que a retenção não tem evento que ainda
            # conte pra janela. Pular aqui é o que mantém isto barato.
            if st.st_mtime < corte_mtime:
                continue
            path = arq.path
            vistos.add(path)
            info = estado["files"].get(path) or {"offset": 0}
            offset = info.get("offset", 0)
            # Arquivo encolheu = rotacionado/reescrito. Reler do zero, senão
            # leríamos lixo a partir de um offset que não existe mais.
            if st.st_size < offset:
                offset = 0
            if st.st_size == offset:
                continue
            try:
                with open(path, errors="ignore") as f:
                    f.seek(offset)
                    for linha in f:
                        if '"usage"' not in linha:
                            continue
                        try:
                            d = json.loads(linha)
                        except Exception:
                            continue
                        msg = d.get("message") or {}
                        u = msg.get("usage") or {}
                        ts = d.get("timestamp") or ""
                        if not u or not ts:
                            continue
                        try:
                            t = datetime.fromisoformat(
                                ts.replace("Z", "+00:00")).timestamp()
                        except ValueError:
                            continue
                        n = _tokens(u)
                        if n <= 0:
                            continue
                        estado["events"].append([t, n])
                        dia = datetime.fromtimestamp(t, BRT).strftime("%Y-%m-%d")
                        estado["days"][dia] = estado["days"].get(dia, 0) + n
                    novo_offset = f.tell()
                estado["files"][path] = {"offset": novo_offset}
            except OSError:
                continue

    if faz_backfill:
        estado["backfilled"] = True

    # Poda: eventos velhos. Sem isto o estado cresce sem teto e lê-lo passa a
    # custar o que economizamos.
    estado["events"] = [e for e in estado["events"] if e[0] > agora - RETENCAO_S]
    estado["events"].sort(key=lambda e: e[0])

    # Os OFFSETS, ao contrário, só saem quando o arquivo some do disco. A versão
    # anterior descartava todo arquivo não visto nesta passada — e como a
    # varredura normal só olha 9h, um arquivo de ontem era esquecido e, se
    # voltasse a ser tocado, seria relido do offset 0 e contado DE NOVO em
    # `days`. O número inflaria sozinho, sem erro visível.
    for p in list(estado["files"]):
        if p not in vistos and not os.path.exists(p):
            del estado["files"][p]
    limite_dia = (datetime.now(BRT) - timedelta(days=30)).strftime("%Y-%m-%d")
    estado["days"] = {d: v for d, v in estado["days"].items() if d >= limite_dia}
    return estado


def calcula(estado):
    agora = time.time()
    inicio = agora - JANELA_S
    na_janela = [e for e in estado["events"] if e[0] > inicio]
    usado = sum(n for _, n in na_janela)

    # Quando a janela alivia: quando o evento mais antigo dela sai, aquele
    # volume é devolvido. É a informação acionável — "espera até X e pode
    # retomar" em vez de só "você está no vermelho".
    alivia_em = None
    devolve = 0
    if na_janela and usado > TETO_5H * VERDE:
        t0, devolve = na_janela[0]
        alivia_em = t0 + JANELA_S

    hoje = datetime.now(BRT).strftime("%Y-%m-%d")
    # None (não zero) enquanto o backfill não rodou: zero seria um número, e um
    # número errado é pior que a ausência dele.
    semana = None if not estado.get("backfilled") else sum(
        v for d, v in estado["days"].items()
        if d > (datetime.now(BRT) - timedelta(days=7)).strftime("%Y-%m-%d")
    )
    return {
        "janela_5h": usado,
        "pct": usado / TETO_5H if TETO_5H else 0,
        "teto": TETO_5H,
        "hoje": estado["days"].get(hoje, 0),
        "semana_7d": semana,
        "alivia_em": alivia_em,
        "alivia_devolve": devolve,
        "eventos_na_janela": len(na_janela),
    }


def _m(n):
    if n >= 1_000_000_000:
        return f"{n/1e9:.1f}G"
    return f"{n/1e6:.0f}M"


def linha_status(c):
    pct = c["pct"]
    cor = "\033[32m" if pct < VERDE else ("\033[33m" if pct < AMARELO else "\033[31m")
    blocos = min(10, int(pct * 10))
    barra = "▓" * blocos + "░" * (10 - blocos)
    txt = f"{cor}{barra} {_m(c['janela_5h'])}/{_m(c['teto'])}\033[0m"
    if c["alivia_em"]:
        hh = datetime.fromtimestamp(c["alivia_em"], BRT).strftime("%H:%M")
        txt += f" \033[2m↓{_m(c['alivia_devolve'])} {hh}\033[0m"
    if c["semana_7d"] is not None:
        txt += f" \033[2m· 7d {_m(c['semana_7d'])}\033[0m"
    return txt


def relatorio(estado, c):
    pct = c["pct"]
    print("\n  \033[1mCota Claude — Max 5x\033[0m")
    print(f"  {linha_status(c)}\n")
    print(f"  Janela de 5h   {_m(c['janela_5h']):>8}  ({pct*100:.0f}% do teto estimado)")
    print(f"  Hoje (BRT)     {_m(c['hoje']):>8}")
    sem = _m(c["semana_7d"]) if c["semana_7d"] is not None else "—"
    print(f"  Últimos 7 dias {sem:>8}")
    if c["alivia_em"]:
        hh = datetime.fromtimestamp(c["alivia_em"], BRT).strftime("%H:%M")
        print(f"\n  Alivia {_m(c['alivia_devolve'])} às {hh}, quando o evento mais "
              f"antigo sai da janela.")
    if pct >= AMARELO:
        print("\n  \033[31mZona vermelha.\033[0m Termine o que está aberto; evite "
              "abrir frente nova ou rodar suíte grande agora.")
    elif pct >= VERDE:
        print("\n  \033[33mMetade do teto.\033[0m Dá pra seguir — só não empilhe "
              "duas frentes pesadas na mesma janela.")

    dias = sorted(estado["days"].items())[-14:]
    if dias:
        print("\n  \033[1mTotal por dia\033[0m \033[2m(o teto é por janela de 5h, "
              "não por dia — isto é contexto)\033[0m")
        pico = max(v for _, v in dias) or 1
        for d, v in dias:
            dt = datetime.strptime(d, "%Y-%m-%d")
            barra = "█" * int(v / pico * 32)
            print(f"  {dt.strftime('%d/%m %a')[:9]:<10}{_m(v):>7}  {barra}")
    print("\n  \033[2mTeto empírico: estourou em 522M (20/09). Se estourar num "
          "valor bem diferente, corrija TETO_5H em scripts/cota.py.\033[0m\n")


def main():
    # Nada aqui pode derrubar a statusline: erro = linha vazia, não stacktrace.
    try:
        quero_hist = ("--full" in sys.argv) or ("--backfill" in sys.argv)
        estado = atualiza(_carrega_estado(), permitir_backfill=quero_hist)
        _salva_estado(estado)
        c = calcula(estado)
    except Exception as e:
        if "--debug" in sys.argv:
            raise
        print("", end="")
        return 0

    if "--json" in sys.argv:
        print(json.dumps(c))
    elif "--full" in sys.argv:
        relatorio(estado, c)
    else:
        print(linha_status(c))
    return 0


if __name__ == "__main__":
    sys.exit(main())
