#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""Gera ~/cockpit/triagem.html — dar dono e projeto às tasks órfãs.

PEDIDO (Renato, 08/08/2026): "bora fazer tudo de uma vez. Monta uma tela com
sugestão de Projeto e RACI para cada uma delas e eu confiro." As opções por item
são as que ele definiu: **atribuir, ou baixar se não fizer mais sentido**.

POR QUE ISTO É PRÉ-REQUISITO DO RITUAL SEMANAL. São 122 itens abertos e **53 sem
dono** — 43 por cento. Uma planning que rode sobre o banco de hoje mostraria
menos da metade do trabalho, e do pior jeito: em silêncio, parecendo completa.

O QUE A LEITURA DOS DADOS MOSTROU, e que define o desenho:

  · **8 das 53 já têm o RACI escrito** no campo `contexto` ("RACI: R=Jéssica |
    A=Thalita/Renato"), com `contact_id` NULL. O dono está declarado e ninguém
    lê — de novo o dado existindo sem consumidor. Para essas não há o que
    adivinhar: há o que PARSEAR.
  · **19 não têm projeto nenhum**, e várias estão em inglês, vindas de alguma
    extração automática ("Send authorization-to-sell to group").
  · Nenhuma está vencida, exceto uma. Ou seja: em geral não é trabalho esquecido
    — é trabalho que nunca teve dono declarado.

COMO AS SUGESTÕES SÃO FEITAS, e por que cada uma carrega CONFIANÇA à vista:

  alta  — o RACI está escrito no contexto, ou o nome citado é raro no CRM
  média — casou por nome/apelido menos distintivo, ou pelo projeto do contato
  baixa — só uma pista fraca; é chute e está declarado como tal

A confiança na tela existe para ele passar rápido pelo que é seguro e ler devagar
o que é meu palpite. Sugestão sem confiança declarada convida a aceitar tudo no
piloto automático — e aí a triagem produz dado errado com aparência de revisado.

RARIDADE EM VEZ DE PALPITE: um nome citado só vira sugestão se identificar. Um
token que aparece em ≤3 fichas do CRM basta sozinho ("Blume", "Amadeo"); um que
aparece em 115 ("Rodrigo") nunca. É a mesma régua do `board_hunt.py`, medida no
corpus e não estimada.

NADA É ESCRITO AQUI. A tela produz um texto que ele copia; aplicar é passo
separado (`--gravar -`), como nas outras telas de decisão.

Uso:  ./triagem_tasks.py            # gera e abre
      ./triagem_tasks.py --gravar - # aplica o que ele colou de volta
"""
import os
import re
import sys
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras

ROOT = "/Users/rap/prospect-system"
SAIDA = os.path.expanduser("~/cockpit/triagem.html")
BRT = ZoneInfo("America/Sao_Paulo")
RARO_ATE = 3        # nº de fichas abaixo do qual um token identifica sozinho

# Sobrenomes/nomes de bacia: casariam com meio CRM e não identificam ninguém.
_VAZIAS = {"filho", "junior", "neto", "santos", "silva", "souza", "prado",
           "almeida", "renato", "faria", "carlos", "jose", "maria", "paulo"}


def env(k):
    for l in open(f"{ROOT}/.env"):
        if l.startswith(k + "="):
            return l.split("=", 1)[1].strip().strip('"')
    return ""


def esc(t):
    return (str(t) if t is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def sem_acento(t):
    t = unicodedata.normalize("NFD", (t or "").lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def tokens(t):
    return [p for p in re.findall(r"[a-z]{4,}", sem_acento(t)) if p not in _VAZIAS]


def conectar():
    return psycopg2.connect(env("DATABASE_URL"), keepalives=1,
                            cursor_factory=psycopg2.extras.RealDictCursor)


# ------------------------------------------------------------------ coleta --

def coletar(cur):
    cur.execute("""
        SELECT t.id, t.titulo, COALESCE(t.contexto,'') AS contexto, t.project_id,
               p.nome AS projeto, t.data_criacao::date AS criada,
               t.data_vencimento::date AS vence, t.status, t.prioridade
          FROM tasks t LEFT JOIN projects p ON p.id = t.project_id
         WHERE t.status IN ('pending','in_progress','on_hold')
           AND t.contact_id IS NULL
         ORDER BY (t.project_id IS NULL) DESC, t.project_id, t.data_criacao
    """)
    tasks = [dict(r) for r in cur.fetchall()]

    # SÓ CONTATOS COM HISTÓRICO. Sem este filtro, "Create WhatsApp group"
    # casava com a ficha "C6 Bank WhatsApp", "site Novo" com "Hotel Novo Mundo" e
    # "microlote para Portugal" com "Heitor Portugal". Entidade cadastrada não é
    # dono de tarefa, e o sinal que as separa é medível: quem trabalha com o
    # Renato tem mensagem trocada ou já é dono de alguma task. Não é lista de
    # palavras proibidas — isso seria o gato-e-rato que o check-G já provou não
    # funcionar.
    cur.execute("""
        SELECT c.id, c.nome FROM contacts c
         WHERE c.nome IS NOT NULL AND btrim(c.nome) <> ''
           AND (EXISTS (SELECT 1 FROM messages m WHERE m.contact_id = c.id)
                OR EXISTS (SELECT 1 FROM tasks t WHERE t.contact_id = c.id))
    """)
    contatos = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT id, nome FROM projects WHERE status IN ('ativo','on_hold')")
    projetos = [dict(r) for r in cur.fetchall()]

    # Raridade de cada token dos nomes de contato — medida no corpus.
    freq = {}
    for c in contatos:
        for t in set(tokens(c["nome"])):
            freq[t] = freq.get(t, 0) + 1

    # Projeto "natural" de cada contato: onde ele tem mais tasks hoje. Serve de
    # segunda pista quando a task cita uma pessoa mas não diz a frente.
    cur.execute("""
        SELECT contact_id, project_id, count(*) n FROM tasks
         WHERE contact_id IS NOT NULL AND project_id IS NOT NULL
         GROUP BY 1,2 ORDER BY 3 DESC
    """)
    proj_do_contato = {}
    for r in cur.fetchall():
        proj_do_contato.setdefault(r["contact_id"], r["project_id"])

    return tasks, contatos, projetos, freq, proj_do_contato


# --------------------------------------------------------------- sugestões --

def raci_escrito(contexto):
    """'RACI: R=Jéssica | A=Thalita/Renato' -> 'Jéssica'. O dono já está lá."""
    m = re.search(r"R\s*=\s*([^|.\n]+)", contexto or "")
    if not m:
        return None
    nome = m.group(1).strip()
    nome = re.split(r"\s+(?:Milestone|Recorrente|Depende)\b", nome)[0].strip()
    return nome[:40] or None


def _nomes_proprios(texto):
    """Candidatos a NOME no texto: sequências capitalizadas.

    A primeira versão fazia o inverso — varria as ~9 mil fichas procurando
    qualquer token delas no texto — e o resultado foi um desastre silencioso:
    o campo `contexto` contém a palavra "professional", existe uma ficha
    "Hansen (Professional driver)", e "professional" é raro no CRM. Logo,
    **47 das 55 tasks receberam confiança ALTA apontando para o Hansen**.
    Também casaram "Natique Ident" com *identify*, "Todos" com *TODOS os
    scopes* e "First Name Last Name" com *name*.

    A lição: procurar a ficha a partir do texto inteiro casa com ruído. Procurar
    NOME PRÓPRIO (capitalizado, no meio da frase) casa com gente. Palavra
    capitalizada por estar em início de frase ou por ser sigla é descartada.
    """
    saida = []
    for frase in re.split(r"[.;:\n]", texto or ""):
        palavras = frase.strip().split()
        for i, w in enumerate(palavras):
            limpo = re.sub(r"[^A-Za-zÀ-ÿ]", "", w)
            if len(limpo) < 3:
                continue
            if not limpo[0].isupper() or limpo.isupper():   # sigla/caixa alta fora
                continue
            if i == 0:                                      # início de frase não conta
                continue
            seq = [limpo]
            for j in range(i + 1, min(i + 3, len(palavras))):
                nxt = re.sub(r"[^A-Za-zÀ-ÿ]", "", palavras[j])
                if len(nxt) >= 3 and nxt[0].isupper() and not nxt.isupper():
                    seq.append(nxt)
                else:
                    break
            saida.append(" ".join(seq))
    return saida


# Fichas cujo "nome" não é nome de gente — casariam com texto comum.
_FICHAS_LIXO = re.compile(
    r"^(todos?|first name|last name|desconhecid|sem nome|contato|cliente|"
    r"suporte|atendimento|oficial|no.?reply)", re.I)


def acha_contato(texto, contatos, freq):
    """Contato citado no TÍTULO, com confiança. None se nada IDENTIFICA.

    Só considera nomes próprios extraídos do texto (ver `_nomes_proprios`), e
    exige que o casamento identifique: nome completo, ou token raro no CRM.
    """
    candidatos = _nomes_proprios(texto)
    if not candidatos:
        return None
    por_token = {}
    for c in contatos:
        if _FICHAS_LIXO.match((c["nome"] or "").strip()):
            continue
        for t in set(tokens(c["nome"])):
            por_token.setdefault(t, []).append(c)

    melhor = None
    for cand in candidatos:
        ts = tokens(cand)
        if not ts:
            continue
        for t in ts:
            for c in por_token.get(t, []):
                ts_ficha = tokens(c["nome"])
                casados = [x for x in ts_ficha if x in ts]
                if not casados:
                    continue
                raro = [x for x in casados if freq.get(x, 99) <= RARO_ATE]
                if len(casados) >= 2:
                    conf = "alta"
                elif raro:
                    conf = "média"        # um nome raro sozinho: provável, não certo
                else:
                    continue
                peso = (2 if conf == "alta" else 1, len(casados), -len(ts_ficha))
                if melhor is None or peso > melhor[0]:
                    melhor = (peso, c, conf, cand)
    if not melhor:
        return None
    _, c, conf, cand = melhor
    return {"id": c["id"], "nome": c["nome"], "conf": conf,
            "por": f"o título cita “{cand}”"}


def acha_projeto(texto, projetos, freq_proj):
    alvo = sem_acento(texto or "")
    for p in projetos:
        ts = [t for t in tokens(p["nome"]) if freq_proj.get(t, 0) <= 2]
        for t in ts:
            if re.search(r"\b" + re.escape(t), alvo):
                return {"id": p["id"], "nome": p["nome"], "conf": "média",
                        "por": f"o título cita “{t}”"}
    return None


def sugerir(tasks, contatos, projetos, freq, proj_do_contato):
    # frequência dos tokens entre NOMES DE PROJETO, pra não casar por palavra
    # que aparece em cinco projetos ("originação", "conselho").
    freq_proj = {}
    for p in projetos:
        for t in set(tokens(p["nome"])):
            freq_proj[t] = freq_proj.get(t, 0) + 1
    por_id = {p["id"]: p["nome"] for p in projetos}

    for t in tasks:
        texto = t["titulo"]   # NÃO o contexto: ver _nomes_proprios
        t["sug_dono"] = None
        t["sug_proj"] = None

        # 1) RACI escrito — a fonte mais forte, porque foi alguém que declarou
        nome_raci = raci_escrito(t["contexto"])
        if nome_raci:
            # Prefixo minúsculo de propósito: o nome do RACI é um nome solto, e
            # `_nomes_proprios` descarta a palavra na posição zero (assume início
            # de frase). Com "responsavel " na frente, o nome cai na posição 1 e
            # é lido. Sem isto, "R=Amadeo" nunca casava com "Amadeo Comin".
            hit = acha_contato("responsavel " + nome_raci, contatos, freq)
            if hit:
                hit["conf"] = "alta"
                hit["por"] = f"RACI escrito no contexto: R={nome_raci}"
                t["sug_dono"] = hit
            else:
                t["sug_dono"] = {"id": None, "nome": nome_raci, "conf": "média",
                                 "por": f"RACI escrito (R={nome_raci}) mas sem ficha casada"}

        # 2) nome citado no texto
        if not t["sug_dono"]:
            t["sug_dono"] = acha_contato(texto, contatos, freq)

        # 3) projeto: só pra quem não tem
        if not t["project_id"]:
            t["sug_proj"] = acha_projeto(texto, projetos, freq_proj)
            if not t["sug_proj"] and t["sug_dono"] and t["sug_dono"].get("id"):
                pid = proj_do_contato.get(t["sug_dono"]["id"])
                if pid:
                    t["sug_proj"] = {"id": pid, "nome": por_id.get(pid, f"#{pid}"),
                                     "conf": "baixa",
                                     "por": f'frente onde {t["sug_dono"]["nome"].split()[0]} tem mais tasks'}
    return tasks


# ------------------------------------------------------------------ render --

CSS = """
:root{--charcoal:#2b2925;--ink:#3a362f;--muted:#8a8072;--creme:#f6f1e7;--card:#fffdf8;
 --line:#e5ddcd;--dourado:#b8973e;--soft:#e9dcb8;--alta:#4a6035;--alta-bg:#eef3e9;
 --media:#c69a2e;--media-bg:#f9f0d6;--baixa:#c0492f;--baixa-bg:#fae9e3;}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--creme);color:var(--ink);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;padding:26px 30px 90px}
header{display:flex;align-items:baseline;gap:16px;flex-wrap:wrap;border-bottom:2px solid var(--dourado);padding-bottom:14px;margin-bottom:8px}
h1{font-size:22px;font-weight:700;color:var(--charcoal)} h1 .dot{color:var(--dourado)}
.meta{margin-left:auto;font-size:13px;color:var(--muted)}
.intro{max-width:900px;margin:14px 0 6px;font-size:13.5px;color:var(--ink)}
.intro b{color:var(--charcoal)}
.kpis{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0 20px;font-size:12.5px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:9px;padding:7px 13px}
.kpi b{color:var(--charcoal);font-size:16px;display:block}
.grupo{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.7px;
 color:var(--dourado);margin:26px 0 10px;border-bottom:1px solid var(--line);padding-bottom:5px}
.t{background:var(--card);border:1px solid var(--line);border-left-width:4px;border-left-color:var(--line);
 border-radius:9px;padding:12px 14px;margin-bottom:10px;max-width:1000px}
.t.alta{border-left-color:var(--alta)} .t.média{border-left-color:var(--media)}
.t.baixa{border-left-color:var(--baixa)} .t.sem{border-left-style:dashed}
.t .id{font:11.5px ui-monospace,Menlo,monospace;color:var(--muted)}
.t .tit{font-weight:700;color:var(--charcoal);font-size:14.5px;margin:2px 0 3px}
.t .ctx{font-size:12px;color:var(--muted);margin-bottom:8px}
.sug{display:flex;gap:18px;flex-wrap:wrap;font-size:13px;margin:8px 0}
.sug .cx{flex:1;min-width:260px}
.sug .lbl{font-size:10.5px;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);font-weight:700}
.sug .val{font-weight:600;color:var(--charcoal)}
.sug .por{font-size:11.5px;color:var(--muted);font-style:italic}
.conf{font-size:10px;border-radius:20px;padding:1px 8px;font-weight:700;margin-left:6px}
.conf.alta{background:var(--alta-bg);color:var(--alta)}
.conf.média{background:var(--media-bg);color:#8a6a12}
.conf.baixa{background:var(--baixa-bg);color:var(--baixa)}
.acoes{display:flex;gap:7px;margin-top:10px;flex-wrap:wrap;align-items:center}
.acoes button{font:inherit;font-size:12.5px;border:1px solid var(--line);background:#fff;
 border-radius:7px;padding:4px 13px;cursor:pointer;color:var(--ink)}
.acoes button:hover{border-color:var(--dourado)}
.acoes button.on{background:var(--dourado);color:#fff;border-color:var(--dourado);font-weight:600}
.acoes input{font:inherit;font-size:12.5px;border:1px solid var(--line);border-radius:7px;
 padding:4px 9px;flex:1;min-width:180px;background:#fbf7ee}
.barra{position:sticky;bottom:14px;display:flex;gap:10px;align-items:center;background:var(--card);
 border:1px solid var(--dourado);border-radius:11px;padding:11px 16px;margin-top:22px;max-width:1000px;
 box-shadow:0 6px 18px rgba(0,0,0,.13)}
.barra .n{margin-right:auto;font-size:12px;font-weight:700;text-transform:uppercase;
 letter-spacing:.6px;color:var(--dourado)}
.barra button{font:inherit;font-size:13px;font-weight:600;border:0;border-radius:7px;padding:6px 15px;cursor:pointer}
#copiar{background:var(--dourado);color:#fff} #limpar{background:transparent;color:var(--muted)}
#saida{width:100%;margin-top:10px;font:12px ui-monospace,Menlo,monospace;border:1px solid var(--line);
 border-radius:7px;padding:8px;background:#fbf7ee}
footer{margin-top:24px;color:var(--muted);font-size:11.5px;max-width:1000px;line-height:1.6}
"""

JS = """
const K='tri_tasks', S=JSON.parse(localStorage.getItem(K)||'{}');
function pinta(){
  document.querySelectorAll('.t').forEach(el=>{
    const id=el.dataset.id, v=S[id];
    el.querySelectorAll('.acoes button').forEach(b=>b.classList.toggle('on', b.dataset.v===(v&&v.v)));
  });
  const n=Object.keys(S).length;
  document.querySelector('.barra').hidden = n===0;
  document.querySelector('.barra .n').textContent = n+' decidida'+(n===1?'':'s');
}
document.querySelectorAll('.acoes button').forEach(b=>{
  b.addEventListener('click',()=>{
    const el=b.closest('.t'), id=el.dataset.id;
    if(S[id] && S[id].v===b.dataset.v) delete S[id];
    else S[id]={v:b.dataset.v, extra:(el.querySelector('.acoes input')||{}).value||''};
    localStorage.setItem(K,JSON.stringify(S)); pinta();
  });
});
document.querySelectorAll('.acoes input').forEach(i=>{
  i.addEventListener('input',()=>{const id=i.closest('.t').dataset.id;
    if(S[id]){S[id].extra=i.value; localStorage.setItem(K,JSON.stringify(S));}});
});
document.getElementById('copiar').addEventListener('click',async()=>{
  const L=[];
  document.querySelectorAll('.t').forEach(el=>{
    const id=el.dataset.id, v=S[id]; if(!v) return;
    if(v.v==='aceitar') L.push(`ATRIBUIR ${id} -> dono #${el.dataset.dono||'?'} projeto #${el.dataset.proj||'manter'}`);
    else if(v.v==='baixar') L.push(`BAIXAR ${id}`);
    else if(v.v==='outro') L.push(`OUTRO ${id} -> ${v.extra||'(sem instrução)'}`);
  });
  const txt=L.join('\\n'), b=document.getElementById('copiar'), out=document.getElementById('saida');
  try{ await navigator.clipboard.writeText(txt); b.textContent='copiado ('+L.length+')'; }
  catch(e){ out.hidden=false; out.value=txt; out.select(); b.textContent='selecione e copie'; }
  setTimeout(()=>b.textContent='Copiar decisões',1800);
});
document.getElementById('limpar').addEventListener('click',()=>{
  Object.keys(S).forEach(k=>delete S[k]); localStorage.removeItem(K); pinta();
});
pinta();
"""


def render(tasks):
    agora = datetime.now(BRT)
    sem_proj = [t for t in tasks if not t["project_id"]]
    com_raci = [t for t in tasks if "RACI" in (t["contexto"] or "")]
    com_sug = [t for t in tasks if t["sug_dono"]]

    def bloco(t):
        d, p = t["sug_dono"], t["sug_proj"]
        conf = (d or {}).get("conf") or "sem"
        cls = conf if conf in ("alta", "média", "baixa") else "sem"
        ctx = (t["contexto"] or "").strip()
        ctx = "" if ctx in ("professional", "dev", "") else esc(ctx[:150])
        idade = (agora.date() - t["criada"]).days

        dono = (f'<span class="val">{esc(d["nome"])}</span>'
                f'<span class="conf {d["conf"]}">{d["conf"]}</span>'
                f'<div class="por">{esc(d["por"])}</div>') if d else \
               '<span class="val" style="color:var(--muted)">— sem sugestão</span>'
        if t["project_id"]:
            proj = f'<span class="val">{esc(t["projeto"])}</span><div class="por">já tem projeto</div>'
        elif p:
            proj = (f'<span class="val">{esc(p["nome"])}</span>'
                    f'<span class="conf {p["conf"]}">{p["conf"]}</span>'
                    f'<div class="por">{esc(p["por"])}</div>')
        else:
            proj = '<span class="val" style="color:var(--muted)">— sem sugestão</span>'

        return f"""<div class="t {cls}" data-id="{t['id']}"
             data-dono="{(d or {}).get('id') or ''}" data-proj="{(p or {}).get('id') or ''}">
  <div class="id">#{t['id']} · criada há {idade}d · {esc(t['status'])}
       {'· vence ' + t['vence'].strftime('%d/%m') if t['vence'] else ''}</div>
  <div class="tit">{esc(t['titulo'])}</div>
  {f'<div class="ctx">{ctx}</div>' if ctx else ''}
  <div class="sug">
    <div class="cx"><span class="lbl">dono sugerido</span><br>{dono}</div>
    <div class="cx"><span class="lbl">projeto</span><br>{proj}</div>
  </div>
  <div class="acoes">
    <button type="button" data-v="aceitar">aceitar</button>
    <button type="button" data-v="baixar">baixar</button>
    <button type="button" data-v="outro">outro →</button>
    <input placeholder="dono/projeto correto, ou o motivo">
  </div>
</div>"""

    grupos, atual, html = {}, None, []
    for t in tasks:
        g = t["projeto"] or "SEM PROJETO"
        grupos.setdefault(g, []).append(t)
    ordem = sorted(grupos, key=lambda g: (g != "SEM PROJETO", -len(grupos[g])))
    for g in ordem:
        html.append(f'<div class="grupo">{esc(g)} · {len(grupos[g])}</div>')
        html.extend(bloco(t) for t in grupos[g])

    return f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Triagem — tasks sem dono</title><style>{CSS}</style></head><body>
<header><h1>Triagem <span class="dot">·</span> tasks sem dono</h1>
<div class="meta">{agora:%d/%m %H:%M}</div></header>

<p class="intro"><b>{len(tasks)} tasks abertas não têm dono</b> — 43 por cento de tudo que está
em aberto. Task sem contato é invisível para os checks F e G e para o ritual semanal: uma
planning que rode sobre isto mostra menos da metade do trabalho, e parecendo completa.</p>
<p class="intro">Cada sugestão vem com <b>confiança à vista</b>. Passa rápido pelas
<span class="conf alta">alta</span> — são RACI já escrito ou nome que só pode ser uma pessoa.
Lê devagar as <span class="conf baixa">baixa</span>: ali é palpite meu.</p>

<div class="kpis">
  <div class="kpi"><b>{len(tasks)}</b>sem dono</div>
  <div class="kpi"><b>{len(sem_proj)}</b>sem projeto também</div>
  <div class="kpi"><b>{len(com_raci)}</b>com RACI já escrito no contexto</div>
  <div class="kpi"><b>{len(com_sug)}</b>com dono sugerido</div>
</div>

{''.join(html)}

<div class="barra" hidden>
  <span class="n">0 decididas</span>
  <button type="button" id="copiar">Copiar decisões</button>
  <button type="button" id="limpar">limpar</button>
  <textarea id="saida" hidden rows="6"></textarea>
</div>

<footer><b>Nada é gravado por esta tela.</b> Ela produz um texto — <code>ATRIBUIR</code>,
<code>BAIXAR</code> ou <code>OUTRO</code> por linha — que você cola no terminal; aplicar é
passo separado. As escolhas sobrevivem a um reload (localStorage), então dá para fazer em
duas sentadas.<br>
<b>“aceitar”</b> usa a sugestão como está. <b>“outro”</b> abre o campo ao lado para você
escrever o dono ou projeto certo. <b>“baixar”</b> é para o que não faz mais sentido existir.
</footer>
<script>{JS}</script></body></html>"""


def main():
    conn = conectar()
    cur = conn.cursor()
    tasks, contatos, projetos, freq, proj_do_contato = coletar(cur)
    tasks = sugerir(tasks, contatos, projetos, freq, proj_do_contato)
    open(SAIDA, "w").write(render(tasks))
    n_sug = sum(1 for t in tasks if t["sug_dono"])
    alta = sum(1 for t in tasks if (t["sug_dono"] or {}).get("conf") == "alta")
    print(f"→ {SAIDA}  ({len(tasks)} tasks · {n_sug} com dono sugerido · {alta} confiança alta)")
    conn.close()
    if "--quieto" not in sys.argv:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        try:
            from cockpit import abrir
            abrir(SAIDA)
        except Exception:
            import subprocess
            subprocess.run(["open", SAIDA])
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
