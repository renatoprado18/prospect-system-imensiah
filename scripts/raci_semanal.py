#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""Gera ~/cockpit/raci.html — a planning semanal, um bloco por pessoa.

O RITUAL (desenhado com o Renato em 08/08/2026): na segunda, primeiro as
atualizações da semana, depois a consolidação. Ele pediu "algo parecido com uma
planning: o que tem para fazer, quem faz, e se há algo travando aquela pessoa".

O TERCEIRO ELEMENTO É O QUE MUDA A NATUREZA DA COISA. "O que tem para fazer" e
"quem faz" o banco responde sozinho. **"O que está travando" só a pessoa sabe** —
não está em tabela nenhuma. Isso transforma o ritual de relatório em CICLO: tem
ida e volta, e a resposta precisa ter para onde voltar. É também o que separa
isto de cobrança: perguntar o que trava é oferecer ajuda, não pedir satisfação.

DUAS VOZES, E A DIFERENÇA NÃO É ESTILO:

  · INTERNO (quem trabalha com ele — `tonha_role_contacts`): a pergunta é direta,
    "tem algo travando algum deles?". Ele é o gestor; perguntar é o trabalho.
  · TERCEIRO (advogado, cliente, sócio): a mensagem é de status com a porta
    aberta — "se precisares de algo meu para andar, me diz". Advogado não
    responde a planning semanal, e cobrar toda segunda queima relação. Registrado
    em [[feedback_raci_async_no_cobranca]].

O DEFAULT É TERCEIRO. `tonha_role_contacts` declara só 3 pessoas (Andressa,
Priscila, Piccino) e não cobre Thalita nem Amadeo. Na dúvida, tratar como
terceiro: errar para "não cobrar" custa menos que cobrar quem não devia. O
Renato troca o tom por pessoa na própria tela, e a escolha persiste.

O BLOCO DELE É DIFERENTE dos outros. Ele não manda mensagem para si mesmo: o
bloco do Renato é "o que está travado CONTIGO" — e é o mais valioso da planning,
porque é o único em que o gargalo é ele. Fica por último, depois de ele ver o que
distribuiu.

O QUE NÃO ENTRA: item sem entregável ("discutir o contrato" não é item; "mandar a
minuta revisada" é) e frente parada por decisão dele. Ver
[[feedback_raci_atividade_nao_entregavel]].

NADA É ENVIADO POR AQUI. Cada bloco tem um botão que copia a mensagem daquela
pessoa; o disparo é dele, um a um, escolhendo a quem perguntar.

Uso:  ./raci_semanal.py            # gera e abre
      ./raci_semanal.py --quieto
"""
import os
import re
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras

ROOT = "/Users/rap/prospect-system"
SAIDA = os.path.expanduser("~/cockpit/raci.html")
BRT = ZoneInfo("America/Sao_Paulo")
INTEL = "https://intel.almeida-prado.com"


def env(k):
    for l in open(f"{ROOT}/.env"):
        if l.startswith(k + "="):
            return l.split("=", 1)[1].strip().strip('"')
    return ""


def esc(t):
    return (str(t) if t is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# QUEM PODE SER COBRADO DIRETAMENTE. Decisão do Renato (08/08): só a Andressa.
# `tonha_role_contacts` traz também Priscila (contadora) e Piccino (advogado),
# mas eles são PRESTADORES — papel funcional não é a mesma coisa que "gente a
# quem eu pergunto o que está travando toda segunda". Cobrar quem presta serviço
# no ritmo de time interno é o que queima relação
# ([[feedback_raci_async_no_cobranca]]). Lista explícita porque é julgamento, e
# julgamento não se deriva de tabela.
INTERNOS = {25737}          # Andressa Santos


def coletar(cur, hoje):
    cur.execute("SELECT contact_id, role, nota FROM tonha_role_contacts")
    papeis = {r["contact_id"]: r for r in cur.fetchall()}
    internos = {k: v for k, v in papeis.items() if k in INTERNOS}

    cur.execute("SELECT contact_id FROM users WHERE id = 1")
    eu = (cur.fetchone() or {}).get("contact_id")

    # Itens abertos com dono. `on_hold` entra: é o que ESPERA alguém, e saber há
    # quanto tempo espera é metade da planning.
    cur.execute("""
        SELECT t.id, t.titulo, t.status, t.prioridade, t.project_id,
               p.nome AS frente, t.contact_id, c.nome AS dono,
               t.data_criacao::date AS criada, t.data_vencimento::date AS vence,
               t.on_hold_since::date AS desde
          FROM tasks t
          JOIN contacts c ON c.id = t.contact_id
          LEFT JOIN projects p ON p.id = t.project_id
         WHERE t.status IN ('pending','in_progress','on_hold')
         ORDER BY t.data_vencimento NULLS LAST, t.data_criacao
    """)
    itens = [dict(r) for r in cur.fetchall()]

    # O que mudou na semana: última troca com cada pessoa, em qualquer canal.
    ids = sorted({i["contact_id"] for i in itens})
    ultima = {}
    if ids:
        cur.execute("""
            SELECT DISTINCT ON (x.cid) x.cid, x.direcao, x.quando, x.canal
              FROM (
                SELECT m.contact_id cid, m.direcao,
                       COALESCE(m.enviado_em, m.recebido_em, m.criado_em) quando,
                       COALESCE(cv.canal,'whatsapp') canal
                  FROM messages m LEFT JOIN conversations cv ON cv.id = m.conversation_id
                 WHERE m.contact_id = ANY(%s)
                UNION ALL
                SELECT g.contact_id, CASE WHEN g.from_me THEN 'outgoing' ELSE 'incoming' END,
                       COALESCE(g.timestamp, g.criado_em), 'grupo'
                  FROM group_messages g WHERE g.contact_id = ANY(%s)
              ) x
             WHERE x.quando IS NOT NULL
             ORDER BY x.cid, x.quando DESC
        """, (ids, ids))
        ultima = {r["cid"]: dict(r) for r in cur.fetchall()}

    pessoas = {}
    for i in itens:
        p = pessoas.setdefault(i["contact_id"], {
            "id": i["contact_id"], "nome": i["dono"], "itens": [],
            "interno": i["contact_id"] in INTERNOS,
            "papel": (papeis.get(i["contact_id"]) or {}).get("nota", ""),
            "eu": i["contact_id"] == eu,
            "ultima": ultima.get(i["contact_id"]),
        })
        i["dias"] = (hoje - i["criada"]).days
        i["atrasada"] = bool(i["vence"] and i["vence"] < hoje)
        p["itens"].append(i)

    todas = sorted(pessoas.values(), key=lambda p: (p["eu"], -len(p["itens"])))

    # CORTE. Sem ele a planning teria 38 blocos — e 38 mensagens numa segunda
    # não é ritual, é enxurrada: exatamente o firehose que ele detesta. Entra
    # quem é interno (o ritual é sobre eles), quem tem 2+ itens (há conversa a
    # ter), ou quem tem item vencido (o prazo já falhou). Fica de fora quem tem
    # um item só, no prazo — perguntar ali é ruído.
    #
    # ⚠️ O CORTE APARECE NA TELA. Silenciar quem ficou de fora faria a planning
    # parecer completa quando cobre metade ([[feedback_regua_cobertura_parcial]]).
    def entra(p):
        return (p["eu"] or p["interno"] or len(p["itens"]) >= 2
                or any(i["atrasada"] for i in p["itens"]))
    dentro = [p for p in todas if entra(p)]
    fora = [p for p in todas if not entra(p)]
    return dentro, fora


def semanas(hoje):
    """(inicio_desta, fim_desta, fim_da_proxima).

    O ritual roda na SEGUNDA, então "esta semana" é a semana que começa na
    segunda. Gerado no fim de semana, aponta para a semana que vai começar — na
    prévia de sábado, "esta semana" já é a de segunda, que é o que ele vai olhar.
    """
    if hoje.weekday() >= 5:                     # sábado/domingo
        ini = hoje + timedelta(days=7 - hoje.weekday())
    else:
        ini = hoje - timedelta(days=hoje.weekday())
    return ini, ini + timedelta(days=6), ini + timedelta(days=13)


_STOP = {"de","da","do","das","dos","e","em","para","por","a","o","as","os",
          "rede","projeto","direta","adequação","apoio"}


def apelido(nome):
    """Nome curto e legível da frente, para o colchete da mensagem.

    "[Adequação de Despesas Pe]" truncado no meio da palavra é pior que nada.
    Corta pelo travessão (o lado direito costuma ser o nome próprio: "Exportação
    Direta — Café Jabô" -> "Café Jabô"), tira o parêntese e descarta as palavras
    de ligação, ficando com as duas mais significativas.
    """
    base = (nome or "").split("(")[0]
    if "—" in base:
        d = base.split("—")[-1].strip()
        if len(d) >= 4:
            base = d
    palavras = [w for w in base.split() if w.lower().strip(".,") not in _STOP]
    curto = " ".join(palavras[:2]) if palavras else base.strip()
    return curto[:20].strip()


def faixa(i, hoje, fim_esta, fim_prox):
    """Onde o item cai na mensagem. `None` = fica na tela, fora do texto.

    POR QUE UM ITEM PODE FICAR DE FORA (decisão do Renato, 08/08): 20 dos 102
    itens vencem depois de duas semanas — o Gate Aptus em 30/10, a 83 dias.
    Cobrar hoje o que vence em três meses não é zelo, é ruído: em duas semanas a
    pessoa aprende que a lista tem oito linhas que não são para agora, e passa a
    ler todas por alto. A tela mostra tudo; a mensagem leva o que é acionável.
    """
    if i["vence"] is None:
        return "sem_prazo"
    if i["vence"] <= fim_esta:                  # inclui os já vencidos
        return "esta"
    if i["vence"] <= fim_prox:
        return "prox"
    return None


def mensagem(p, hoje, marcas_prazo=None):
    """A mensagem pré-montada, em três seções. Interno é perguntado; terceiro é
    informado. A terceira seção — sem prazo — é pedido de acordo, não cobrança:
    item sem data não foi esquecido, ele NUNCA FOI COMBINADO com ninguém.
    Ideia do Renato, e melhor que a minha (que era filtrar por idade): a ausência
    de prazo vira a própria pergunta."""
    marcas_prazo = marcas_prazo or {}
    ini, fim_esta, fim_prox = semanas(hoje)
    multi_frente = len({i["project_id"] for i in p["itens"] if i["project_id"]}) > 1
    grupos = {"esta": [], "prox": [], "sem_prazo": []}
    for i in p["itens"]:
        f = faixa(i, hoje, fim_esta, fim_prox)
        if f:
            grupos[f].append(i)
    if not any(grupos.values()):
        return ""

    L = [f'{p["nome"].split()[0]}, bom dia —']
    # O TOM DA ABERTURA IMPORTA (Renato, 08/08): "fechando o quadro da semana,
    # estes são os teus" soa a auditoria — e o ritual não existe pra prestar
    # contas, existe pra destravar. "Acompanhamento do que temos" põe os dois do
    # mesmo lado da mesa, que é o que a pergunta seguinte pressupõe.
    L.append("fazendo um acompanhamento do que temos em aberto:" if p["interno"]
             else "fazendo um acompanhamento do que temos em aberto entre nós:")

    def linha(i):
        # QUANDO O COLCHETE AJUDA E QUANDO ATRAPALHA (medido em 08/08).
        #
        # 12 das 16 pessoas têm UMA frente só — para elas o prefixo repete em
        # toda linha algo que ela já sabe. Prefixo vira ruído, não contexto. Só
        # quem tem 2+ frentes precisa distinguir de qual item é qual.
        #
        # E o colchete do TÍTULO fica: "[Carambola]" diz mais à Andressa do que
        # "[Processos Cíveis]", que é o nome do MEU projeto. Ela reconhece a
        # empresa; a organização interna é minha. Tirar o do título para pôr o do
        # campo trocava informação por rótulo de sistema.
        titulo = i["titulo"]
        fr = ""
        if multi_frente and i["frente"] and not titulo.lstrip().startswith("["):
            fr = f'[{apelido(i["frente"])}] '
        t = f'• {fr}{titulo}'
        m = []
        if i["vence"] and i["vence"] < hoje:
            m.append(f'venceu {i["vence"]:%d/%m}')
        elif i["vence"]:
            m.append(f'{i["vence"]:%d/%m}')
        if i["status"] == "on_hold":
            m.append("aguardando")
        return t + (f' ({" · ".join(m)})' if m else "")

    if grupos["esta"]:
        L += ["", "ESTA SEMANA"] + [linha(i) for i in grupos["esta"]]
    if grupos["prox"]:
        L += ["", "SEMANA QUE VEM"] + [linha(i) for i in grupos["prox"]]
    if grupos["sem_prazo"]:
        # A MESMA SEÇÃO, DUAS VOZES. "me diz até quando você consegue" é pergunta
        # de gestor — cabe com quem trabalha comigo, não com sócia de consultoria
        # ou advogado. Para externo o item sem data é constatação com convite, e
        # a data continua sendo pedida sem virar prestação de contas.
        L += ["", "SEM PRAZO COMBINADO — me diz até quando você consegue:"
              if p["interno"] else
              "AINDA SEM DATA COMBINADA — quando tiver uma previsão, me avisa:"]
        for i in grupos["sem_prazo"]:
            imposto = marcas_prazo.get(str(i["id"]))
            L.append(f'• {i["titulo"]}' + (f' (preciso até {imposto})' if imposto else ""))

    L += ["", "Tem algo travando algum deles?" if p["interno"]
              else "Se precisar de alguma coisa minha para andar, é só me dizer."]
    return "\n".join(L)


CSS = """
:root{--charcoal:#2b2925;--ink:#3a362f;--muted:#8a8072;--creme:#f6f1e7;--card:#fffdf8;
 --line:#e5ddcd;--dourado:#b8973e;--soft:#e9dcb8;--quente:#c0492f;--quente-bg:#fae9e3;
 --hold:#c69a2e;--hold-bg:#f9f0d6;--eu:#3f6f92;--eu-bg:#e3edf4}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--creme);color:var(--ink);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;padding:26px 30px 70px}
header{display:flex;align-items:baseline;gap:16px;flex-wrap:wrap;border-bottom:2px solid var(--dourado);padding-bottom:14px;margin-bottom:8px}
h1{font-size:22px;font-weight:700;color:var(--charcoal)} h1 .dot{color:var(--dourado)}
.meta{margin-left:auto;font-size:13px;color:var(--muted)}
.intro{max-width:920px;margin:14px 0;font-size:13.5px}
.intro b{color:var(--charcoal)}
.kpis{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0 22px;font-size:12.5px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:9px;padding:7px 14px}
.kpi b{color:var(--charcoal);font-size:17px;display:block}
.p{background:var(--card);border:1px solid var(--line);border-left-width:4px;
 border-left-color:var(--soft);border-radius:10px;padding:15px 17px;margin-bottom:14px;max-width:920px}
.p.interno{border-left-color:var(--dourado)}
.p.eu{border-left-color:var(--eu);background:#fbfaf6}
.ph{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:3px}
.ph h2{font-size:17px;color:var(--charcoal)}
.tag{font-size:10.5px;border-radius:20px;padding:2px 9px;font-weight:700;text-transform:uppercase;letter-spacing:.5px}
.tag.interno{background:var(--soft);color:#6b551d} .tag.terceiro{background:#eee7d8;color:var(--muted)}
.tag.eu{background:var(--eu-bg);color:var(--eu)}
.ph .n{margin-left:auto;font-size:12px;color:var(--muted)}
.papel{font-size:11.5px;color:var(--muted);margin-bottom:9px}
.ult{font-size:11.5px;color:var(--muted);margin-bottom:10px}
ul.it{list-style:none;margin:8px 0} ul.it li{padding:4px 0;font-size:13.5px;border-bottom:1px dashed var(--line)}
ul.it li:last-child{border:0}
.it .fr{color:var(--muted);font-size:11.5px}
.tid{font:11px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);
 text-decoration:none;border-bottom:1px dotted var(--line);margin-right:3px}
.tid:hover{color:var(--dourado);border-bottom-color:var(--dourado)}
.frl{color:var(--muted);text-decoration:none}
.frl:hover{color:var(--dourado);text-decoration:underline}
.pid{font:10px ui-monospace,Menlo,monospace;opacity:.6}
.mk{font-size:10.5px;border-radius:4px;padding:1px 6px;margin-left:6px;font-weight:600}
.mk.atraso{background:var(--quente-bg);color:var(--quente)}
.mk.hold{background:var(--hold-bg);color:#8a6a12}
.mk.dias{background:#f0ece3;color:var(--muted)}
.msg{margin-top:12px}
.msg .lbl{font-size:10.5px;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);font-weight:700}
.msg textarea{width:100%;margin-top:5px;font:13.5px/1.5 inherit;color:var(--ink);background:#fbf7ee;
 border:1px solid var(--line);border-radius:8px;padding:10px 12px;resize:vertical}
.acoes{display:flex;gap:8px;margin-top:9px;align-items:center}
.acoes button{font:inherit;font-size:12.5px;border:1px solid var(--line);background:#fff;border-radius:7px;padding:4px 13px;cursor:pointer}
.acoes button.copiar{background:var(--dourado);color:#fff;border-color:var(--dourado);font-weight:600}
.acoes button:hover{border-color:var(--dourado)}
.acoes .voz{margin-left:auto;font-size:11.5px;color:var(--muted)}
ul.it li{position:relative}
.it .ac{position:absolute;right:0;top:2px;display:none;gap:4px;align-items:center;
 background:var(--card);padding:3px 4px;border:1px solid var(--line);border-radius:7px;
 box-shadow:0 2px 8px rgba(80,66,40,.10);z-index:3}
li:hover .ac, .it .ac.fixa{display:flex}
.it .ac button{font:inherit;font-size:11px;border:1px solid var(--line);background:#fff;
 border-radius:5px;padding:2px 8px;cursor:pointer;color:var(--muted)}
.it .ac button:hover{border-color:var(--dourado);color:var(--ink)}
.it .ac button.on{background:var(--dourado);color:#fff;border-color:var(--dourado);font-weight:600;opacity:1}
ul.it li.decidida{background:#f7f1e4;border-left:3px solid var(--dourado);padding-left:9px;margin-left:-12px}
ul.it li.decidida .selo{display:inline-block;font-size:10px;font-weight:700;text-transform:uppercase;
 letter-spacing:.5px;background:var(--dourado);color:#fff;border-radius:4px;padding:1px 7px;margin-left:7px}
.selo{display:none}
.it .ac input{font:inherit;font-size:11px;border:1px solid var(--line);border-radius:5px;
 padding:2px 6px;width:104px;background:#fbf7ee;display:none}
.it .ac input.on{display:inline-block}
.fora{margin:16px 0 4px;font-size:11.5px;color:var(--muted);font-style:italic}
.decisoes{position:sticky;bottom:14px;display:flex;gap:10px;align-items:center;background:var(--card);
 border:1px solid var(--dourado);border-radius:11px;padding:11px 16px;margin-top:20px;max-width:920px;
 box-shadow:0 6px 18px rgba(0,0,0,.13);z-index:9}
.decisoes[hidden]{display:none}
.decisoes .n{margin-right:auto;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:var(--dourado)}
.decisoes button{font:inherit;font-size:13px;font-weight:600;border:0;border-radius:7px;padding:6px 15px;cursor:pointer}
#dcopiar{background:var(--dourado);color:#fff} #dlimpar{background:transparent;color:var(--muted)}
footer{margin-top:26px;color:var(--muted);font-size:11.5px;max-width:920px;line-height:1.65}
footer b{color:var(--ink)}
"""

JS = """
const DK='raci_dec', D=JSON.parse(localStorage.getItem(DK)||'{}');
function pintaDec(){
  document.querySelectorAll('li[data-id]').forEach(li=>{
    const d=D[li.dataset.id];
    li.querySelectorAll('.ac button').forEach(b=>b.classList.toggle('on', !!d && d.v===b.dataset.v));
    li.classList.toggle('decidida', !!d);
    const selo=li.querySelector('.selo');
    if(selo) selo.textContent = d ? (d.v + (d.extra ? ' → '+d.extra : '')) : '';
    const ac=li.querySelector('.ac'); if(ac) ac.classList.toggle('fixa', !!d);
  });
  const n=Object.keys(D).length, bar=document.querySelector('.decisoes');
  bar.hidden=n===0; bar.querySelector('.n').textContent=n+(n===1?' alteração':' alterações');
}
document.querySelectorAll('li[data-id] .ac button').forEach(b=>{
  b.addEventListener('click',()=>{
    const li=b.closest('li'), id=li.dataset.id, v=b.dataset.v;
    const inp=li.querySelector('.ac input');
    if(D[id] && D[id].v===v){ delete D[id]; li.querySelector('.ac').classList.remove('fixa'); if(inp) inp.classList.remove('on'); }
    else {
      D[id]={v, extra:(inp||{}).value||'', t:li.dataset.t||''};
      li.querySelector('.ac').classList.add('fixa');           // fica visível após escolher
      if(inp && v!=='baixar'){ inp.classList.add('on'); inp.focus(); }
      else if(inp) inp.classList.remove('on');
    }
    localStorage.setItem(DK,JSON.stringify(D)); pintaDec();
  });
});
document.querySelectorAll('li[data-id] .ac input').forEach(inp=>{
  inp.addEventListener('input',()=>{const id=inp.closest('li').dataset.id;
    if(D[id]){D[id].extra=inp.value; localStorage.setItem(DK,JSON.stringify(D)); pintaDec();}});
});
document.getElementById('dcopiar').addEventListener('click',async()=>{
  const L=[];
  document.querySelectorAll('li[data-id]').forEach(li=>{
    const id=li.dataset.id, d=D[id]; if(!d) return;
    const t=(li.dataset.t||'').slice(0,42);
    if(d.v==='baixar') L.push(`BAIXAR ${id}  # ${t}`);
    else if(d.v==='dono') L.push(`DONO ${id} -> ${d.extra||'(quem?)'}  # ${t}`);
    else if(d.v==='frente') L.push(`FRENTE ${id} -> ${d.extra||'(qual?)'}  # ${t}`);
    else if(d.v==='data') L.push(`DATA ${id} -> ${d.extra||'(quando?)'}  # ${t}`);
    else if(d.v==='prazo') L.push(`PRAZO-IMPOSTO ${id} -> ${d.extra||'(quando?)'}  # ${t}`);
  });
  const txt=L.join('\\n'), b=document.getElementById('dcopiar');
  try{ await navigator.clipboard.writeText(txt); b.textContent='copiado ('+L.length+')'; }
  catch(e){ b.textContent='sem permissão'; console.log(txt); }
  setTimeout(()=>b.textContent='Copiar alterações',1800);
});
document.getElementById('dlimpar').addEventListener('click',()=>{
  Object.keys(D).forEach(k=>delete D[k]); localStorage.removeItem(DK); pintaDec();
});
pintaDec();

document.querySelectorAll('.p').forEach(bl=>{
  const ta=bl.querySelector('textarea'), bt=bl.querySelector('.copiar'), tg=bl.querySelector('.trocar');
  if(bt) bt.addEventListener('click',async()=>{
    const antes=bt.textContent;
    try{ await navigator.clipboard.writeText(ta.value); bt.textContent='copiado ✓'; }
    catch(e){ ta.select(); bt.textContent='selecionado — ⌘C'; }
    setTimeout(()=>bt.textContent=antes,1700);
  });
  if(tg) tg.addEventListener('click',()=>{
    const alt=bl.dataset.alt, atual=ta.value;
    ta.value=alt; bl.dataset.alt=atual;
    bl.classList.toggle('interno');
    const t=bl.querySelector('.tag');
    t.textContent = t.textContent==='interno' ? 'terceiro' : 'interno';
    t.className = 'tag ' + t.textContent;
  });
});
"""


def render(pessoas, fora, hoje):
    agora = datetime.now(BRT)
    n_itens = sum(len(p["itens"]) for p in pessoas)
    n_hold = sum(1 for p in pessoas for i in p["itens"] if i["status"] == "on_hold")
    n_atraso = sum(1 for p in pessoas for i in p["itens"] if i["atrasada"])
    internos = [p for p in pessoas if p["interno"]]

    blocos = []
    for p in pessoas:
        classe = "eu" if p["eu"] else ("interno" if p["interno"] else "")
        rot = "você" if p["eu"] else ("interno" if p["interno"] else "terceiro")

        ini, fim_esta, fim_prox = semanas(hoje)
        ROT = {"esta": "esta semana", "prox": "semana que vem",
               "sem_prazo": "sem prazo combinado", None: "depois"}
        buckets = {"esta": [], "prox": [], "sem_prazo": [], None: []}
        for i in p["itens"]:
            buckets[faixa(i, hoje, fim_esta, fim_prox)].append(i)

        def li(i, na_msg):
            mk = []
            if i["vence"] and i["vence"] < hoje:
                mk.append(f'<span class="mk atraso">venceu {i["vence"]:%d/%m}</span>')
            elif i["vence"]:
                mk.append(f'<span class="mk dias">{i["vence"]:%d/%m}</span>')
            if i["status"] == "on_hold":
                mk.append('<span class="mk hold">aguardando</span>')
            mk.append(f'<span class="mk dias">{i["dias"]}d</span>')
            # a ação de prazo só aparece onde faz sentido: item sem data
            extra = ('<button type="button" data-v="prazo">prazo</button>'
                     if i["vence"] is None else
                     '<button type="button" data-v="data">data</button>')
            tit = i["titulo"]
            # O #id e a frente viram LINK — mas só na tela. Na mensagem eles não
            # entram: identificador interno não significa nada para quem recebe,
            # e link que a pessoa não pode abrir é ruído com cara de rigor.
            # A tarefa não tem página própria no INTEL, então aponta para a
            # frente, que é onde ela aparece.
            alvo = f'{INTEL}/projetos/{i["project_id"]}' if i["project_id"] else None
            idl = (f'<a class="tid" href="{alvo}" target="_blank" rel="noopener">#{i["id"]}</a>'
                   if alvo else f'<span class="tid">#{i["id"]}</span>')
            frl = (f'<a class="frl" href="{alvo}" target="_blank" rel="noopener">'
                   f'{esc(apelido(i["frente"]))} <span class="pid">#{i["project_id"]}</span></a>'
                   if alvo else '<span class="frl">sem frente</span>')
            return (f'<li data-id="{i["id"]}" data-t="{esc(tit)}">'
                    f'{idl} {esc(tit)}<span class="selo"></span>{"".join(mk)}'
                    f'<div class="fr">{frl}</div>'
                    f'<div class="ac"><button type="button" data-v="baixar">baixar</button>'
                    f'<button type="button" data-v="dono">dono</button>'
                    f'<button type="button" data-v="frente">frente</button>'
                    f'{extra}<input placeholder="valor"></div></li>')

        partes = []
        for chave in ("esta", "prox", "sem_prazo", None):
            itens_b = buckets[chave]
            if not itens_b:
                continue
            rot_bucket = ROT[chave]
            if chave is None:
                partes.append(f'<div class="fora">↓ {len(itens_b)} '
                              f'{"item" if len(itens_b)==1 else "itens"} de prazo mais longo — '
                              f'ficam aqui, <b>fora da mensagem</b></div>')
            else:
                partes.append(f'<div class="fora" style="font-style:normal;font-weight:700;'
                              f'color:var(--dourado);text-transform:uppercase;letter-spacing:.6px">{rot_bucket}</div>')
            partes.append('<ul class="it">' + "".join(li(i, chave is not None) for i in itens_b) + '</ul>')
        lis_html = "".join(partes)

        u = p["ultima"]
        ult = ""
        if u:
            d = (hoje - u["quando"].date()).days
            quem = "você escreveu" if u["direcao"] == "outgoing" else "ela/ele escreveu"
            ult = (f'<div class="ult">última troca: {quem} há {d}d '
                   f'({u["quando"]:%d/%m} · {esc(u["canal"])})</div>')

        if p["eu"]:
            corpo = (f'{ult}{lis_html}'
                     '<div class="msg"><span class="lbl">este bloco não vira mensagem</span>'
                     '<div class="ult" style="margin-top:6px">É o que está travado com você — '
                     'o único bloco em que o gargalo é seu. Vem por último de propósito: '
                     'depois de ver o que distribuiu.</div></div>')
        else:
            msg = mensagem(p, hoje)
            p2 = dict(p); p2["interno"] = not p["interno"]
            alt = mensagem(p2, hoje)
            corpo = (f'{ult}{lis_html}'
                     f'<div class="msg"><span class="lbl">mensagem para {esc(p["nome"].split()[0])}</span>'
                     f'<textarea rows="{min(4 + len(p["itens"]), 14)}">{esc(msg)}</textarea>'
                     f'<div class="acoes"><button type="button" class="copiar">copiar</button>'
                     f'<button type="button" class="trocar">trocar voz</button>'
                     f'<span class="voz">{"pergunta direta" if p["interno"] else "oferta, não cobrança"}</span>'
                     f'</div></div>')

        papel = f'<div class="papel">{esc(p["papel"])}</div>' if p["papel"] else ""
        blocos.append(f'''<div class="p {classe}" data-alt="{esc(alt) if not p["eu"] else ""}">
 <div class="ph"><h2>{esc(p["nome"])}</h2><span class="tag {rot}">{rot}</span>
   <span class="n">{len(p["itens"])} {"item" if len(p["itens"])==1 else "itens"}</span></div>
 {papel}{corpo}</div>''')

    return f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Planning semanal — RACI</title><style>{CSS}</style></head><body>
<header><h1>Planning semanal <span class="dot">·</span> quem tem o quê, e o que trava</h1>
<div class="meta">{agora:%d/%m %H:%M}</div></header>

<p class="intro">Um bloco por pessoa, com <b>os itens dela</b> e uma mensagem pronta.
Tu revisas, escolhes a quem perguntar, copias e disparas — <b>nunca a lista completa
para todos</b>: no grupo, cada um lê o item do outro e ninguém se sente cobrado.</p>
<p class="intro">A voz muda conforme quem recebe. <b>Interno</b> leva pergunta direta
("tem algo travando?"). <b>Terceiro</b> leva status com a porta aberta — advogado não
responde a planning semanal, e cobrar toda segunda queima a relação. O botão
<b>trocar voz</b> corrige quando eu classificar errado.</p>

{"".join(blocos)}

<div class="decisoes" hidden>
  <span class="n">0 alterações</span>
  <button type="button" id="dcopiar">Copiar alterações</button>
  <button type="button" id="dlimpar">limpar</button>
</div>

<footer><b>Nada é enviado por esta tela.</b> Cada bloco copia só a mensagem daquela
pessoa. Quando alguém responder <i>"está travado porque preciso de X de ti"</i>, isso
vira task tua — foi assim que combinamos, e é o que dá sentido a perguntar.<br>
<b>Fora do corte desta semana: {len(fora)} pessoas</b> — cada uma com um item só, no prazo
({esc(", ".join(p["nome"].split()[0] for p in fora[:12]))}{"…" if len(fora)>12 else ""}).
Perguntar ali seria ruído, mas o corte fica à vista: planning que esconde o que deixou de
fora parece completa cobrindo metade.<br>
<b>Só {len(internos)} pessoas estão declaradas como internas</b> em
<code>tonha_role_contacts</code>. Todo o resto entra como terceiro por padrão: errar
para "não cobrar" custa menos que cobrar quem não devia.
</footer>
<script>{JS}</script></body></html>"""


def main():
    conn = psycopg2.connect(env("DATABASE_URL"), cursor_factory=psycopg2.extras.RealDictCursor)
    cur = conn.cursor()
    hoje = datetime.now(BRT).date()
    pessoas, fora = coletar(cur, hoje)
    open(SAIDA, "w").write(render(pessoas, fora, hoje))
    n = sum(len(p["itens"]) for p in pessoas)
    print(f"→ {SAIDA}  ({len(pessoas)} pessoas · {n} itens · {len(fora)} fora do corte)")
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
