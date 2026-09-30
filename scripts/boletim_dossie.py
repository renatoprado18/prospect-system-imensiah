#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""Camada DETERMINISTA do dossiê do Boletim de Inteligência. Zero LLM, zero custo.

POR QUE. O boletim de 20/09 entregava manchete + julgamento, e o Renato pediu
"análise mais profunda de todo o contexto de cada matéria, seus protagonistas".
A resposta honesta é que a parte mais valiosa disso não precisa de modelo: quem
são os protagonistas, se ele os conhece, quem do círculo dele está mais perto, e
quantas vezes aquela entidade já apareceu — tudo isso é consulta. O LLM só é
necessário pro "por que isso importa", que é um parágrafo e fica pra depois.

O QUE ESTE MÓDULO NÃO FAZ, de propósito: não interpreta, não conclui, não
qualifica relevância. Ele diz "este nome apareceu no título e casa com a ficha
#18707" — nunca "logo você deveria falar com ele". A lição de 20/09 é exatamente
essa: o erro anterior não foi falta de dado, foi conclusão em cima de dado que o
banco já contradizia ([[feedback_fonte_unica_de_fatos]]).

REUSA a máquina de entidade/dedup do detector (`_norm`, `_entity_tokens`,
`_story_tokens`) em vez de reescrever — é a mesma que já sobreviveu ao falso
positivo do "del Valle" ([[feedback_filtro_vocabulario_errado_falha_calado]]).

O QUE ACRESCENTA ao `cruzamento_noticia_contato`: o detector casa contatos pela
ENTIDADE DO WATCHER (o alvo vigiado). Aqui se casa pelos NOMES CITADOS NO PRÓPRIO
TÍTULO — que é como "Ricardo Magalhães Gomes" numa manchete da Westwing aparece,
apesar de nenhum watcher vigiar o nome dele.

Uso:  DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 scripts/boletim_dossie.py [--dias 30] [--out arquivo.json]
      DB_TARGET=local scripts/boletim_dossie.py
"""
import argparse
import json
import os
import re
import sys
from collections import defaultdict

import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
from services.detectors.detector_cruzamentos import (  # noqa: E402
    _norm, _entity_tokens, _story_tokens, _STOP,
)

# ── EXTRAÇÃO DE NOME PRÓPRIO ────────────────────────────────────────────────
# Manchete em PT-BR capitaliza a primeira palavra da frase, então a 1ª posição é
# descartada como candidata: "Justiça convoca credores..." não é a entidade
# "Justiça". Conectores minúsculos entram DENTRO de um nome ("Almeida Prado",
# "Câmara de Comércio") mas nunca o iniciam.
CONECTOR = {"de", "da", "do", "das", "dos", "e", "del", "van", "von", "the"}

# Capitalizado e ainda assim não é entidade: dias, meses, instituições genéricas
# e o vocabulário de manchete econômica. Sem isto o dossiê "encontra" Justiça,
# Governo e Brasil em metade das matérias e o sinal morre no ruído.
NAO_ENTIDADE = {
    "justica", "governo", "brasil", "estado", "uniao", "republica", "congresso",
    "senado", "camara", "ministerio", "receita", "banco", "central", "bolsa",
    "ibovespa", "selic", "pib", "cpi", "cpmi", "stf", "stj", "tse", "tst", "tcu",
    "cvm", "cade", "bndes", "ipca", "igpm", "dolar", "euro", "real", "reais",
    "janeiro", "fevereiro", "marco", "abril", "maio", "junho", "julho", "agosto",
    "setembro", "outubro", "novembro", "dezembro",
    "segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo",
    "veja", "entenda", "saiba", "analise", "exclusivo", "urgente", "opiniao",
    "mercados", "empresas", "negocios", "economia", "politica", "mundo",
    "conselho", "diretoria", "assembleia", "presidente", "diretor", "ceo", "cfo",
    "nova", "novo", "novos", "novas", "apos", "sobre", "contra", "para", "como",
    # afloraram ao liberar a posição 0 (token único >=6 chars) — vocabulário de
    # manchete econômica, não entidade. Sem isto viram "protagonista" sem ser.
    "multas", "reajuste", "credores", "credor", "acionistas", "acionista",
    "justica", "mercado", "balanco", "prejuizo", "receita", "lucro", "divida",
    "relatorio", "pesquisa", "estudo", "decisao", "medida", "projeto", "programa",
    "aumento", "queda", "alta", "baixa", "leilao", "consulta", "audiencia",
}

# GEOGRAFIA — a primeira rodada devolveu "São Paulo → 6 conhecidos" e
# "Curitiba → 2 conhecidos": nome de cidade casando o campo `empresa` de quem
# tem a cidade no nome da empresa. Não é vínculo, é colisão de token — e pior,
# uma colisão que PARECE achado. Cidade/estado/país nunca é a entidade da
# matéria pra efeito de protagonista.
GEOGRAFIA = {
    "paulo", "paulista", "curitiba", "parana", "janeiro", "horizonte", "minas",
    "gerais", "bahia", "pernambuco", "ceara", "goias", "grosso", "catarina",
    "grande", "alegre", "brasilia", "campinas", "santos", "ribeirao", "preto",
    "araguari", "guaxupe", "europa", "europeu", "europeia", "austria", "viena",
    "china", "eua", "estados", "unidos", "portugal", "espanha", "franca",
    "alemanha", "japao", "india", "peru", "colombia", "chile", "argentina",
    "vietna", "sudoeste", "nordeste", "sudeste", "norte", "sul", "oeste", "leste",
}

# Palavra de negócio genérica que não distingue entidade nenhuma. Some ao `_STOP`
# do detector (que já cobre sufixo societário) — sem isto, "Desenvolvimento do
# Consumidor" casa 6 fichas por "desenvolvimento".
GENERICO = {
    "contratos", "contrato", "desenvolvimento", "consumidor", "consumidores",
    "regulamento", "instantaneo", "operacao", "resgate", "plano", "planos",
    "fatia", "debate", "encontro", "reuniao", "evento", "premio", "premiacao",
    "gestao", "negocio", "industria", "comercio", "servico", "mercados",
    # achados na auditoria da saída real (20/09): passaram o filtro de frequência
    # porque casam poucas fichas, mas ainda assim não identificam entidade.
    # "Administração da Motiva" trazia 5 pessoas — 4 pelo token "administracao".
    "inovacao", "mudanca", "mudancas", "master", "administracao", "risco", "caso",
}

# Sem isto, "ministra da Casa Civil" devolvia a PESSOA "Casa Civil" e
# "Companhia Brasileira de Alumínio" virava protagonista com cargo "ministração
# da" (o `\b` acima matou o segundo; este mata o primeiro).
INSTITUICAO = {
    "casa", "civil", "ambiente", "departamento", "economico", "companhia",
    "secretaria", "agencia", "federal", "nacional", "publico", "publica",
    "instituto", "fundacao", "universidade", "sindicato", "federacao",
    "associacao", "camara", "tribunal", "supremo", "superior", "regional",
    "comissao", "conselho", "assembleia", "plenario", "corte", "vara",
    "comite", "participacao", "sociedade", "episodio", "argumentos",
}

# Coletivo/descritor que a auditoria de 20/09 pegou entrando como PESSOA:
# "Godke Advogados", "Fruki Bebidas", "Interino da Fictor Alimentos",
# "Boletim Metrópoles Frequência", "Chefia da Embrapa Trigo". Pessoa não tem
# "Advogados" no nome; empresa tem.
COLETIVO = {
    "advogados", "advocacia", "associados", "consultores", "auditores",
    "bebidas", "alimentos", "invest", "investimentos", "boletim", "chefia",
    "interino", "interina", "frequencia", "editora", "seguros", "capital",
    "corretora", "distribuidora", "transportes", "construtora", "incorporadora",
}

TICKER_RE = re.compile(r"\(([A-Z]{4}\d{1,2})\)")


def veiculo_e_nucleo(titulo):
    """Separa ' - Veículo' do fim. Mesma régua do _story_tokens: só corta o
    último segmento e só se for curto (nome de veículo, não conteúdo)."""
    partes = re.split(r"\s[-–—|]\s", titulo)
    if len(partes) > 1 and len(partes[-1].split()) <= 5:
        return partes[-1].strip(), " ".join(partes[:-1]).strip()
    return None, titulo


def extrai_entidades(titulo):
    """Candidatos a nome próprio no título. Devolve (nomes, tickers).

    Conservador de propósito: prefere PERDER uma entidade a inventar uma. Um
    nome perdido é uma linha a menos no dossiê; um nome inventado é uma
    afirmação errada sobre gente real.
    """
    _, nucleo = veiculo_e_nucleo(titulo)
    tickers = TICKER_RE.findall(nucleo)
    nucleo = TICKER_RE.sub(" ", nucleo)

    # tokeniza preservando a capitalização e a posição na frase
    tokens = re.findall(r"[A-ZÀ-Ý][\wÀ-ÿ'’&.]*|[a-zà-ÿ][\wÀ-ÿ'’]*|[^\w\s]", nucleo)
    nomes, atual, inicio = [], [], None
    for i, t in enumerate(tokens):
        cap = t[:1].isupper()
        conector = _norm(t) in CONECTOR
        if cap:
            if not atual:
                inicio = i
            atual.append(t)
        elif conector and atual:
            atual.append(t)          # conector só continua um nome já aberto
        else:
            if atual:
                nomes.append((atual, inicio))
                atual, inicio = [], None
    if atual:
        nomes.append((atual, inicio))

    # 20/09 — a regra antiga descartava o token da POSIÇÃO 0 pra não ler
    # "Justiça convoca credores" como a entidade "Justiça". O controle positivo
    # mostrou o preço: "Eduardo Marson Ferreira assume..." virava "Marson
    # Ferreira" e "Fernanda Borges detalha..." virava "Borges" — um token só,
    # reprovado no matcher. Perdia justamente o protagonista da manchete.
    # Régua nova: uma SEQUÊNCIA de 2+ capitalizadas vale mesmo começando em 0
    # (é nome de gente); token ÚNICO em 0 passa só pela mesma peneira do resto
    # (>=6 chars e fora de NAO_ENTIDADE), que é o que de fato mata "Justiça".
    out = []
    for grupo, pos in nomes:
        # conector sobrando na borda não faz parte do nome
        while grupo and _norm(grupo[-1]) in CONECTOR:
            grupo.pop()
        # "Contratos do Grupo Fictor" saía como uma entidade só, porque
        # "Contratos" abre a frase e o conector "do" costura o resto. Tira o
        # prefixo genérico e fica a entidade de verdade ("Grupo Fictor"), em vez
        # de descartar a linha inteira.
        while len(grupo) > 1 and (_norm(grupo[0]) in GENERICO
                                  or _norm(grupo[0]) in NAO_ENTIDADE):
            grupo.pop(0)
        while grupo and _norm(grupo[0]) in CONECTOR:
            grupo.pop(0)
        if not grupo:
            continue
        # Nome próprio em manchete tem no máximo ~5 palavras. Acima disso é
        # fragmento de frase costurado por conectores — foi assim que saiu
        # "Detalhamento da Receita de Fictor Alimentos SA BMFBOVESPA".
        if len(grupo) > 5:
            continue
        nome = " ".join(grupo)
        toks = _entity_tokens(nome)
        if not toks or all(t in NAO_ENTIDADE or t in GEOGRAFIA or t in GENERICO
                           for t in toks):
            continue
        # 1 palavra só passa se for longa e específica (Westwing, Flamarpar,
        # Petrobras); 2+ palavras é o caso normal de nome de pessoa.
        if len(grupo) == 1 and (len(toks[0]) < 6 or toks[0] in NAO_ENTIDADE):
            continue
        if nome not in out:
            out.append(nome)
    return out, tickers


# ── CONSULTAS ───────────────────────────────────────────────────────────────
def _person_tokens(nome):
    """Tokens de NOME DE PESSOA. Não reusa `_entity_tokens` porque o piso de 4
    chars dele apaga sobrenome curto: "Shehroz Ali" ficava só com 'shehroz' e o
    matcher (que exige dois extremos) desistia calado. Piso 3 aqui, e a precisão
    volta pela exigência de que algum token seja longo (>=5) — 'Ali Sá' sozinho
    não consulta nada."""
    toks = [t for t in re.findall(r"[a-z0-9]+", _norm(nome))
            if len(t) >= 3 and t not in NAO_ENTIDADE and t not in CONECTOR]
    return toks if any(len(t) >= 5 for t in toks) else []


def casa_pessoa(cur, nome):
    """Ficha cujo NOME casa com o candidato. Exige os dois extremos (primeiro e
    último token significativos) pra não colar homônimo parcial: 'Marcos Ribeiro'
    não deve casar 'Marcos Antonio Souza Ribeiro' por acidente — mas casa, e
    corretamente, porque os dois extremos batem."""
    toks = _person_tokens(nome)
    if len(toks) < 2:
        return []
    # TODOS os tokens do nome citado têm de estar na ficha, não só os extremos.
    # Com só os extremos, "Eduardo Marson Ferreira" casava também três fichas de
    # "José Eduardo Ferreira" (#3025/#22660/#25379) — três pessoas erradas
    # apresentadas como protagonista da matéria. A direção do subconjunto é a
    # certa: tokens-do-título ⊆ tokens-da-ficha, então "Marson Ferreira" (sem o
    # primeiro nome) segue casando "Eduardo Marson Ferreira".
    toks = toks[:4]
    cond = " AND ".join([r"unaccent(lower(nome)) ~* ('\y' || %s)"] * len(toks))
    cur.execute(
        f"""
        SELECT id, nome, coalesce(cargo,'') AS cargo, coalesce(empresa,'') AS empresa,
               circulo, ultimo_contato::date::text AS ultimo_contato,
               (SELECT count(*) FROM messages m WHERE m.contact_id = c.id) AS msgs
        FROM contacts c
        WHERE {cond}
        ORDER BY circulo NULLS LAST, id
        LIMIT 5
        """,
        tuple(toks),
    )
    return [dict(r) for r in cur.fetchall()]


# Quantas fichas um token pode casar antes de ser considerado genérico. A régua
# é MEDIDA, não enumerada: listar "inovacao", "master", "receita" à mão não
# escala e a próxima palavra genérica sempre passa. Se o token casa dezenas de
# empresas diferentes no CRM, ele não identifica entidade nenhuma — é isso que
# "genérico" significa, operacionalmente. 12 é folgado: as entidades reais que
# importam aqui (Motiva, Westwing, Guaxupé, Flamarpar) casam 1 a 6.
MAX_FICHAS_POR_TOKEN = 12


def casa_empresa(cur, nome, limite=6):
    """Quem o Renato conhece DENTRO da entidade. Word-start com token >=5 e o
    token tem de ser DISTINTIVO (ver MAX_FICHAS_POR_TOKEN)."""
    toks = [t for t in _entity_tokens(nome)
            if len(t) >= 5
            and t not in NAO_ENTIDADE and t not in GEOGRAFIA
            and t not in GENERICO and t not in _STOP]
    if not toks:
        return []
    distintivos = []
    for t in toks:
        cur.execute(
            "SELECT count(*) AS n FROM contacts "
            "WHERE empresa IS NOT NULL AND unaccent(lower(empresa)) ~* %s",
            (r"\y" + t,),
        )
        n = cur.fetchone()["n"]
        if 0 < n <= MAX_FICHAS_POR_TOKEN:
            distintivos.append(t)
    if not distintivos:
        return []
    cur.execute(
        """
        SELECT id, nome, coalesce(cargo,'') AS cargo, coalesce(empresa,'') AS empresa,
               circulo, ultimo_contato::date::text AS ultimo_contato
        FROM contacts
        WHERE empresa IS NOT NULL AND unaccent(lower(empresa)) ~* ANY(%s)
        ORDER BY circulo NULLS LAST, nome
        LIMIT %s
        """,
        ([r"\y" + t for t in distintivos], limite),
    )
    return [dict(r) for r in cur.fetchall()]


def historico_watcher(cur, watcher_id, titulo_atual):
    """Quantas vezes esta frente já rendeu manchete, e há quanto tempo. É o que
    diz se a notícia de hoje é um pico ou a 40ª repetição — e o boletim não tinha
    como saber isso antes."""
    cur.execute(
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE hit_at > now() - interval '30 days') AS d30,
               count(*) FILTER (WHERE hit_at > now() - interval '7 days') AS d7,
               min(hit_at)::date::text AS primeiro,
               max(hit_at)::date::text AS ultimo
        FROM project_news_hits WHERE watcher_id = %s
        """,
        (watcher_id,),
    )
    h = dict(cur.fetchone())
    # histórias anteriores DISTINTAS (dedup por token-set, não por título literal)
    cur.execute(
        """
        SELECT DISTINCT ON (title) title, published_at::date::text AS data
        FROM project_news_hits WHERE watcher_id = %s AND title <> %s
        ORDER BY title, published_at DESC
        """,
        (watcher_id, titulo_atual),
    )
    vistas, anteriores = [_story_tokens(titulo_atual)], []
    for r in sorted(cur.fetchall(), key=lambda r: r["data"] or "", reverse=True):
        tk = _story_tokens(r["title"])
        if not tk or any(len(tk & v) / min(len(tk), len(v)) >= 0.6 for v in vistas if v):
            continue
        vistas.append(tk)
        anteriores.append({"title": r["title"], "data": r["data"]})
        if len(anteriores) >= 4:
            break
    h["anteriores"] = anteriores
    return h


def d_desconhecidos_p(lst):
    """Desconhecidos ordenados por especificidade (mais tokens primeiro) — a
    entidade composta ('Grupo Fictor') vale mais que o token solto ('Fictor')."""
    return sorted(lst, key=lambda x: -len(_entity_tokens(x)))


CIRCULO_NOME = {1: "círculo 1 (íntimo)", 2: "círculo 2", 3: "círculo 3", 4: "círculo 4"}


def dossie_de(cur, hit):
    """Um dossiê por manchete. Cada bloco carrega a PROCEDÊNCIA — a tabela de
    onde saiu — pra que nada aqui possa ser lido como opinião minha."""
    nomes, tickers = extrai_entidades(hit["title"])
    conhecidos, empresas, desconhecidos = [], [], []

    for nome in nomes:
        fichas = casa_pessoa(cur, nome)
        if fichas:
            for f in fichas:
                f["citado_como"] = nome
                f["circulo_label"] = CIRCULO_NOME.get(f.get("circulo"), "sem círculo")
                f["procedencia"] = f"contacts#{f['id']}"
                conhecidos.append(f)
            continue
        # não é pessoa conhecida: pode ser empresa onde ele conhece alguém
        na_empresa = casa_empresa(cur, nome)
        if na_empresa:
            empresas.append({
                "entidade": nome,
                "pessoas": [
                    {**p, "circulo_label": CIRCULO_NOME.get(p.get("circulo"), "sem círculo"),
                     "procedencia": f"contacts#{p['id']}"}
                    for p in na_empresa
                ],
                "procedencia": "contacts.empresa (word-start, token >=5)",
            })
        else:
            desconhecidos.append(nome)

    # "Motiva", "MOTIVA SA", "JCP da Motiva" e "Administração da Motiva" são a
    # mesma entidade escrita de quatro formas. Colapsa por contenção de tokens e
    # mantém a variante mais curta (a mais próxima do nome real).
    empresas.sort(key=lambda x: len(_entity_tokens(x["entidade"])))
    colapsadas = []
    for e in empresas:
        te = set(_entity_tokens(e["entidade"]))
        if any(set(_entity_tokens(k["entidade"])) <= te for k in colapsadas):
            continue
        colapsadas.append(e)
    empresas = colapsadas

    # ── CAMADA 2 — corpo do artigo ──────────────────────────────────────────
    texto, url_final, erro_texto = texto_artigo(hit["url"])
    protagonistas, numeros = [], {}
    if texto:
        numeros = numeros_do_texto(texto)
        for nome, cargo in pessoas_com_cargo(texto).items():
            fichas = casa_pessoa(cur, nome)
            protagonistas.append({
                "nome": nome, "cargo_na_materia": cargo,
                "conhecido": bool(fichas),   # nome casou; confiança é outro campo
                "ficha": ({"id": fichas[0]["id"], "nome": fichas[0]["nome"],
                           "cargo": fichas[0]["cargo"], "empresa": fichas[0]["empresa"],
                           "circulo_label": CIRCULO_NOME.get(fichas[0].get("circulo"), "sem círculo"),
                           "ultimo_contato": fichas[0]["ultimo_contato"],
                           "vinculo": historico_relacional(cur, fichas[0]["id"]),
                           "procedencia": f"contacts#{fichas[0]['id']}"} if fichas else None),
                "procedencia": "corpo do artigo (nome + cargo)",
            })
            if protagonistas[-1]["ficha"]:
                f0 = protagonistas[-1]["ficha"]
                conf, motivo = confianca_do_casamento(f0, f0["vinculo"], cargo)
                f0["confianca"], f0["confianca_motivo"] = conf, motivo
        # conhecido primeiro: é o que muda o que ele faz
        protagonistas.sort(key=lambda x: (not x["conhecido"], x["nome"]))

    # A entidade já passou pelo corpus dele? O termo tem de ser ENTIDADE — a
    # primeira versão usava a `query` do watcher e dava 0 em tudo, porque query é
    # busca ('"Westwing" Brasil', 'EUDR desmatamento regulamento europeu café'),
    # não nome. Agora usa as entidades de fato extraídas, mais específica antes.
    candidatos = [x["entidade"] for x in empresas] + nomes + d_desconhecidos_p(desconhecidos)
    corpus, usados = {}, []
    for termo in candidatos:
        toks = [t for t in _entity_tokens(termo)
                if len(t) >= 5 and t not in GEOGRAFIA and t not in GENERICO
                and t not in NAO_ENTIDADE and t not in _STOP]
        if not toks or toks[0] in usados:
            continue
        # BUSCA POR FRASE, não pelo primeiro token. Com token solto, "PCH Botas"
        # casava a palavra "botas" (calçado) e "Rio Pardo" casava "Santa Cruz do
        # Rio Pardo" — cidade de SP, nada a ver com Ribas do Rio Pardo/MS. Num
        # card que pergunta "a PCH é do grupo?", isso sugeria histórico que não
        # existe: o mesmo modo de falha do homônimo, agora em lugar.
        # E especificidade mínima: termo de 1 token curto não vira consulta.
        frase = re.sub(r"\s+", " ", termo).strip()
        if len(toks) < 2 and len(toks[0]) < 6:
            continue
        usados.append(toks[0])
        c = entidade_no_corpus(cur, frase)
        if c["whatsapp"]["n"] or c["email"]["n"] or c["notas"]["n"]:
            corpus[termo] = c
        if len(usados) >= 4:
            break

    return {
        "title": hit["title"],
        "frente": hit.get("projeto"),
        "url_final": url_final,
        "texto_chars": len(texto) if texto else 0,
        "erro_texto": erro_texto,
        "protagonistas": protagonistas,
        "numeros": numeros,
        "corpus": corpus,
        "tickers": tickers,
        "conhecidos": conhecidos,
        "empresas": empresas,
        "desconhecidos": desconhecidos,
        "historico": historico_watcher(cur, hit["watcher_id"], hit["title"]),
        "procedencia_nomes": "extraído do título (nome próprio), não de LLM",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dias", type=int, default=30)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    alvo = (os.getenv("DB_TARGET") or "").strip().lower()
    if alvo not in ("local", "prod"):
        raise SystemExit("DB_TARGET=local|prod é obrigatório (ver reference_db_target_protocol)")
    if alvo == "prod" and (os.getenv("ALLOW_PROD_FROM_LOCAL") or "").strip() != "1":
        raise SystemExit("prod da máquina exige ALLOW_PROD_FROM_LOCAL=1")
    # Lê o .env quando a env não vem do shell: sob launchd NADA é injetado, e
    # exigir `source .env` faria o cron falhar toda sexta por motivo de ambiente.
    # Mesmo helper do cockpit.py.
    def _do_env(k):
        caminho = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
        try:
            for linha in open(caminho):
                if linha.startswith(k + "="):
                    return linha.split("=", 1)[1].strip().strip('"')
        except OSError:
            pass
        return ""

    if alvo == "prod":
        dsn = os.getenv("DATABASE_URL") or _do_env("DATABASE_URL")
        if not dsn:
            raise SystemExit("DATABASE_URL ausente no ambiente e no .env")
    else:
        dsn = os.getenv("LOCAL_DATABASE_URL", "postgresql://localhost:5432/intel")

    conn = psycopg2.connect(dsn)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """
        SELECT DISTINCT ON (h.title) h.title, h.url, h.watcher_id,
               w.query AS watcher_query, p.id AS project_id, p.nome AS projeto
        FROM project_news_hits h
        JOIN project_news_watchers w ON w.id = h.watcher_id
        LEFT JOIN projects p ON p.id = w.project_id
        WHERE h.hit_at > now() - interval %s
        ORDER BY h.title, h.published_at DESC
        """,
        (f"{args.dias} days",),
    )
    hits = [dict(r) for r in cur.fetchall()]

    dossies = {}
    for h in hits:
        dossies[h["title"]] = dossie_de(cur, h)

    out = args.out or os.path.expanduser("~/cockpit/boletim_dossies.json")
    json.dump(dossies, open(out, "w"), ensure_ascii=False, indent=1)

    n_conh = sum(len(d["conhecidos"]) for d in dossies.values())
    n_emp = sum(len(d["empresas"]) for d in dossies.values())
    n_desc = sum(len(d["desconhecidos"]) for d in dossies.values())
    com_algo = sum(1 for d in dossies.values() if d["conhecidos"] or d["empresas"])
    print(f"{out}")
    print(f"  {len(dossies)} manchetes · {com_algo} com pelo menos um vínculo encontrado")
    print(f"  pessoas que ele conhece: {n_conh} · entidades com gente conhecida dentro: {n_emp}")
    print(f"  nomes citados sem vínculo no INTEL: {n_desc}")
    conn.close()




# ═══════════════════════════════════════════════════════════════════════════
# CAMADA 2 — PROFUNDIDADE (20/09/2026, "está rasa")
#
# A camada 1 lia só o TÍTULO, e título carrega duas entidades enquanto a matéria
# carrega dez. Aqui se lê o texto do artigo e se cruza com o corpus do próprio
# Renato. Segue determinista: extração + consulta, zero LLM.
#
# ⚠️ FRAGILIDADE DECLARADA: o link dos watchers é redirect do Google News e o
# destino real só sai por um endpoint NÃO DOCUMENTADO (`batchexecute`). Funciona
# hoje; pode parar sem aviso. Por isso: cache em disco, e quando falha o dossiê
# DEGRADA pra camada 1 e DIZ que degradou — nunca afina calado.
# ═══════════════════════════════════════════════════════════════════════════
import hashlib
import unicodedata
from concurrent.futures import ThreadPoolExecutor

import httpx

CACHE_DIR = os.path.expanduser("~/cockpit/.cache_artigos")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"}

# "João Silva, diretor de governança" / "o CEO João Silva" — cargo é o que dá
# contexto ao nome, e é exatamente o que o título não traz.
CARGO_PALAVRAS = (
    r"\b(?:CEO|CFO|COO|CTO|presidente|vice-presidente|diretor[ae]?|diretor-geral|"
    r"conselheir[oa]|s[óo]ci[oa]|fundador[ae]?|cofundador[ae]?|chairman|gerente|"
    r"superintendente|coordenador[ae]?|advogad[oa]|economista|analista|"
    r"secret[áa]ri[oa]|ministr[oa]|governador[ae]?|procurador[ae]?|juiz[ae]?|"
    r"desembargador[ae]?|relator[ae]?|perit[oa]|auditor[ae]?)\b"
)
NOME_RE = r"[A-ZÀ-Ý][a-zà-ÿ'’]+(?:\s+(?:d[aeo]s?|e)\s+[A-ZÀ-Ý][a-zà-ÿ'’]+|\s+[A-ZÀ-Ý][a-zà-ÿ'’]+){1,4}"
# A forma mais comum em matéria brasileira é "o atual CEO da companhia, André
# Machado" — cargo, VÍRGULA, nome. A primeira versão proibia vírgula no filler
# (`[^,.;]`) e por isso não casava nada no texto real: 14 mil caracteres de
# matéria da Westwing devolveram zero protagonista. Três formas cobrem o campo:
RE_NOME_CARGO = re.compile(rf"({NOME_RE})\s*,\s*(?:o |a )?({CARGO_PALAVRAS}[^,.;]{{0,60}})", re.U)
RE_CARGO_VIRG_NOME = re.compile(rf"({CARGO_PALAVRAS}[^.;]{{0,50}}?),\s*({NOME_RE})", re.U)
RE_CARGO_NOME = re.compile(rf"({CARGO_PALAVRAS}[^,.;]{{0,30}}?)\s+({NOME_RE})", re.U)
# 4ª forma, achada no artigo da Motiva: o cargo vem ligado por VERBO, não por
# vírgula — "Frederico ... Pascowitch atua como diretor gerente", "vaga deixada
# por Eduardo Bunker Gentil, que ocupava posição de conselheiro independente".
# Enumerar verbo por verbo não acaba; a régua é PROXIMIDADE na mesma frase (o
# `[^.]` não cruza ponto), com o gate de confiança segurando o par errado.
RE_NOME_PERTO_CARGO = re.compile(rf"({NOME_RE})[^.]{{0,45}}?({CARGO_PALAVRAS}[^,.;]{{0,44}})", re.U)
RE_DINHEIRO = re.compile(r"R\$\s?[\d.,]+\s?(?:mil|milh[õo]es|bilh[õo]es|tri)?|"
                         r"(?:US\$|€)\s?[\d.,]+\s?(?:mil|milh[õo]es|bilh[õo]es)?", re.U)
RE_PCT = re.compile(r"\d{1,3}(?:,\d+)?\s?%")


def _cache_path(url):
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, hashlib.sha256(url.encode()).hexdigest()[:24] + ".json")


def resolve_gnews(url):
    """Redirect do Google News → URL real do veículo. Cacheado; None se falhar."""
    if "news.google.com" not in url:
        return url
    m = re.search(r"/articles/([^?]+)", url)
    if not m:
        return None
    gid = m.group(1)
    try:
        r = httpx.get(f"https://news.google.com/rss/articles/{gid}", headers=UA,
                      follow_redirects=True, timeout=20)
        sig = re.search(r'data-n-a-sg="([^"]+)"', r.text)
        ts = re.search(r'data-n-a-ts="([^"]+)"', r.text)
        if not (sig and ts):
            return None
        req = json.dumps(["garturlreq", [["X", "X", ["X", "X"], None, None, 1, 1, "US:en",
                                          None, 1, None, None, None, None, None, 0, 1],
                                         "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
                          gid, int(ts.group(1)), sig.group(1)])
        resp = httpx.post("https://news.google.com/_/DotsSplashUi/data/batchexecute",
                          headers={**UA, "Content-Type":
                                   "application/x-www-form-urlencoded;charset=UTF-8"},
                          data={"f.req": json.dumps([[["Fbv4je", req, None, "1"]]])},
                          timeout=25)
        achou = re.findall(r'https?://(?!news\.google|www\.google)[^\\"]{20,}', resp.text)
        return achou[0] if achou else None
    except Exception:
        return None


def texto_artigo(url):
    """Texto limpo do artigo, cacheado em disco. Devolve (texto, url_final, erro)."""
    cp = _cache_path(url)
    if os.path.exists(cp):
        c = json.load(open(cp))
        return c.get("texto"), c.get("url_final"), c.get("erro")
    real = resolve_gnews(url)
    texto = erro = None
    if not real:
        erro = "redirect do Google News não resolveu"
    else:
        try:
            import trafilatura
        except ImportError:
            # Falha de AMBIENTE, não do artigo — e por isso NÃO se cacheia. O
            # cache é permanente: uma única rodada sem a lib gravaria
            # "ImportError" em 85 arquivos e o dossiê nunca mais tentaria
            # aqueles artigos, mesmo depois de instalar. A camada 2 inteira
            # morreria calada, com o boletim saindo todo degradado pra sempre.
            # `trafilatura` vive no requirements-local.txt (não é dep de prod).
            return None, real, "trafilatura ausente — pip install -r requirements-local.txt"
        try:
            baixado = trafilatura.fetch_url(real)
            texto = trafilatura.extract(baixado) if baixado else None
            if not texto:
                erro = "página não rendeu texto extraível (paywall ou JS)"
        except Exception as ex:
            erro = f"{type(ex).__name__}"
    json.dump({"texto": texto, "url_final": real, "erro": erro},
              open(cp, "w"), ensure_ascii=False)
    return texto, real, erro


def pessoas_com_cargo(texto):
    """Nome + cargo extraídos do CORPO. É o que o título nunca dá."""
    out = {}
    # ordem = especificidade decrescente; `setdefault` mantém o 1º acerto
    for rx, ordem in ((RE_NOME_CARGO, "nc"), (RE_CARGO_VIRG_NOME, "cn"),
                      (RE_CARGO_NOME, "cn"), (RE_NOME_PERTO_CARGO, "nc")):
        for m in rx.finditer(texto):
            nome, cargo = (m.group(1), m.group(2)) if ordem == "nc" else (m.group(2), m.group(1))
            nome = re.sub(r"\s+", " ", nome).strip()
            # Palavra de função capitalizada por abrir a frase, colada no nome:
            # "Já Frederico de Souza Queiroz Pascowitch", "Para Fernando Canutto",
            # "Em Baía Formosa". Tira do início; o nome de verdade fica.
            nome = re.sub(
                r"^(?:Em|J[áa]|Para|Com|Por|Sobre|Entre|Ap[óo]s|Desde|Mas|Ou|Que|"
                r"Quando|Como|Se|Ao|[ÀA]|No|Na|Nos|Nas|Do|Da|Dos|Das|De|E)\s+",
                "", nome).strip()
            pt = _person_tokens(nome)
            if len(pt) < 2 or any(t in INSTITUICAO or t in GEOGRAFIA or t in COLETIVO
                                  or t in _STOP or t in GENERICO for t in pt):
                continue
            cargo = re.sub(r"\s+", " ", cargo).strip(" ,.;")
            out.setdefault(nome, cargo[:70])
    return out


def numeros_do_texto(texto):
    d = list(dict.fromkeys(RE_DINHEIRO.findall(texto)))[:6]
    p = list(dict.fromkeys(RE_PCT.findall(texto)))[:6]
    return {"dinheiro": d, "percentuais": p}


def confianca_do_casamento(ficha, vinculo, cargo_na_materia):
    """Nome igual NÃO é vínculo. O CEO da Westwing na matéria é "André Machado
    Sanson de Oliveira"; o #275 do CRM é "André Machado — Consultor de projetos,
    Ax BP Consulting", zero interação, nunca contatado. Chamar isso de "você
    conhece" repetiria exatamente o erro do Marson: conclusão em cima de
    coincidência ([[feedback_fonte_unica_de_fatos]]).

    FORTE exige EVIDÊNCIA, não semelhança: interação registrada (mensagem,
    tarefa, frente em comum, último contato) ou a empresa da ficha aparecendo no
    cargo descrito na matéria. Sem isso é homônimo provável, e o boletim diz isso.
    """
    tem_interacao = bool(vinculo["mensagens"] or vinculo["tarefas"]
                         or vinculo["frentes"] or ficha.get("ultimo_contato"))
    emp = _norm(ficha.get("empresa") or "")
    cargo_n = _norm(cargo_na_materia or "")
    empresa_casa = bool(emp) and any(
        t in cargo_n for t in _entity_tokens(emp) if len(t) >= 5)
    if empresa_casa or tem_interacao:
        return "forte", ("empresa da ficha aparece no cargo descrito" if empresa_casa
                         else "há interação registrada no INTEL")
    return "homonimo_provavel", ("nome bate, mas a ficha não tem nenhuma interação "
                                 "registrada e a empresa não confere")


def historico_relacional(cur, cid):
    """Profundidade do VÍNCULO, não só a existência dele: últimas trocas, tarefas
    abertas, frentes em comum. É a diferença entre 'você conhece' e 'vocês estão
    no meio de uma conversa'."""
    # public.messages NÃO tem `timestamp` (isso é copilot.emails) — a data é
    # enviado_em/recebido_em/criado_em. E `direcao`: outgoing = Renato enviou.
    cur.execute(
        """SELECT direcao,
                  coalesce(enviado_em, recebido_em, criado_em)::date::text AS data,
                  left(coalesce(conteudo,''),110) AS txt
           FROM public.messages
           WHERE contact_id = %s AND coalesce(conteudo,'') <> ''
           ORDER BY coalesce(enviado_em, recebido_em, criado_em) DESC NULLS LAST
           LIMIT 3""", (cid,))
    msgs = [dict(r) for r in cur.fetchall()]
    # public.tasks e copilot.tasks têm as MESMAS 1.324 linhas mas colunas
    # diferentes (public usa data_vencimento; copilot, due_date). Fixo em
    # public + nome de coluna de lá, senão o search_path decide por sorte.
    cur.execute(
        """SELECT titulo, status, data_vencimento::date::text AS due
           FROM public.tasks
           WHERE contact_id = %s
             AND coalesce(status,'') NOT IN ('completed','done','cancelled','concluida')
           ORDER BY data_vencimento NULLS LAST LIMIT 3""", (cid,))
    tarefas = [dict(r) for r in cur.fetchall()]
    cur.execute(
        """SELECT p.nome, coalesce(pm.papel,'') AS papel FROM public.project_members pm
           JOIN public.projects p ON p.id = pm.project_id
           WHERE pm.contact_id = %s LIMIT 4""", (cid,))
    frentes = [dict(r) for r in cur.fetchall()]
    return {"mensagens": msgs, "tarefas": tarefas, "frentes": frentes}


_CORPUS_MEMO = {}


def entidade_no_corpus(cur, termo):  # noqa: D401
    # `termo` é FRASE (ex "PCH Botas"), não token — ver o comentário no chamador.
    #
    # MEMOIZADO: "Fictor" aparece em 18 das 85 manchetes e cada repetição refazia
    # 4 consultas com `ILIKE '%...%'` — que é varredura completa, sem índice que
    # sirva. A entidade não muda no meio da rodada, então o resultado é o mesmo.
    if termo in _CORPUS_MEMO:
        return _CORPUS_MEMO[termo]
    """A entidade já passou pelo SEU mundo? WhatsApp, e-mail e notas de projeto.
    Responde "você já falou disso, e quando" — o sinal que mais muda a leitura de
    uma manchete, e que nenhuma camada anterior tinha."""
    like = f"%{termo}%"
    out = {}
    cur.execute("""SELECT count(*) AS n,
                          max(coalesce(enviado_em, recebido_em, criado_em))::date::text AS ultimo
                   FROM public.messages WHERE conteudo ILIKE %s""", (like,))
    out["whatsapp"] = dict(cur.fetchone())
    # Os e-mails vivem em copilot.emails (outro schema) e cobrem as DUAS contas —
    # a pessoal inclusive, que o MCP do Gmail não alcança
    # ([[reference_intel_indexa_email_pessoal]]). Sem o schema explícito, some.
    cur.execute("""SELECT count(*) AS n, max("timestamp")::date::text AS ultimo
                   FROM copilot.emails
                   WHERE subject ILIKE %s OR content ILIKE %s""", (like, like))
    out["email"] = dict(cur.fetchone())
    cur.execute("""SELECT count(*) AS n, max(criado_em)::date::text AS ultimo
                   FROM public.project_notes WHERE conteudo ILIKE %s""", (like,))
    out["notas"] = dict(cur.fetchone())

    # TRECHO, não só contagem. "há 1 nota mencionando Ribas do Rio Pardo" é uma
    # curiosidade; o trecho é que resolve — nesse caso ele diz que o presidente
    # do conselho do grupo teve embargo do IBAMA sobre 2.228 ha NAQUELE
    # município, o que transforma "a PCH é do grupo?" de chute em pergunta com
    # endereço. Contagem sem trecho é o mesmo erro do boletim v1: número que não
    # diz o que fazer.
    cur.execute("""SELECT pn.conteudo, p.nome AS projeto, pn.criado_em::date::text AS data
                   FROM public.project_notes pn
                   LEFT JOIN public.projects p ON p.id = pn.project_id
                   WHERE pn.conteudo ILIKE %s
                   ORDER BY pn.criado_em DESC LIMIT 1""", (like,))
    r = cur.fetchone()
    if r:
        txt = re.sub(r"\s+", " ", r["conteudo"] or "")
        i = txt.lower().find(termo.lower())
        ini, fim = max(0, i - 150), min(len(txt), i + len(termo) + 260)
        out["notas"]["trecho"] = ("…" if ini else "") + txt[ini:fim] + ("…" if fim < len(txt) else "")
        out["notas"]["projeto"] = r["projeto"]
        out["notas"]["data"] = r["data"]
    _CORPUS_MEMO[termo] = out
    return out


# O entrypoint fica no FIM do arquivo de propósito: a camada 2 é definida
# depois de `dossie_de`, e com o `__main__` no meio o main() rodava antes de
# `texto_artigo` existir (NameError em runtime, não em import).
if __name__ == "__main__":
    main()
