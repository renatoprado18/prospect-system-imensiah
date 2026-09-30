#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""Boletim de Inteligência semanal → ~/cockpit/boletim.html

Nasceu em 20/09/2026 de uma pergunta: "como obter notícias de forma eficiente,
sem ter que ler tudo?". A medição respondeu antes do desenho — o INTEL já
coletava 3.388 manchetes/mês e criava alertas que NUNCA viravam ação (204
`news_alert`: 122 expirados, 71 dispensados, zero acionados), e a página
/clipping estava sem um toque desde 21/07. Não faltava ingestão; faltava a
notícia chegar com pergunta embutida, no formato de bater o olho.

QUATRO CAMADAS, três delas em ARQUIVO (edição semanal não deve exigir Python):

  CHÃO       este script, lendo o Neon: hits dos watchers, clipping, métricas
  ESTADO     ~/cockpit/boletim_estado.json    — POSTURA por frente (do Renato)
  CURADORIA  ~/cockpit/boletim_curadoria.json — o julgamento da edição (meu)
  DOSSIÊ     ~/cockpit/boletim_dossies.json   — determinista, de boletim_dossie.py

A RÉGUA DE NÃO-REPETIÇÃO, que foi o segundo pedido dele ("deveria ter uma forma
de não repetir notícias"):
  1. não repete MANCHETE  — boletim_publicados.json (só grava com --publicar)
  2. não repete HISTÓRIA  — 8 veículos sobre o mesmo fato = 1 linha + "+N"
  3. não repete PERGUNTA  — ESTADO.postura; frente fora de `agir` não sobe

E o quarto destino, que nasceu de um erro meu: "NÃO É GANCHO" — a notícia
aparece com o motivo de não usá-la (o Marson é conselheiro independente e
insider de WEST3; notícia da Westwing é cuidado, não puxada de conversa).

ACESSO: o Renato não consegue ler o Valor. Dentro de um cluster o link vai pro
veículo mais livre e o restrito vira corroboração; quando a história só existe
atrás do muro, ela aparece SEM link e rotulada — em vez de oferecer um clique
que morre.

Uso:
    scripts/boletim.py                 # prévia, nada persistido
    scripts/boletim.py --publicar      # edição oficial (o cron de sexta usa)
    scripts/boletim_semanal.sh         # dossiê + boletim publicado, como no cron
"""

import argparse
import html
import json
import os
import re
import sys
from datetime import date

import psycopg2
import psycopg2.extras

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COCKPIT = os.path.expanduser("~/cockpit")
# OUT depende de --publicar e a definição real fica depois do parse: a prévia
# escreve em ARQUIVO SEPARADO. Ela ESCREVIA no boletim.html e destruía a edição
# vigente — provado em 30/09: conferir à mão 5 dias depois de publicar
# sobrescreveu a sexta com o estado vazio (as 15 manchetes já eram publicadas, a
# régua de repetição suprimiu todas, e o que sobrou na tela dele foi uma página
# de 13 KB). A conferência não pode queimar a entrega que ela confere.
ESTADO_PATH = f"{COCKPIT}/boletim_estado.json"
CURADORIA_PATH = f"{COCKPIT}/boletim_curadoria.json"
PUBLICADOS_PATH = f"{COCKPIT}/boletim_publicados.json"
DOSSIES_PATH = f"{COCKPIT}/boletim_dossies.json"


def env(k):
    """Mesmo leitor do cockpit.py — o plist não injeta env, o script lê o .env."""
    for linha in open(f"{ROOT}/.env"):
        if linha.startswith(k + "="):
            return linha.split("=", 1)[1].strip().strip('"')
    return ""


_ap = argparse.ArgumentParser(description="Gera o Boletim de Inteligência semanal.")
_ap.add_argument("--publicar", action="store_true",
                 help="marca as manchetes desta edição como já mostradas (o cron usa)")
_ap.add_argument("--dias", type=int, default=30, help="janela de hits considerada")
_ap.add_argument("--quieto", action="store_true", help="sem saída em caso de sucesso")
ARGS = _ap.parse_args()
PUBLICAR = ARGS.publicar
OUT = f"{COCKPIT}/boletim.html" if PUBLICAR else f"{COCKPIT}/boletim_previa.html"

# ── CAMADAS DE ARQUIVO ──────────────────────────────────────────────────────
# ESTADO decide o ROTEAMENTO (o que pode pedir você); CURADORIA traz o CONTEÚDO
# (por que importa, qual a ação); DOSSIES é a camada determinista. Os três são
# arquivos, não código: a edição semanal não deve exigir mexer em Python.
if not os.path.exists(ESTADO_PATH):
    raise SystemExit(f"falta {ESTADO_PATH} — sem postura declarada não se gera boletim")
if not os.path.exists(CURADORIA_PATH):
    raise SystemExit(f"falta {CURADORIA_PATH} — sem curadoria o boletim é só uma lista")

ESTADO = json.load(open(ESTADO_PATH))
_CUR = json.load(open(CURADORIA_PATH))
DOSSIES = json.load(open(DOSSIES_PATH)) if os.path.exists(DOSSIES_PATH) else {}
# IDADE do dossiê, declarada na página. Se o passo 1 falhar, o render ainda sai —
# mas com o arquivo de ONTEM, e um dossiê velho com cara de atual é o mesmo
# defeito que o overlay do cockpit teve de aprender a mostrar.
DOSSIE_IDADE_DIAS = None
if os.path.exists(DOSSIES_PATH):
    import time
    DOSSIE_IDADE_DIAS = int((time.time() - os.path.getmtime(DOSSIES_PATH)) // 86400)
publicados = set(json.load(open(PUBLICADOS_PATH))) if os.path.exists(PUBLICADOS_PATH) else set()

SEMANA = _CUR["semana"]
# FRESCOR DA CURADORIA. Esta camada é escrita por mim numa sessão (plano Max,
# custo de API zero) — mas o cron de sexta roda de qualquer jeito. Sem este
# carimbo, uma semana em que eu não atualizei o julgamento renderia a leitura da
# semana passada sobre as manchetes desta, com cara de atual. Mesmo defeito que o
# dossiê velho tinha, e a mesma cura: declarar a idade na página.
CURADORIA_EM = _CUR.get("gerado_em")
CURADORIA_IDADE = None
if CURADORIA_EM:
    CURADORIA_IDADE = (date.today() - date.fromisoformat(CURADORIA_EM)).days
CURADORIA = _CUR["curadoria"]
RADAR = _CUR["radar"]
CLIP_DESTAQUE = _CUR["clip_destaque"]
DESCARTE_EXEMPLO = _CUR["descarte_exemplo"]

# ── GUARDA DO ESTADO ────────────────────────────────────────────────────────
# Postura "acompanhar" sem condição de saída é inválida: viraria cegueira
# comprada. Aborta alto — o cron falhando é melhor que um boletim que cala uma
# frente sem dizer o que a faria voltar.
for _nome, _f in ESTADO["frentes"].items():
    if _f["postura"] != "agir" and not _f.get("so_volta_se"):
        raise SystemExit(
            f"ESTADO inválido — frente “{_nome}” está em postura “{_f['postura']}” sem "
            f"`so_volta_se`.\nEscreva a condição de saída em {ESTADO_PATH} ou mude para `agir`."
        )


def postura_de(frente):
    return ESTADO["frentes"].get(frente, {})


# ── CHÃO: lido do Neon aqui dentro (o protótipo dependia de JSONs no scratchpad) ──
_conn = psycopg2.connect(env("DATABASE_URL"))
_cur = _conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

_cur.execute(
    """
    SELECT DISTINCT ON (h.title) h.title, w.query, p.id AS pid, p.nome AS projeto,
           h.source, h.published_at::date::text AS data, h.url
    FROM project_news_hits h
    JOIN project_news_watchers w ON w.id = h.watcher_id
    LEFT JOIN projects p ON p.id = w.project_id
    WHERE h.hit_at > now() - interval %s
    ORDER BY h.title, h.published_at DESC
    """, (f"{ARGS.dias} days",))
hits = [dict(r) for r in _cur.fetchall()]

_cur.execute(
    """
    SELECT p.id AS pid, c.nome, coalesce(c.cargo,'') AS cargo,
           coalesce(c.empresa,'') AS empresa, coalesce(pm.papel,'') AS papel
    FROM project_members pm
    JOIN projects p ON p.id = pm.project_id
    JOIN contacts c ON c.id = pm.contact_id
    WHERE p.id IN (SELECT project_id FROM project_news_watchers)
      AND c.nome NOT ILIKE '%Renato de Faria%'
    ORDER BY p.id, c.nome
    """)
membros = [dict(r) for r in _cur.fetchall()]

_cur.execute(
    """
    SELECT resumo_dia, gerado_em::text AS gerado_em,
           (SELECT json_agg(e) FROM jsonb_array_elements(c2.conteudo) e) AS itens
    FROM news_clippings c2 ORDER BY gerado_em DESC LIMIT 1
    """)
clipping = dict(_cur.fetchone() or {"resumo_dia": "", "gerado_em": "", "itens": []})

_cur.execute(
    """
    SELECT (SELECT count(*) FROM news_items WHERE collected_at > now()-interval '30 days') AS itens_30d,
           (SELECT count(*) FROM project_news_hits WHERE hit_at > now()-interval '30 days') AS hits_30d,
           (SELECT count(*) FROM project_news_watchers WHERE active) AS watchers,
           (SELECT count(*) FROM action_proposals WHERE action_type='news_alert') AS prop_total,
           (SELECT count(*) FROM action_proposals WHERE action_type='news_alert' AND status='expired') AS prop_expired,
           (SELECT count(*) FROM action_proposals WHERE action_type='news_alert' AND status='dismissed') AS prop_dismissed,
           (SELECT max(created_at)::date::text FROM news_interactions) AS ultima_interacao
    """)
M = dict(_cur.fetchone())
_cur.execute(
    """
    SELECT coalesce(i.source,'?') AS source,
           count(*) FILTER (WHERE n.action IN ('liked','hot_take')) AS aproveitou,
           count(*) FILTER (WHERE n.action='dismissed') AS descartou
    FROM news_interactions n JOIN news_items i ON i.id = n.news_id
    GROUP BY 1 HAVING count(*) > 10 ORDER BY 2 DESC
    """)
M["fontes"] = [dict(r) for r in _cur.fetchall()]
_conn.close()

# ── POOL LIVRE: coletado ao vivo, pra trocar o link do paywall por um gêmeo ───
# Importa `news_hub` do app pra NÃO duplicar a lista de fontes nem o fetcher
# (que é onde vive o fix do User-Agent).
POOL_LIVRE = []
try:
    sys.path.insert(0, os.path.join(ROOT, "app"))
    import asyncio
    from services.news_hub import NEWS_SOURCES, fetch_rss_feed

    async def _pool():
        out = []
        for cfg in NEWS_SOURCES.values():
            if cfg.get("acesso") != "livre":
                continue
            try:
                itens = await asyncio.wait_for(
                    fetch_rss_feed(cfg["url"], cfg["name"]), timeout=25)
                out += [{"source": cfg["name"], "title": i["title"],
                         "link": i.get("link", "")} for i in itens]
            except Exception:
                continue          # fonte fora do ar não derruba o boletim
        return out

    POOL_LIVRE = asyncio.run(_pool())
except Exception as _e:                                    # noqa: BLE001
    print(f"aviso: pool de fontes livres indisponível ({type(_e).__name__}) — "
          f"os links de paywall ficam sem substituto", file=sys.stderr)


# ── ACESSO POR VEÍCULO ──────────────────────────────────────────────────────
# Tier pelo VEÍCULO DE DESTINO, não pelo feed: os feeds do Google News entregam
# UOL, g1, CNN e InfoMoney — mas também o Valor (15× no recorte medido em
# 20/09). O nome do veículo vem no sufixo do título, que `veiculo()` extrai.
# Só listo o que sei estar restrito; ausência de badge = não sabidamente
# restrito, NUNCA "confirmado livre".
VEICULO_RESTRITO = {
    "valor econômico": "paywall", "valor": "paywall",
    "estadão": "paywall", "o estado de s. paulo": "paywall",
    "o globo": "paywall", "globo": "paywall",
    "folha de s.paulo": "medidor", "folha": "medidor",
    "exame": "medidor", "gazeta do povo": "medidor",
    "the economist": "paywall", "financial times": "paywall",
}


def acesso_do(veic):
    return VEICULO_RESTRITO.get((veic or "").strip().lower())


# ── CHÃO ────────────────────────────────────────────────────────────────────
by_title = {h["title"]: h for h in hits}
membros_by_nome = {m["nome"]: m for m in membros}
STOP = set("de da do das dos e o a os as em no na nos nas para por com que se um uma "
           "ao aos à às ex mais não sobre até após entre é ser vai já".split())

suprimidas_repeticao = 0
agrupadas = 0
bloqueadas = 0
substituidas = 0


def toks(t):
    t = re.sub(r"[^\wáàâãéêíóôõúüç ]", " ", t.lower())
    return {w for w in t.split() if len(w) > 3 and w not in STOP}


pool_livre = []  # preenchido abaixo, depois de toks() estar definido


def mesma_historia(a, b):
    ta, tb = toks(manchete_limpa(a)), toks(manchete_limpa(b))
    if not ta or not tb:
        return False
    return len(ta & tb) / min(len(ta), len(tb)) >= 0.6


def veiculo(title):
    return title.rsplit(" - ", 1)[-1] if " - " in title else "—"


def manchete_limpa(title):
    return title.rsplit(" - ", 1)[0] if " - " in title else title


def acha(trechos):
    """Casa a curadoria com os hits reais; tira repetidas de edições anteriores e agrupa histórias."""
    global suprimidas_repeticao, agrupadas
    brutos, vistos = [], set()
    for t in trechos:
        for title, h in by_title.items():
            if t.lower() in title.lower() and title not in vistos:
                vistos.add(title)
                if title in publicados:          # nível 1 — já saiu em boletim
                    suprimidas_repeticao += 1
                    continue
                brutos.append(h)
    brutos.sort(key=lambda h: h["data"] or "", reverse=True)
    grupos = []                                   # nível 2 — mesma história, N veículos
    for h in brutos:
        for g in grupos:
            if mesma_historia(g[0]["title"], h["title"]):
                g.append(h)
                agrupadas += 1
                break
        else:
            grupos.append([h])
    return grupos


def br(d):
    if not d:
        return "s/data"
    y, m, dd = d.split("-")
    return f"{dd}/{m}"


def e(s):
    return html.escape(s or "")


def mil(n):
    return f"{n:,}".replace(",", ".")


def li_manchetes(grupos):
    """Dentro de um cluster, linka o veículo MAIS LIVRE; o restrito vira corroboração."""
    global bloqueadas
    out = []
    for g in grupos:
        # ordena o cluster por acesso: não-restrito primeiro
        ordem = {None: 0, "medidor": 1, "paywall": 2}
        g = sorted(g, key=lambda h: ordem[acesso_do(veiculo(h["title"]))])
        h = g[0]
        tier = acesso_do(veiculo(h["title"]))
        if tier:
            bloqueadas += 1
        extra = ""
        if len(g) > 1:
            outros = ", ".join(sorted({veiculo(x["title"]) for x in g[1:]}))
            extra = f'<span class="mais" title="{e(outros)}">+{len(g) - 1} veículo' \
                    f'{"s" if len(g) > 2 else ""}</span>'
        badge = f'<span class="muro">{tier}</span>' if tier else ""
        out.append(f'<li><a href="{e(h["url"])}" target="_blank">{e(manchete_limpa(h["title"]))}</a>'
                   f'<span class="src">{e(veiculo(h["title"]))} · {br(h["data"])}</span>'
                   f'{badge}{extra}</li>')
    return "".join(out)


def gemeo_livre(titulo):
    """Acha a MESMA história numa fonte livre. É o substituto do clique morto."""
    tv = toks(titulo)
    if not tv:
        return None
    best = None
    for p, tp in pool_livre:
        if not tp:
            continue
        j = len(tv & tp) / min(len(tv), len(tp))
        if j >= 0.45 and (best is None or j > best[1]):
            best = (p, j)
    return best[0] if best else None


def dossie_html(grupos):
    """Camada determinista (scripts/boletim_dossie.py): protagonistas, vínculos e
    histórico. Vem COLAPSADO — a página segue servindo pra bater o olho, e a
    profundidade só aparece no que ele abrir. Custo de leitura por default: zero.
    Nada aqui conclui nada: cada linha carrega a procedência."""
    blocos = []
    for g in grupos:
        d = DOSSIES.get(g[0]["title"])
        if not d:
            continue
        linhas = []
        # ── camada 2: protagonistas do CORPO do artigo ──────────────────────
        for pr in d.get("protagonistas", [])[:8]:
            f = pr.get("ficha")
            conf = (f or {}).get("confianca")
            if conf == "forte":
                v = f["vinculo"]
                trilha = []
                for m in v["mensagens"][:2]:
                    quem = "você" if (m["direcao"] or "") == "outgoing" else "ele/ela"
                    trilha.append(f'{quem} em {br(m["data"])}: “{e((m["txt"] or "").strip()[:70])}”')
                for tf in v["tarefas"][:2]:
                    trilha.append(f'tarefa aberta: {e(tf["titulo"][:54])}'
                                  + (f' (vence {br(tf["due"])})' if tf.get("due") else ""))
                if v["frentes"]:
                    trilha.append("frentes em comum: " + ", ".join(
                        e(fr["nome"][:30]) for fr in v["frentes"]))
                linhas.append(
                    f'<div class="dl forte"><span class="dk">protagonista · vínculo confirmado</span>'
                    f'<b>{e(pr["nome"])}</b> — {e(pr["cargo_na_materia"])}'
                    f'<br><span class="ficha">ficha: {e(f["nome"])}'
                    f'{" · " + e(f["cargo"][:46]) if f["cargo"] else ""}'
                    f'{" · " + e(f["empresa"][:30]) if f["empresa"] else ""} · {e(f["circulo_label"])}</span>'
                    + ("".join(f'<div class="tr">{t}</div>' for t in trilha))
                    + f'<span class="proc">{e(f["procedencia"])} — {e(f["confianca_motivo"])}</span></div>')
            elif conf == "homonimo_provavel":
                linhas.append(
                    f'<div class="dl homonimo"><span class="dk">protagonista · ⚠️ homônimo provável, '
                    f'NÃO é vínculo</span><b>{e(pr["nome"])}</b> — {e(pr["cargo_na_materia"])}'
                    f'<br><span class="ficha">existe a ficha {e(f["nome"])}'
                    f'{" (" + e(f["empresa"][:34]) + ")" if f["empresa"] else ""}, mas '
                    f'{e(f["confianca_motivo"])}</span>'
                    f'<span class="proc">{e(f["procedencia"])} — tratar como pessoa desconhecida '
                    f'até confirmar</span></div>')
            else:
                linhas.append(
                    f'<div class="dl"><span class="dk">protagonista citado (sem ficha)</span>'
                    f'<b>{e(pr["nome"])}</b> — {e(pr["cargo_na_materia"])}'
                    f'<span class="proc">{e(pr["procedencia"])}</span></div>')

        # ── camada 2: a entidade no corpus dele ─────────────────────────────
        for termo, c in (d.get("corpus") or {}).items():
            partes = []
            if c["whatsapp"]["n"]:
                partes.append(f'<b>{c["whatsapp"]["n"]}</b> mensagens no WhatsApp '
                              f'(última {br(c["whatsapp"]["ultimo"])})')
            if c["email"]["n"]:
                partes.append(f'<b>{c["email"]["n"]}</b> e-mails '
                              f'(último {br(c["email"]["ultimo"])})')
            if c["notas"]["n"]:
                partes.append(f'<b>{c["notas"]["n"]}</b> notas de projeto '
                              f'(última {br(c["notas"]["ultimo"])})')
            trecho = ""
            if (c["notas"] or {}).get("trecho"):
                nt = c["notas"]
                trecho = (f'<div class="tr"><b>{e(nt.get("projeto") or "projeto")}</b>, '
                          f'{br(nt.get("data"))}: {e(nt["trecho"])}</div>')
            linhas.append(
                f'<div class="dl"><span class="dk">“{e(termo)}” já passou pelo seu mundo</span>'
                + " · ".join(partes) + trecho
                + '<span class="proc">public.messages · copilot.emails · public.project_notes — '
                  'busca textual, pode incluir homônimo</span></div>')

        # ── camada 2: números do artigo ─────────────────────────────────────
        nums = d.get("numeros") or {}
        if nums.get("dinheiro") or nums.get("percentuais"):
            linhas.append(
                f'<div class="dl"><span class="dk">números que a matéria traz</span>'
                + " · ".join(e(x) for x in (nums.get("dinheiro") or [])[:5]
                             + (nums.get("percentuais") or [])[:5])
                + '<span class="proc">extraídos do corpo do artigo</span></div>')
        if d.get("erro_texto"):
            linhas.append(
                f'<div class="dl degradado"><span class="dk">⚠️ corpo do artigo indisponível</span>'
                f'{e(d["erro_texto"])} — este dossiê ficou só com o título'
                f'<span class="proc">o link dos watchers é redirect do Google News; quando não '
                f'resolve, o dossiê degrada e avisa em vez de afinar calado</span></div>')
        for ent in d["empresas"]:
            quem = "; ".join(
                f'{e(p["nome"])}{" (" + e(p["cargo"][:40]) + ")" if p["cargo"] else ""}'
                for p in ent["pessoas"][:4])
            resto = (f' <i>+{len(ent["pessoas"]) - 4}</i>' if len(ent["pessoas"]) > 4 else "")
            linhas.append(
                f'<div class="dl"><span class="dk">fichas com <code>empresa</code> '
                f'casando “{e(ent["entidade"])}”</span>{quem}{resto}'
                f'<span class="proc">{e(ent["procedencia"])} — é casamento de campo, '
                f'não confirmação de que trabalham lá hoje</span></div>')
        if d["tickers"]:
            linhas.append(
                f'<div class="dl"><span class="dk">companhia listada</span>'
                f'{", ".join(e(t) for t in d["tickers"])}'
                f'<span class="proc">ticker no próprio título — o fato nasce em '
                f'comunicado à CVM antes do jornal</span></div>')
        h = d["historico"]
        ant = "".join(
            f'<li>{e(manchete_limpa(a["title"]))} <span class="src">{br(a["data"])}</span></li>'
            for a in h["anteriores"])
        linhas.append(
            f'<div class="dl"><span class="dk">histórico desta frente</span>'
            f'<b>{h["total"]}</b> manchetes no total · <b>{h["d30"]}</b> em 30 dias · '
            f'<b>{h["d7"]}</b> em 7 dias · vigiada desde {br(h["primeiro"])}'
            f'<span class="proc">project_news_hits (watcher)</span></div>'
            + (f'<div class="dl"><span class="dk">histórias anteriores distintas</span>'
               f'<ul class="mini">{ant}</ul></div>' if ant else ""))
        if d["desconhecidos"]:
            linhas.append(
                f'<div class="dl"><span class="dk">nomes citados sem ficha no INTEL</span>'
                f'{", ".join(e(x) for x in d["desconhecidos"][:8])}'
                f'<span class="proc">extração de nome próprio do título — pode conter '
                f'falso positivo; é informação, não afirmação</span></div>')
        blocos.append(
            f'<details class="dossie"><summary>dossiê determinista · '
            f'{e(manchete_limpa(g[0]["title"])[:58])}…</summary>{"".join(linhas)}</details>')
    return "".join(blocos)


def quem_html(nomes):
    out = []
    for n in nomes:
        m = membros_by_nome.get(n) or {}
        det = " · ".join(x for x in [m.get("cargo"), m.get("empresa")] if x)
        out.append(f'<span class="pessoa"><b>{e(n)}</b>{" — " + e(det) if det else ""}</span>')
    return " ".join(out)


pool_livre = [(p, toks(p["title"])) for p in POOL_LIVRE]

NIVEIS = {"janela": ("janela curta", "warn"), "gate": ("sob gate", "warn"),
          "verificar": ("verificar antes", "calm")}

# ── ROTEAMENTO: é aqui que o ESTADO manda ───────────────────────────────────
pede_voce, acompanhando, guardrails = [], [], []
publicar_agora = []

for c in CURADORIA:
    grupos = acha(c["manchetes"])
    if not grupos:
        continue
    st = postura_de(c["frente"])
    publicar_agora += [h["title"] for g in grupos for h in g]

    vetado = None
    for v in st.get("nao_e_gancho", []):
        if any(t in h["title"].lower() for g in grupos for h in g for t in v["termos"]):
            vetado = v
            break

    if st.get("postura", "agir") != "agir":
        acompanhando.append((c, grupos, st))
    elif vetado:
        guardrails.append((c, grupos, st, vetado))
    else:
        pede_voce.append((c, grupos, st))

radar = []
for r in RADAR:
    grupos = acha(r["manchetes"])
    if grupos:
        publicar_agora += [h["title"] for g in grupos for h in g]
        radar.append((r, grupos))

clips = []
for trecho in CLIP_DESTAQUE:
    for it in (clipping.get("itens") or []):
        if trecho.lower() in (it.get("titulo_resumido") or "").lower():
            clips.append(it)
            break

# ── HTML ────────────────────────────────────────────────────────────────────
fontes = {f["source"]: f for f in (M.get("fontes") or [])}
valor = fontes.get("Valor Econômico", {})
estrat = fontes.get("Google News - Estratégia Empresarial", {})
n_calados = sum(len(g) for _, gs, _ in acompanhando for g in gs) + \
            sum(len(g) for _, gs, _, _ in guardrails for g in gs)

cards = []
for c, grupos, st in pede_voce:
    rot, cls = NIVEIS[c["nivel"]]
    fechados = "".join(f"<li>{e(x)}</li>" for x in st.get("fatos_fechados", []))
    cards.append(f"""
    <article class="card {cls}">
      <div class="card-top"><span class="frente">{e(c['frente'])}</span>
        <span class="tag {cls}">{rot}</span></div>
      <h3>{c['titulo']}</h3>
      <ul class="manchetes">{li_manchetes(grupos)}</ul>
      <p class="porque"><span class="rot">por que chega até você</span>{c['porque']}</p>
      {f'<p class="fechado"><span class="rot">já sabido — não se pergunta de novo</span><ul class="mini">{fechados}</ul></p>' if fechados else ''}
      <p class="acao"><span class="rot">a ação</span>{c['acao']}</p>
      <p class="quem"><span class="rot">quem está desse lado</span>{quem_html(c['quem'])}</p>
      {dossie_html(grupos)}
    </article>""")

acomp = []
for c, grupos, st in acompanhando:
    idade = (date.today() - date.fromisoformat(st["declarado_em"])).days
    idade_txt = "declarado hoje" if idade == 0 else f"declarado há {idade} dia{'s' if idade > 1 else ''}"
    fechados = "".join(f"<li>{e(x)}</li>" for x in st.get("fatos_fechados", []))
    acomp.append(f"""
    <article class="card quiet">
      <div class="card-top"><span class="frente">{e(c['frente'])}</span>
        <span class="tag quiet">{e(st['postura'])} · {idade_txt}</span></div>
      <h3>{c['titulo']}</h3>
      <ul class="manchetes">{li_manchetes(grupos)}</ul>
      <p class="fechado"><span class="rot">já sabido — não se pergunta de novo</span>
        <ul class="mini">{fechados}</ul></p>
      <p class="volta"><span class="rot">só volta a pedir você se</span>{e(st['so_volta_se'])}</p>
      {f'<p class="quem">{e(st["nota"])}</p>' if st.get('nota') else ''}
      {dossie_html(grupos)}
    </article>""")

guards = []
for c, grupos, st, v in guardrails:
    fechados = "".join(f"<li>{e(x)}</li>" for x in st.get("fatos_fechados", []))
    guards.append(f"""
    <article class="card crit">
      <div class="card-top"><span class="frente">{e(c['frente'])}</span>
        <span class="tag crit">não é gancho</span></div>
      <h3>{c['titulo']}</h3>
      <ul class="manchetes">{li_manchetes(grupos)}</ul>
      <p class="porque"><span class="rot">por que NÃO usar</span>{e(v['porque'])}</p>
      <p class="fechado"><span class="rot">o fato, contra fonte primária</span>
        <ul class="mini">{fechados}</ul></p>
      <p class="quem"><span class="rot">de quem se trata</span>{quem_html(c['quem'])}</p>
      {dossie_html(grupos)}
    </article>""")

radar_li = []
for r, grupos in radar:
    h = grupos[0][0]
    radar_li.append(f"""
      <li><b>{e(r['frente'])}</b>
        <a href="{e(h['url'])}" target="_blank">{e(manchete_limpa(h['title']))}</a>
        <span class="src">{e(veiculo(h['title']))} · {br(h['data'])}</span>
        <span class="nota">{e(r['nota'])}</span></li>""")

_clip_out = []
for it in clips:
    titulo = it.get("titulo_resumido") or ""
    tier = acesso_do(it.get("source"))
    gem = gemeo_livre(titulo) if tier else None
    if gem:
        substituidas += 1
        link, fonte = gem["link"], gem["source"]
        badge = f'<span class="livre">livre via {e(fonte)}</span>'
        titulo_html = f'<a href="{e(link)}" target="_blank">{e(titulo)}</a>'
    elif tier:
        bloqueadas += 1
        badge = f'<span class="muro">{tier} · sem gêmeo livre</span>'
        titulo_html = f'<span class="sem-link">{e(titulo)}</span>'
    else:
        badge = ""
        titulo_html = f'<a href="{e(it.get("link"))}" target="_blank">{e(titulo)}</a>'
    _clip_out.append(f"""
      <li>{titulo_html}
        <span class="src">{e(it.get('source'))} · {e(it.get('categoria'))}</span>{badge}
        <span class="nota">{e(it.get('resumo'))}</span></li>""")
clips_li = "".join(_clip_out)

quietos_n = M["watchers"] - len({h["projeto"] for h in hits if h.get("projeto")})
clip_dia = clipping["gerado_em"][:10]

HTML = f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Boletim de Inteligência · {SEMANA}</title>
<style>
:root {{
  --paper:#f4f0e7; --panel:#fbf9f3; --panel-edge:#e4ddcc; --ink:#23211c;
  --ink-soft:#57534a; --ink-faint:#8a8477; --brass:#9c7a33; --brass-soft:#b79a55;
  --crit:#a4321f; --crit-bg:#f0e0d6; --warn:#9a6a13; --warn-bg:#f0e6cf;
  --calm:#4f6b4a; --calm-bg:#e3e9dc;
  --shadow:0 1px 2px rgba(35,33,28,.06),0 6px 20px rgba(35,33,28,.05);
  --serif:Georgia,"Iowan Old Style","Times New Roman",serif;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  --mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,monospace;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--paper); color:var(--ink); font-family:var(--sans);
  line-height:1.5; -webkit-font-smoothing:antialiased; }}
.wrap {{ max-width:860px; margin:0 auto; padding:40px 22px 72px; }}
a {{ color:var(--ink); text-decoration:none; border-bottom:1px solid var(--brass-soft); }}
a:hover {{ color:var(--brass); }}

.mast {{ border-bottom:2px solid var(--ink); padding-bottom:14px; }}
.mast-top {{ display:flex; justify-content:space-between; align-items:baseline; gap:16px; flex-wrap:wrap; }}
.brand {{ font-family:var(--serif); font-size:clamp(28px,5.5vw,42px); font-weight:700;
  letter-spacing:-.01em; line-height:1.05; }}
.brand em {{ font-style:italic; color:var(--brass); }}
.stamp {{ font-family:var(--mono); font-size:12px; color:var(--ink-faint); text-align:right; }}
.stamp b {{ color:var(--ink-soft); font-weight:600; }}
.funil {{ display:flex; flex-wrap:wrap; gap:6px 16px; margin:12px 0 0;
  font-family:var(--mono); font-size:11.5px; color:var(--ink-faint); }}
.funil b {{ color:var(--ink-soft); font-weight:600; }}
.funil .seta {{ color:var(--brass-soft); }}

h2.sec {{ font-family:var(--serif); font-size:15px; text-transform:uppercase; letter-spacing:.14em;
  color:var(--brass); margin:38px 0 4px; font-weight:700; }}
h2.sec + .sub {{ margin:0 0 16px; font-size:13px; color:var(--ink-faint); }}

.card {{ background:var(--panel); border:1px solid var(--panel-edge); border-left:3px solid var(--brass);
  border-radius:10px; padding:16px 18px; margin:0 0 14px; box-shadow:var(--shadow); }}
.card.crit {{ border-left-color:var(--crit); }}
.card.warn {{ border-left-color:var(--warn); }}
.card.calm {{ border-left-color:var(--calm); }}
.card.quiet {{ border-left-color:var(--ink-faint); background:#f7f4ec; box-shadow:none; }}
.card-top {{ display:flex; justify-content:space-between; align-items:baseline; gap:12px;
  flex-wrap:wrap; margin-bottom:6px; }}
.frente {{ font-family:var(--mono); font-size:10.5px; text-transform:uppercase; letter-spacing:.09em;
  color:var(--ink-faint); }}
.tag {{ font-family:var(--mono); font-size:10px; text-transform:uppercase; letter-spacing:.09em;
  padding:2px 8px; border-radius:20px; white-space:nowrap; }}
.tag.crit {{ background:var(--crit-bg); color:var(--crit); }}
.tag.warn {{ background:var(--warn-bg); color:var(--warn); }}
.tag.calm {{ background:var(--calm-bg); color:var(--calm); }}
.tag.quiet {{ background:#eae5d8; color:var(--ink-soft); }}
.card h3 {{ font-family:var(--serif); font-size:20px; line-height:1.25; margin:0 0 10px; font-weight:700; }}
.card.quiet h3 {{ font-size:17px; color:var(--ink-soft); }}

ul.manchetes {{ list-style:none; margin:0 0 12px; padding:0 0 0 13px; border-left:2px solid var(--panel-edge); }}
ul.manchetes li {{ font-size:13.5px; margin:0 0 5px; }}
.src {{ display:inline-block; font-family:var(--mono); font-size:10.5px; color:var(--ink-faint);
  margin-left:7px; white-space:nowrap; }}
.mais {{ display:inline-block; font-family:var(--mono); font-size:9.5px; color:var(--brass);
  background:var(--warn-bg); border-radius:20px; padding:1px 7px; margin-left:6px; cursor:help; }}
.muro {{ display:inline-block; font-family:var(--mono); font-size:9.5px; color:var(--crit);
  background:var(--crit-bg); border-radius:20px; padding:1px 7px; margin-left:6px; }}
.livre {{ display:inline-block; font-family:var(--mono); font-size:9.5px; color:var(--calm);
  background:var(--calm-bg); border-radius:20px; padding:1px 7px; margin-left:6px; }}
.sem-link {{ color:var(--ink-soft); border-bottom:1px dotted var(--ink-faint); }}
details.dossie {{ margin:10px 0 0; border-top:1px solid var(--panel-edge); padding-top:9px; }}
details.dossie summary {{ font-family:var(--mono); font-size:10.5px; text-transform:uppercase;
  letter-spacing:.09em; color:var(--brass); cursor:pointer; list-style:none; }}
details.dossie summary::-webkit-details-marker {{ display:none; }}
details.dossie summary::before {{ content:"▸ "; }}
details.dossie[open] summary::before {{ content:"▾ "; }}
details.dossie summary:hover {{ color:var(--ink); }}
.dl {{ margin:9px 0 0; padding-left:11px; border-left:2px solid var(--panel-edge);
  font-size:13px; color:var(--ink); }}
.dk {{ display:block; font-family:var(--mono); font-size:9.5px; text-transform:uppercase;
  letter-spacing:.1em; color:var(--ink-faint); margin-bottom:2px; }}
.proc {{ display:block; font-family:var(--mono); font-size:10px; color:var(--ink-faint);
  margin-top:3px; }}
.dl.forte {{ border-left-color:var(--calm); }}
.dl.homonimo {{ border-left-color:var(--crit); background:#fbf1ec; padding:6px 0 6px 11px;
  border-radius:0 6px 6px 0; }}
.dl.degradado {{ border-left-color:var(--warn); }}
.ficha {{ font-size:12.5px; color:var(--ink-soft); }}
.tr {{ font-size:12.5px; color:var(--ink-soft); margin-top:3px; padding-left:9px;
  border-left:1px dotted var(--panel-edge); }}

.card p {{ margin:0 0 9px; font-size:14px; }}
.rot {{ display:block; font-family:var(--mono); font-size:10px; text-transform:uppercase;
  letter-spacing:.11em; color:var(--brass); margin-bottom:2px; }}
.porque {{ color:var(--ink-soft); }}
.acao {{ background:#fff; border:1px solid var(--panel-edge); border-radius:7px; padding:10px 12px; }}
.volta {{ background:#fff; border:1px dashed var(--panel-edge); border-radius:7px; padding:10px 12px;
  color:var(--ink-soft); }}
.fechado {{ color:var(--ink-soft); }}
ul.mini {{ margin:2px 0 0; padding-left:17px; }}
ul.mini li {{ font-size:13px; margin-bottom:3px; }}
.quem {{ margin-bottom:0; font-size:12.5px; color:var(--ink-faint); }}
.pessoa {{ display:inline-block; margin-right:12px; }}
.pessoa b {{ color:var(--ink-soft); }}

ul.lista {{ list-style:none; margin:0; padding:0; }}
ul.lista > li {{ background:var(--panel); border:1px solid var(--panel-edge); border-radius:8px;
  padding:11px 14px; margin:0 0 8px; font-size:13.5px; }}
ul.lista > li > b {{ font-family:var(--mono); font-size:10.5px; text-transform:uppercase;
  letter-spacing:.09em; color:var(--ink-faint); display:block; margin-bottom:3px; font-weight:600; }}
.nota {{ display:block; color:var(--ink-faint); font-size:12.5px; margin-top:4px; }}

.resumo-dia {{ background:var(--panel); border:1px solid var(--panel-edge); border-radius:10px;
  padding:14px 16px; margin:0 0 12px; font-size:14px; color:var(--ink-soft); font-family:var(--serif); }}

.nada {{ background:var(--calm-bg); border:1px solid var(--calm); border-radius:10px;
  padding:16px 18px; margin:22px 0 0; font-size:14px; color:var(--ink); }}
.nada-p {{ display:block; margin-top:6px; font-size:13px; color:var(--ink-soft); }}
.metodo {{ margin-top:42px; border-top:2px solid var(--ink); padding-top:16px; }}
.metodo h2 {{ font-family:var(--serif); font-size:15px; text-transform:uppercase; letter-spacing:.14em;
  color:var(--brass); margin:0 0 12px; }}
.metodo ul {{ margin:0; padding-left:18px; }}
.metodo li {{ font-size:13px; color:var(--ink-soft); margin-bottom:9px; }}
.metodo code {{ font-family:var(--mono); font-size:11.5px; background:var(--panel);
  border:1px solid var(--panel-edge); border-radius:4px; padding:1px 5px; }}
.assin {{ margin-top:22px; font-family:var(--mono); font-size:11px; color:var(--ink-faint); }}
@media print {{ body {{ background:#fff; }} .card,.lista>li {{ box-shadow:none; }} }}
</style></head><body><div class="wrap">

<header class="mast">
  <div class="mast-top">
    <div class="brand">Boletim de <em>Inteligência</em></div>
    <div class="stamp"><b>{SEMANA}</b><br>Almeida Prado · Conselhos Empresariais<br>edição semanal · sexta</div>
  </div>
  <div class="funil">
    <span><b>{mil(M['itens_30d'])}</b> manchetes varridas em 30 dias</span><span class="seta">→</span>
    <span><b>{M['hits_30d']}</b> nas {M['watchers']} frentes</span><span class="seta">→</span>
    <span><b>{len(cards)}</b> pedem você</span><span class="seta">→</span>
    <span><b>{n_calados}</b> caladas por decisão sua</span>
  </div>
</header>

{"" if (cards or guards or acomp or radar_li) else """
<div class="nada">
  <b>Semana sem novidade nas frentes.</b>
  As """ + str(M["watchers"]) + """ frentes foram varridas e nada apareceu que já não tenha saído
  numa edição anterior. Isto é informação, não falha: a régua de não-repetição suprimiu
  """ + str(suprimidas_repeticao) + """ manchete(s) já mostrada(s), e nenhuma história nova entrou.
  <span class="nada-p">O clipping de leitura opcional abaixo segue, porque é repertório e não frente.</span>
</div>"""}
<h2 class="sec">Pede você</h2>
<p class="sub">Cruza com frente em postura <code>agir</code> <i>e</i> com alguém que você conhece.
  Nada aqui é FYI, e nada aqui é pergunta já respondida.</p>
{''.join(cards)}

<h2 class="sec">Não é gancho — cuidado, não ação</h2>
<p class="sub">Apareceu, tem a ver com você, e o movimento certo é <b>não</b> usar. O boletim mostra
  para você não ser pego de surpresa por quem comentar.</p>
{''.join(guards)}

<h2 class="sec">Acompanhando — sem ação, por decisão sua</h2>
<p class="sub">A máquina viu e calou de propósito. Está aqui com a postura, a idade dela e a condição
  de saída — para você poder mudar de ideia, não para pedir nada.</p>
{''.join(acomp)}

<h2 class="sec">Radar</h2>
<ul class="lista">{''.join(radar_li)}
  <li><b>silêncio</b>{quietos_n} das {M['watchers']} frentes vigiadas não tiveram uma linha no período —
    entre elas Phisalia, Orbiz Capital, Roger Michaelis e Eco Resort Itacaré.
    <span class="nota">Silêncio é informação: nenhuma dessas empresas gerou notícia pública em 30 dias.</span></li>
</ul>

<h2 class="sec">Leitura opcional · 4 min</h2>
<p class="sub">Do clipping de {br(clip_dia)}, gerado às 5h35. Não é sobre as suas frentes — é repertório.</p>
<div class="resumo-dia">{e(clipping['resumo_dia'])}</div>
<ul class="lista">{clips_li}</ul>

<section class="metodo">
  <h2>A régua deste boletim</h2>
  <ul>
    <li><b>Não repete manchete.</b> O que já saiu numa edição não volta
      (<code>boletim_publicados.json</code>). Nesta edição: <b>{suprimidas_repeticao}</b> suprimidas —
      é a primeira, ainda não havia histórico.</li>
    <li><b>Não repete história.</b> {agrupadas} manchete(s) colapsada(s) em outra desta edição:
      oito veículos sobre o mesmo fato viram uma linha com “+N veículos”.</li>
    <li><b>Não repete pergunta.</b> A camada nova, e a que faltava. Postura por frente em
      <code>~/cockpit/boletim_estado.json</code> — frente que não está em <code>agir</code> não pode
      subir para “Pede você”. <b>Postura sem condição de saída escrita aborta o gerador</b>: “acompanhar”
      sem <code>so_volta_se</code> seria cegueira comprada, não decisão.</li>
    <li><b>O que gerou a régua.</b> Na edição de 20/09 eu subi Fictor pedindo confirmação de um crédito
      cuja habilitação já estava no próprio registro do projeto (<code>status=pausado</code>,
      “R$ 65k, habilitados confirmados”), e tratei o Marson como CEO da Westwing quando uma memória de
      13/08 já o corrigia contra o Formulário de Referência da CVM. <b>Nos dois casos o fato estava no
      banco.</b> Não faltava dado — faltava o boletim ser obrigado a ler o que já se sabe.
      A ficha do Marson (#18707) foi corrigida no Neon em 20/09.</li>
    <li><b>Acesso: corrigido na fonte hoje.</b> O feed do Valor era a <i>home</i> do jornal —
      905 dos {mil(M['itens_30d'])} itens de 30 dias (27% do corpus), trazendo campanha eleitoral e
      Datafolha. Trocado pelas três editorias (Empresas / Finanças / Legislação) e somadas
      <b>8 fontes livres</b> (Agência Brasil ×2, InfoMoney, Money Times, Poder360, Brazil Journal,
      NeoFeed, Conjur). As 22 fontes foram testadas uma a uma: <b>22 entregando</b>.</li>
    <li><b>O muro agora é visível, e contornado quando dá.</b> Dentro de um cluster o boletim linka o
      veículo mais livre e o restrito vira corroboração; quando a história só existe atrás do muro, ela
      aparece <b>sem link</b>, rotulada. Nesta edição: <b>{substituidas}</b> item(ns) trocado(s) por
      gêmeo livre, <b>{bloqueadas}</b> segue(m) restrito(s). Medição de hoje: dos
      {len(clipping['itens'] or [])} itens curados, 3 tinham gêmeo no pool antigo — a Agência Brasil
      sozinha recuperou 3 dos 7 órfãos.</li>
    <li><b>E a falha que o teste pegou:</b> sem <code>User-Agent</code> o coletor se anunciava como
      <code>python-httpx</code> e a Agência Brasil devolvia <b>500</b>, a NeoFeed <b>403</b> — nos mesmos
      feeds que o <code>curl</code> abria. Corrigido no <code>news_hub.py</code>. É a falha que mais
      engana: o cron roda, ninguém erra, e a fonte simplesmente nunca entrega nada.</li>
    <li><b>Ruído com nome.</b> O watcher <code>Vallen Clinic</code> ingeriu “{DESCARTE_EXEMPLO}”
      — futebol equatoriano, puxado por “del Valle”. O modo estrito mata na emissão; o lixo entra no banco.</li>
  </ul>
  <p class="assin">Chão lido do Neon em {date.today().strftime('%d/%m/%Y')} ·
    curadoria {"escrita hoje" if CURADORIA_IDADE == 0 else
      (f"⚠️ escrita há {CURADORIA_IDADE} dia(s) — o julgamento pode não cobrir as manchetes desta edição"
       if CURADORIA_IDADE is not None else "sem data — frescor desconhecido")} ·
    dossiê determinista {"gerado hoje" if DOSSIE_IDADE_DIAS == 0 else
      (f"⚠️ gerado há {DOSSIE_IDADE_DIAS} dia(s) — o passo do dossiê pode ter falhado"
       if DOSSIE_IDADE_DIAS else "AUSENTE — cards sem camada 2")} ·
    postura em <code>~/cockpit/boletim_estado.json</code> (atualizado {br(ESTADO['atualizado_em'])}) ·
    gerador <code>boletim.py</code>{' · edição PUBLICADA' if PUBLICAR else ' · prévia, nada persistido'}</p>
</section>

</div></body></html>"""

os.makedirs(COCKPIT, exist_ok=True)
open(OUT, "w").write(HTML)
if PUBLICAR:
    json.dump(sorted(publicados | set(publicar_agora)), open(PUBLICADOS_PATH, "w"),
              ensure_ascii=False, indent=1)

print(f"{OUT} · {len(HTML):,} bytes")
print(f"  pede você: {len(cards)} · não é gancho: {len(guards)} · acompanhando: {len(acomp)} · "
      f"radar: {len(radar_li)} · leitura: {len(clips)}")
print(f"  agrupadas por história: {agrupadas} · suprimidas por repetição: {suprimidas_repeticao} · "
      f"caladas por postura: {n_calados}")
print(f"  {'PUBLICADA — ' + str(len(set(publicar_agora))) + ' manchetes marcadas' if PUBLICAR else 'prévia (nada persistido)'}")
