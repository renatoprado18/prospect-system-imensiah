#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""Gera ~/cockpit/indice.html — a porta da pasta do cockpit.

PEDIDO (Renato, 12/08/2026): *"este monte de arquivo não me diz nada"*. A pasta
tinha 53 arquivos soltos e a única pista do que cada um era estava no nome —
`pulso_andressa`, `regua_controle`, `panorama_11_08`. Nome de arquivo é
identificador, não informação: quem não escreveu a página não sabe o que ela é, e
quem escreveu esquece em uma semana.

O índice não inventa descrição: lê o `<title>` e a linha de subtítulo que cada
página já traz. Se uma peça chega aqui sem dizer o que é, o defeito está nela —
e aparece como "sem título" em vez de ficar escondido atrás de um nome bonito.

A CURADORIA É O ATO DE MOVER. Peça na raiz = viva; em `arquivo/` = histórico.
Sem metadado novo, sem JSON de estado pra manter sincronizado — a organização da
pasta É a classificação. Foi o que a arrumação de 12/08 estabeleceu
([[reference_cockpit_local_page]]).

O QUE ESTE ÍNDICE NÃO FAZ: decidir sozinho o que venceu. Tentei derivar validade
da data no nome (`prep_13ago`, `sandra_bizdev_14_08`) e o formato não é um só —
`13ago`, `14_08`, `06-08`, `11_08`, e peças sem data nenhuma que seguem válidas
(`posicionamento_ap`). Régua que acerta 70% num painel de navegação é pior que
régua nenhuma: esconde o que importa e o dono não descobre por quê.

Uso:  ./indice_cockpit.py            # gera e abre
      ./indice_cockpit.py --quieto   # gera sem abrir
"""
import html
import os
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

COCKPIT = os.path.expanduser("~/cockpit")
SAIDA = os.path.join(COCKPIT, "indice.html")
BRT = ZoneInfo("America/Sao_Paulo")

# Painéis que se refazem sozinhos pelo launchd. O terceiro campo é a IDADE MÁXIMA
# ESPERADA em dias — e ele existe porque a primeira versão desta tela acusou o
# `raci.html` de estar "parado há 2 dias" com o job funcionando perfeitamente: o
# RACI roda `Weekday 1` (segunda 7h30), então dois dias depois é o esperado, não
# atraso. Régua diária aplicada a ritual semanal FABRICA o atraso que denuncia —
# é a segunda vez que isso acontece com o RACI (a primeira foi `9f79c45`, na data
# do relatório). Cadência é do painel, nunca do calendário de quem olha.
VIVOS = {
    "index.html": ("Cockpit do dia", "com.almeidaprado.cockpit, a cada 5 min", 1),
    "board_hunt.html": ("Funil de originação de conselhos", "com.almeidaprado.boardhunt, a cada 5 min", 1),
    "board_hunt_resumo.html": ("Board hunt em 1 folha — pra enviar", "gerado junto com o board", 1),
    "raci.html": ("Planning semanal RACI", "com.almeidaprado.raci, segunda 7h30", 8),
}

# Telas com gerador próprio: podem ser refeitas a qualquer momento com um comando.
# O comando vai na tela — saber que existe e não achar como rodar dá no mesmo.
SOB_DEMANDA = {
    "nomes.html": "scripts/nomes.py",
    "telefones.html": "scripts/telefones.py",
    "duplicatas.html": "scripts/duplicatas.py",
    "mapa.html": "scripts/mapa.py",
    "sistema.html": "scripts/sistema.py",
    "memory_inventory.html": "scripts/memory_inventory.py",
    "triagem.html": "scripts/triagem_tasks.py",
    "devolutiva.html": "scripts/devolutiva.py",
    "memoria_particao.html": "scripts/gera_particao_memoria.py",
    # Saíram de VIVOS em 12/08: apagados a pedido do Renato depois que a tela
    # mostrou que estavam parados há 8 dias. Continuam listados porque o gerador
    # existe — o que morreu foi o hábito de olhar, não a ferramenta.
    "placar.html": "scripts/cos_agent/placar.py",
    "fatos.html": "scripts/cos_agent/fatos.py",
}


def esc(t):
    return html.escape(str(t) if t is not None else "")


def _texto(m):
    if not m:
        return ""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", m.group(1)))).strip()


def ler(caminho):
    """Título e subtítulo declarados pela própria página."""
    try:
        t = open(caminho, encoding="utf-8", errors="ignore").read(120000)
    except Exception:
        return "(ilegível)", ""
    titulo = _texto(re.search(r"<title[^>]*>(.*?)</title>", t, re.S | re.I)) \
        or _texto(re.search(r"<h1[^>]*>(.*?)</h1>", t, re.S | re.I))
    sub = _texto(re.search(
        r'<(?:h2|p|div|span)[^>]*class="[^"]*(?:sub|meta|lead|desc)[^"]*"[^>]*>(.*?)</', t, re.S | re.I))
    if len(sub) > 130:
        sub = sub[:130].rsplit(" ", 1)[0] + "…"
    return titulo or "sem título", sub


def idade(ts, hoje):
    d = (hoje - ts.date()).days
    return "hoje" if d == 0 else ("ontem" if d == 1 else f"há {d} dias")


def coletar(pasta, hoje):
    itens = []
    if not os.path.isdir(pasta):
        return itens
    for f in sorted(os.listdir(pasta)):
        p = os.path.join(pasta, f)
        if not os.path.isfile(p) or f.startswith("."):
            continue
        st = os.stat(p)
        ts = datetime.fromtimestamp(st.st_mtime, BRT)
        if f.endswith(".html"):
            titulo, sub = ler(p)
        else:
            titulo, sub = f, ""
        itens.append({"arq": f, "rel": os.path.relpath(p, COCKPIT), "titulo": titulo,
                      "sub": sub, "ts": ts, "idade": idade(ts, hoje),
                      "kb": round(st.st_size / 1024)})
    return itens


def linha(it, extra=""):
    sub = f'<div class="s">{esc(it["sub"])}</div>' if it["sub"] else ""
    ex = f'<div class="x">{extra}</div>' if extra else ""
    cls = "it ausente" if it.get("ausente") else "it"
    tag = it.get("aviso", "")
    # Sem arquivo no disco não há o que abrir: vira <div>, não link morto.
    ini = '<div class="%s">' % cls if it.get("ausente") else f'<a class="{cls}" href="{esc(it["rel"])}">'
    fim = "</div>" if it.get("ausente") else "</a>"
    return f"""{ini}
      <div class="tt">{esc(it['titulo'])}</div>{sub}{ex}{tag}
      <div class="f"><span class="ar">{esc(it['arq'])}</span><span class="dt">{esc(it['idade'])}</span></div>
    {fim}"""


CSS = """
  :root{--creme:#f6f1e7;--card:#fffdf8;--ink:#3a362f;--charcoal:#2b2925;--muted:#8a8072;
    --line:#e5ddcd;--dourado:#b8973e;--verde:#4a6035}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--creme);color:var(--ink);padding:28px 30px 70px;
    font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
  header{border-bottom:2px solid var(--dourado);padding-bottom:13px;margin-bottom:8px;
    display:flex;align-items:baseline;gap:16px;flex-wrap:wrap}
  h1{font-size:23px;color:var(--charcoal);font-weight:700}
  h1 .dot{color:var(--dourado)}
  header .meta{margin-left:auto;font-size:13px;color:var(--muted)}
  h2{font-size:12px;text-transform:uppercase;letter-spacing:.7px;color:var(--charcoal);
    margin:26px 0 4px;display:flex;align-items:baseline;gap:10px}
  h2 .n{color:var(--muted);font-weight:600;font-size:11px}
  .exp{font-size:12.5px;color:var(--muted);margin-bottom:11px;max-width:760px}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(268px,1fr));gap:11px;
    align-items:start}
  /* `.it` e não `a.it`: tela sob demanda ainda não gerada não tem o que abrir e
     vira <div> — com o seletor de âncora ela saía sem padding, com o texto
     encostado na borda. */
  .it{display:block;background:var(--card);border:1px solid var(--line);border-radius:10px;
    padding:12px 14px;text-decoration:none;color:inherit;box-shadow:0 1px 2px rgba(80,66,40,.05)}
  a.it:hover{border-color:var(--dourado);box-shadow:0 2px 7px rgba(80,66,40,.12)}
  .tt{font-weight:700;color:var(--charcoal);font-size:14.5px;line-height:1.25;
    overflow-wrap:anywhere}
  .s{font-size:12.5px;color:var(--muted);margin-top:4px}
  .x{font-size:11.5px;color:var(--verde);margin-top:6px;font-family:ui-monospace,Menlo,monospace}
  .f{display:flex;gap:8px;align-items:baseline;margin-top:8px;font-size:11px;color:var(--muted)}
  /* Nome de arquivo é uma palavra só e não quebra sozinho: sem isto o
     `Curriculum_Renato_de_Faria_e_Almeida_Prado_2026.docx` vaza por cima do card
     vizinho. */
  .f .ar{font-family:ui-monospace,Menlo,monospace;opacity:.72;overflow:hidden;
    text-overflow:ellipsis;white-space:nowrap;min-width:0}
  .f .dt{margin-left:auto;white-space:nowrap;flex:0 0 auto}
  .alerta{font-size:11.5px;color:#c0492f;margin-top:7px;font-weight:600}
  div.it.ausente{opacity:.62;border-style:dashed;box-shadow:none}
  div.it.ausente .tt{color:var(--muted)}
  details{margin-top:8px}
  details summary{cursor:pointer;font-size:12.5px;color:var(--dourado);font-weight:600;
    list-style:none;margin-bottom:11px}
  details summary::-webkit-details-marker{display:none}
  details summary::before{content:"▸ "} details[open] summary::before{content:"▾ "}
  footer{margin-top:34px;padding-top:12px;border-top:1px solid var(--line);
    font-size:12px;color:var(--muted);line-height:1.6;max-width:780px}
  footer code{font-family:ui-monospace,Menlo,monospace;font-size:11.5px}
"""


def render(hoje):
    raiz = coletar(COCKPIT, hoje)
    arq = coletar(os.path.join(COCKPIT, "arquivo"), hoje)
    ent = coletar(os.path.join(COCKPIT, "entregaveis"), hoje)

    vivos, demanda, pecas = [], [], []
    for it in raiz:
        if it["arq"] in (os.path.basename(SAIDA),) or it["arq"].endswith(".json"):
            continue
        if it["arq"] in VIVOS:
            rot, fonte, teto = VIVOS[it["arq"]]
            it["rotulo"], it["fonte"] = rot, fonte
            # Painel que se declara vivo e está velho é a pior das duas: mostra
            # número com cara de agora, e quem olha não tem como saber que o
            # produtor parou. Mas o teto é o DA CADÊNCIA DELE — ver o comentário
            # em VIVOS. [[feedback_medir_o_consumidor_certo]]
            dias = (hoje - it["ts"].date()).days
            if dias > teto:
                it["aviso"] = (f'<div class="alerta">⚠ parado há {dias} dias — '
                               f'{esc(fonte)}</div>')
            vivos.append(it)
        elif it["arq"] in SOB_DEMANDA:
            demanda.append(it)
        else:
            pecas.append(it)

    # Tela sob demanda sem arquivo no disco continua existindo como FERRAMENTA — o
    # arquivo é só a última vez que alguém a rodou. Listar apenas o que está no
    # disco faria o índice esconder metade do repertório: em 12/08 o Renato apagou
    # `telefones.html` e `triagem.html`, e com eles sumiu do índice a informação de
    # que existe um jeito de refazer as duas. Um índice que só enxerga arquivo é um
    # `ls` com CSS.
    presentes = {i["arq"] for i in demanda}
    for nome in SOB_DEMANDA:
        if nome in presentes:
            continue
        demanda.append({"arq": nome, "rel": nome,
                        "titulo": nome.replace(".html", "").replace("_", " ").capitalize(),
                        "sub": "nunca gerada nesta pasta — rode o comando pra criar",
                        "ts": None, "idade": "não gerada", "kb": 0, "ausente": True})
    demanda.sort(key=lambda x: (x.get("ausente", False), x["arq"]))

    # Peça viva mais recente primeiro: é o que a sessão de hoje produziu.
    pecas.sort(key=lambda x: x["ts"], reverse=True)
    arq.sort(key=lambda x: x["ts"], reverse=True)
    agora = datetime.now(BRT)

    def bloco(titulo, expl, itens, extra=lambda i: ""):
        if not itens:
            return ""
        corpo = "".join(linha(i, extra(i)) for i in itens)
        return (f'<h2>{titulo} <span class="n">{len(itens)}</span></h2>'
                f'<div class="exp">{expl}</div><div class="grid">{corpo}</div>')

    html_vivos = bloco(
        "Painéis vivos", "Refazem-se sozinhos — o que você vê é o estado de agora.",
        vivos, lambda i: esc(i["fonte"]))
    html_pecas = bloco(
        "Em uso", "Peças desta semana: preparação de reunião, material que ainda vai ser "
        "usado, decisão esperando você. Quando a data passa, descem pro arquivo.", pecas)
    html_demanda = bloco(
        "Telas sob demanda", "Têm gerador próprio: o conteúdo pode estar velho, mas um "
        "comando refaz com o dado de hoje.", demanda,
        lambda i: f'./{esc(SOB_DEMANDA[i["arq"]])}')
    html_ent = bloco(
        "Entregáveis", "O que sai da casa: currículo, atas, propostas.", ent)

    arq_corpo = "".join(linha(i) for i in arq)
    html_arq = (f'<h2>Arquivo <span class="n">{len(arq)}</span></h2>'
                f'<details><summary>peças já consumidas — reuniões que aconteceram, '
                f'triagens executadas, versões anteriores</summary>'
                f'<div class="grid">{arq_corpo}</div></details>') if arq else ""

    return f"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cockpit — índice</title>
<style>{CSS}</style>
</head>
<body>
<header>
  <h1>Cockpit <span class="dot">·</span> o que existe aqui</h1>
  <div class="meta">{agora:%d/%m %H:%M}</div>
</header>

{html_vivos}
{html_pecas}
{html_demanda}
{html_ent}
{html_arq}

<footer>
  Os títulos vêm das próprias páginas — este índice não descreve nada por fora, então
  peça que não diz o que é aparece como <b>sem título</b>.<br>
  <b>A curadoria é o ato de mover:</b> peça na raiz da pasta aparece em &ldquo;Em uso&rdquo;;
  movida pra <code>arquivo/</code>, desce pro fim. Não há lista a manter em outro lugar.<br>
  Refazer este índice: <code>./scripts/indice_cockpit.py</code> — roda junto com o cockpit.
</footer>
</body>
</html>"""


def main():
    open(SAIDA, "w").write(render(datetime.now(BRT).date()))
    print(f"→ {SAIDA}")
    if "--quieto" not in sys.argv:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        try:
            from cockpit import abrir
            abrir(SAIDA)
        except Exception:
            import subprocess
            subprocess.run(["open", SAIDA])


if __name__ == "__main__":
    sys.exit(main() or 0)
