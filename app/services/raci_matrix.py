"""
RACI genérico — a matriz de um projeto, venha ela de onde vier.

O QUE ISTO RESOLVE (pedido do Renato 28/07/26, task #999703)
------------------------------------------------------------
Ver o RACI de QUALQUER projeto no INTEL e imprimir em PDF on-brand num
clique, pra compartilhar com quem não tem acesso ao sistema.

Antes disto, "RACI" era duas coisas incompatíveis:

  - no ConselhoOS (outro Neon), `raci_itens` estruturado — mas só existe pra
    empresa de conselho. A reorg das 7 empresas (#47) não é conselho, e é
    justamente onde o Renato precisa mandar a matriz pro Piccino e pra
    Priscila;
  - no INTEL, RACI como TEXTO dentro de uma `project_note` (a #268 do #47) —
    legível por humano, inútil pra máquina: não ordena por prazo, não filtra
    por status, não imprime.

A ESCOLHA DE ARQUITETURA: UNIR NA LEITURA, NÃO SINCRONIZAR
----------------------------------------------------------
São dois Neons sem sync ([[feedback_intel_conselhoos_sync_lacuna]]). A
tentação óbvia — copiar o RACI do ConselhoOS pra dentro do INTEL — criaria a
terceira cópia do mesmo fato, e cópia que não se reconcilia é exatamente a
classe de defeito que já mordeu aqui (a nota #302 que sobrevive contradizendo
o memo certo). Então cada fonte é lida NA FONTE, em cada requisição, e a
união só existe em memória.

ESCRITA: NA FONTE TAMBÉM (write-through, 29/07)
------------------------------------------------
A primeira versão era read-only em relação ao ConselhoOS. Durou um dia: o
caso real que motivou a tela — atualizar o RACI da Vallen depois da reunião —
esbarrava em 57 itens dos quais ZERO eram editáveis, porque todos vêm do
ConselhoOS. Uma matriz que mostra tudo e deixa mexer em nada não serve pro
momento em que ela é usada.

Renato decidiu abrir a escrita (29/07). O que NÃO muda é a regra de cópia:
editar aqui grava NA FONTE — item de conselho é `UPDATE` no ConselhoOS, item
do INTEL é `UPDATE` no INTEL. Nada é espelhado, então não nasce a terceira
cópia. Precedente já existia: `conselhoos_raci_sync` escreve lá desde sempre
quando a task INTEL fecha.

O que a escrita cross-DB exige de cuidado (o schema dos dois lados NÃO é o
mesmo — ver `_update_conselhoos`):
  - `area`, `acao`, `prazo` e `status` são NOT NULL no ConselhoOS; no INTEL
    `prazo` é opcional. Apagar prazo de item de conselho é rejeitado com
    mensagem, não com erro 500 do banco;
  - `status` é ENUM (`raci_status`) lá e CHECK aqui — valor inválido é
    barrado antes do INSERT, dos dois lados;
  - `concluido_relatado_em` NÃO é tocado ao concluir. É o campo que o
    `raci_weekly_report` usa pra saber o que ainda não foi anunciado no
    grupo; preenchê-lo aqui faria a conclusão nascer "já relatada" e sumir
    do relatório sem nunca ter sido dita.

DELETE segue INTEL-only. Apagar linha de RACI de conselho é destruir registro
de ata de uma empresa que não é minha, por um caminho que não é o dela — e
ninguém pediu isso.

COMO UM PROJETO ACHA O RACI DE CONSELHO DELE
---------------------------------------------
`projects.empresa_id` -> `empresas.conselhoos_empresa_id` -> empresa do
ConselhoOS (migration 056). O elo projeto->empresa não existia; `projects`
só tinha `empresa_relacionada`, TEXT livre preenchido em 5 de 28 projetos
ativos e com grafias que não casam com `empresas.nome_canonico`.

Projeto sem `empresa_id` (a maioria) simplesmente não tem fonte-conselho: a
matriz é só a do INTEL, e isso não é erro nem estado degradado.
"""
import logging
import os
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from database import get_db

logger = logging.getLogger(__name__)


# Os 4 status são os mesmos dos dois lados (enum `raci_status` no ConselhoOS,
# CHECK na tabela do INTEL). A ordem aqui é a de exibição.
STATUS_ORDER = ["atrasado", "pendente", "em_andamento", "concluido"]

STATUS_LABEL = {
    "atrasado": "Atrasado",
    "pendente": "Pendente",
    "em_andamento": "Em andamento",
    "concluido": "Concluído",
}

FONTE_INTEL = "intel"
FONTE_CONSELHOOS = "conselhoos"


def _conselhoos_url() -> str:
    """URL do ConselhoOS lida em CALL-TIME + strip().

    Mesmo motivo de `raci_smart_updates._conselhoos_url`: a Vercel cola '\\n'
    no valor ([[feedback_env_var_whitespace]]) e uma constante de módulo lida
    no import fica vazia pra quem seta a env depois. Ler na constante já
    custou um diagnóstico falso ("item não encontrado no RACI" quando quem
    faltava era a conexão).
    """
    return (os.getenv("CONSELHOOS_DATABASE_URL") or "").strip()


def _as_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _normalize(row: Dict, fonte: str) -> Dict:
    """
    Uma linha de qualquer fonte no MESMO formato.

    `status_efetivo` é derivado, não lido: um item pendente cujo prazo passou
    é `atrasado` para quem lê, mesmo que a coluna diga `pendente`. Derivar na
    leitura (em vez de um cron que reescreve status) evita que a matriz
    dependa de um job ter rodado — e o board já registra o custo de status
    que envelhece sem ninguém mexer.

    Item SEM prazo nunca vira atrasado. É o caso das responsabilidades
    permanentes ("coordenação da cadência"), que não vencem; marcá-las de
    vermelho seria falso-atrasado, o que corroeu a credibilidade do RACI do
    Vallen em 13/07.
    """
    prazo = _as_date(row.get("prazo"))
    status = (row.get("status") or "pendente").strip()

    status_efetivo = status
    if status != "concluido" and prazo and prazo < date.today():
        status_efetivo = "atrasado"

    dias = (prazo - date.today()).days if prazo else None

    return {
        "fonte": fonte,
        "id": row.get("id"),
        "uid": f"{fonte}:{row.get('id')}",
        "area": (row.get("area") or "").strip() or None,
        "acao": (row.get("acao") or "").strip(),
        "r": (row.get("responsavel_r") or "").strip() or None,
        "a": (row.get("responsavel_a") or "").strip() or None,
        "c": (row.get("responsavel_c") or "").strip() or None,
        "i": (row.get("responsavel_i") or "").strip() or None,
        "prazo": prazo.isoformat() if prazo else None,
        "prazo_br": prazo.strftime("%d/%m/%Y") if prazo else None,
        "dias_para_prazo": dias,
        "status": status,
        "status_efetivo": status_efetivo,
        "status_label": STATUS_LABEL.get(status_efetivo, status_efetivo),
        "notas": (row.get("notas") or "").strip() or None,
        "task_id": row.get("task_id"),
        # Editar vale nas duas fontes (write-through, 29/07). Remover, não:
        # `removivel` é o que separa mexer numa linha de destruí-la.
        "editavel": True,
        "removivel": fonte == FONTE_INTEL,
        # O ConselhoOS não aceita item sem prazo (coluna NOT NULL). A tela usa
        # isto pra avisar ANTES, em vez de deixar o usuário limpar o campo e
        # descobrir no erro que aquilo nunca foi possível.
        "prazo_obrigatorio": fonte == FONTE_CONSELHOOS,
    }


def _fetch_intel(cursor, project_id: int) -> List[Dict]:
    cursor.execute("""
        SELECT id, area, acao, responsavel_r, responsavel_a, responsavel_c,
               responsavel_i, prazo, status, notas, task_id
          FROM raci_itens
         WHERE project_id = %s
    """, (project_id,))
    return [_normalize(dict(r), FONTE_INTEL) for r in cursor.fetchall()]


def _fetch_conselhoos(empresa_uuid: str) -> List[Dict]:
    """
    Itens do ConselhoOS pra uma empresa. READ-ONLY.

    Falha graciosa de propósito: sem a env, com o outro Neon fora do ar ou
    com a tabela ausente, devolve [] e a matriz mostra só o lado INTEL. A
    página não pode cair por causa de um banco que nem é o dela — mas quem
    chama recebe o aviso por `_fetch_conselhoos_status` pra poder dizer na
    tela que a fonte está incompleta, em vez de mentir um RACI menor.
    """
    itens, _ = _fetch_conselhoos_status(empresa_uuid)
    return itens


def _fetch_conselhoos_status(empresa_uuid: str):
    """Devolve (itens, erro_ou_None)."""
    url = _conselhoos_url()
    if not url:
        return [], "CONSELHOOS_DATABASE_URL não configurada"

    try:
        import psycopg2
        import psycopg2.extras

        conn = psycopg2.connect(url, connect_timeout=5)
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("""
                SELECT id, area, acao, responsavel_r, responsavel_a,
                       responsavel_c, responsavel_i, prazo, status, notas
                  FROM raci_itens
                 WHERE empresa_id = %s
            """, (empresa_uuid,))
            rows = cur.fetchall()
        finally:
            conn.close()
        return [_normalize(dict(r), FONTE_CONSELHOOS) for r in rows], None
    except Exception as e:
        logger.warning(f"RACI ConselhoOS indisponível para {empresa_uuid}: {e}")
        return [], str(e)


def _sort_key(item: Dict):
    """
    Ordem de leitura: o que está atrasado primeiro, concluído por último; e
    dentro do bucket, por prazo mais próximo. Item sem prazo vai pro fim do
    seu bucket — não some, mas também não disputa o topo com quem tem data.
    """
    bucket = STATUS_ORDER.index(item["status_efetivo"]) if item["status_efetivo"] in STATUS_ORDER else 9
    sem_prazo = item["prazo"] is None
    return (bucket, sem_prazo, item["prazo"] or "", item["acao"].lower())


def get_matrix(project_id: int, status: Optional[str] = None) -> Dict:
    """
    A matriz RACI de um projeto, unindo as fontes disponíveis.

    `status`: filtro opcional sobre `status_efetivo` ('atrasado', 'pendente',
    'em_andamento', 'concluido'). O resumo é sempre do conjunto COMPLETO — um
    filtro que também encolhesse o resumo esconderia justamente o que se quer
    ver ("quantos atrasados existem" enquanto se olha os concluídos).
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT p.id, p.nome, p.tipo, p.status, p.empresa_id,
                   e.nome_canonico AS empresa_nome,
                   e.conselhoos_empresa_id
              FROM projects p
              LEFT JOIN empresas e ON e.id = p.empresa_id
             WHERE p.id = %s
        """, (project_id,))
        projeto = cursor.fetchone()
        if not projeto:
            return {"error": "projeto não encontrado", "project_id": project_id}
        projeto = dict(projeto)

        itens = _fetch_intel(cursor, project_id)

    fontes = [{"fonte": FONTE_INTEL, "itens": len(itens), "erro": None}]

    empresa_uuid = projeto.get("conselhoos_empresa_id")
    if empresa_uuid:
        conselho_itens, erro = _fetch_conselhoos_status(str(empresa_uuid))
        itens.extend(conselho_itens)
        fontes.append({
            "fonte": FONTE_CONSELHOOS,
            "empresa": projeto.get("empresa_nome"),
            "itens": len(conselho_itens),
            "erro": erro,
        })

    resumo = {s: 0 for s in STATUS_ORDER}
    for it in itens:
        resumo[it["status_efetivo"]] = resumo.get(it["status_efetivo"], 0) + 1

    if status:
        itens = [it for it in itens if it["status_efetivo"] == status]

    itens.sort(key=_sort_key)

    return {
        "project": {
            "id": projeto["id"],
            "nome": projeto["nome"],
            "tipo": projeto["tipo"],
            "status": projeto["status"],
            "empresa": projeto.get("empresa_nome"),
        },
        "itens": itens,
        "total": len(itens),
        "resumo": resumo,
        "fontes": fontes,
        "filtro_status": status,
        "gerado_em": datetime.now().strftime("%d/%m/%Y %H:%M"),
    }


# ==================== envio pro grupo do projeto ====================

_EMOJI_STATUS = {"atrasado": "🚨", "em_andamento": "🔄",
                 "pendente": "⏳", "concluido": "✅"}

_TITULO_BUCKET = {
    "atrasado": "Atrasados",
    "em_andamento": "Em andamento",
    "pendente": "Pendentes",
}


def _primeiro_nome(nome: Optional[str]) -> str:
    """'Jéssica (cobrindo Veridiana)' -> 'Jéssica'. No grupo todo mundo sabe
    quem é quem; o parêntese come a linha e empurra o prazo pra outra."""
    if not nome:
        return "—"
    limpo = nome.split("(")[0].strip()
    return limpo.split("/")[0].strip() or nome.strip()


def _cortar(texto: str, n: int = 95) -> str:
    texto = (texto or "").strip().replace("\n", " ")
    return texto if len(texto) <= n else texto[: n - 1].rstrip() + "…"


def format_for_whatsapp(matrix: Dict, incluir_concluidos: bool = False) -> str:
    """
    A matriz como texto de WhatsApp, pra mandar no grupo do projeto.

    SEM NUMERAÇÃO, e isso é decisão, não esquecimento. O `parse_raci_update`
    escuta os grupos e interpreta "3 concluído" pela ordem do
    `generate_raci_report` — que NÃO é esta ordem (a daqui inclui itens do
    INTEL que aquele report não enxerga, e ordena por `status_efetivo`
    derivado). Mandar numerado convidaria uma resposta que acertaria o item
    errado — exatamente o desalinhamento que a trava de 29/07 existe pra
    conter. Bullet não convida número.

    Concluídos entram só como contagem por padrão: quem lê o RACI no grupo
    quer saber o que falta. O texto é editável antes de sair, então listar é
    escolha de quem envia, não default de quem gera.
    """
    p = matrix.get("project") or {}
    itens = matrix.get("itens") or []
    resumo = matrix.get("resumo") or {}

    linhas = [f"📋 *RACI — {p.get('nome') or 'projeto'}*",
              f"_{date.today().strftime('%d/%m/%Y')}_", ""]

    for bucket in ("atrasado", "em_andamento", "pendente"):
        do_bucket = [i for i in itens if i["status_efetivo"] == bucket]
        if not do_bucket:
            continue
        linhas.append(f"{_EMOJI_STATUS[bucket]} *{_TITULO_BUCKET[bucket]} "
                      f"({len(do_bucket)}):*")
        for it in do_bucket:
            prazo = f" ({it['prazo_br']})" if it.get("prazo_br") else ""
            linhas.append(f"• {_cortar(it['acao'])} — *{_primeiro_nome(it.get('r'))}*{prazo}")
        linhas.append("")

    concluidos = [i for i in itens if i["status_efetivo"] == "concluido"]
    if concluidos:
        if incluir_concluidos:
            linhas.append(f"✅ *Concluídos ({len(concluidos)}):*")
            for it in concluidos:
                linhas.append(f"• {_cortar(it['acao'])}")
            linhas.append("")
        else:
            linhas.append(f"✅ *{len(concluidos)} concluído"
                          f"{'s' if len(concluidos) > 1 else ''}* desde o início.")
            linhas.append("")

    if not itens:
        linhas.append("_Nenhum item na matriz._")

    total = sum(resumo.values()) if resumo else len(itens)
    linhas.append(f"_{total} itens no total._")
    return "\n".join(linhas).strip()


# Teto do corpo de texto da Evolution/WhatsApp. Um RACI de 60+ itens passa
# disso com folga — e a Evolution corta em silêncio, então metade do RACI
# chegaria no grupo sem ninguém perceber que faltou.
WHATSAPP_MAX_CHARS = 4096


# ==================== escrita (write-through, cada fonte na sua) ==========

_CAMPOS = ("area", "acao", "responsavel_r", "responsavel_a", "responsavel_c",
           "responsavel_i", "prazo", "status", "notas", "task_id")

# `task_id` fica de fora: do lado de lá a coluna é `intel_task_id` e quem a
# governa é o `conselhoos_raci_sync`. Deixar a tela escrever nela seria criar
# um segundo dono pro mesmo elo.
_CAMPOS_CONSELHOOS = ("area", "acao", "responsavel_r", "responsavel_a",
                      "responsavel_c", "responsavel_i", "prazo", "status",
                      "notas")

# NOT NULL no ConselhoOS (`\d raci_itens` do outro Neon, conferido 29/07).
_OBRIGATORIOS_CONSELHOOS = ("area", "acao", "prazo", "status")


def _split_uid(uid: Any):
    """
    `'intel:12'` / `'conselhoos:<uuid>'` -> `(fonte, ident)`.

    Id nu (`12`) é aceito como INTEL: era o formato da primeira versão da
    tela e continua chegando de link salvo ou aba aberta. `(None, None)`
    quando não dá pra dizer com certeza de qual fonte é — adivinhar aqui
    escreveria no banco errado.
    """
    texto = str(uid).strip()
    if ":" in texto:
        fonte, _, ident = texto.partition(":")
        fonte, ident = fonte.strip().lower(), ident.strip()
    else:
        fonte, ident = FONTE_INTEL, texto

    if fonte not in (FONTE_INTEL, FONTE_CONSELHOOS) or not ident:
        return None, None
    if fonte == FONTE_INTEL and not ident.lstrip("-").isdigit():
        return None, None
    return fonte, ident


def _limpar(valor: Any) -> Any:
    return (valor.strip() or None) if isinstance(valor, str) else valor


def _validar_status(campos: Dict) -> Optional[str]:
    """`status` é ENUM lá e CHECK aqui: os dois estouram feio com valor
    inválido. Barrar antes devolve 400 legível em vez de 500."""
    if "status" in campos:
        valor = _limpar(campos["status"])
        if valor not in STATUS_ORDER:
            return f"status inválido: {campos['status']!r}"
    return None


def create_item(project_id: int, data: Dict) -> Dict:
    """Cria um item de RACI no lado INTEL. `acao` é o único obrigatório."""
    acao = (data.get("acao") or "").strip()
    if not acao:
        return {"error": "acao é obrigatória"}
    erro = _validar_status({"status": data.get("status") or "pendente"})
    if erro:
        return {"error": erro}

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO raci_itens
                (project_id, area, acao, responsavel_r, responsavel_a,
                 responsavel_c, responsavel_i, prazo, status, notas, origem, task_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            project_id,
            (data.get("area") or "").strip() or None,
            acao,
            (data.get("responsavel_r") or "").strip() or None,
            (data.get("responsavel_a") or "").strip() or None,
            (data.get("responsavel_c") or "").strip() or None,
            (data.get("responsavel_i") or "").strip() or None,
            data.get("prazo") or None,
            (data.get("status") or "pendente").strip(),
            (data.get("notas") or "").strip() or None,
            (data.get("origem") or "manual").strip(),
            data.get("task_id"),
        ))
        item_id = cursor.fetchone()["id"]
        conn.commit()
    return {"ok": True, "id": item_id}


def update_item(item_uid: Any, data: Dict) -> Dict:
    """
    Atualiza um item na FONTE dele. `item_uid` é `'intel:12'` ou
    `'conselhoos:<uuid>'` (id nu = INTEL, retrocompat).

    Só mexe nos campos que vieram — um PATCH que zerasse o que não foi
    enviado apagaria responsável por omissão.
    """
    fonte, ident = _split_uid(item_uid)
    if not fonte:
        return {"error": f"identificador de item inválido: {item_uid!r}"}
    if fonte == FONTE_CONSELHOOS:
        return _update_conselhoos(ident, data)
    return _update_intel(int(ident), data)


def _update_intel(item_id: int, data: Dict) -> Dict:
    campos = {k: data[k] for k in _CAMPOS if k in data}
    if not campos:
        return {"error": "nada a atualizar"}
    erro = _validar_status(campos)
    if erro:
        return {"error": erro}

    sets, valores = [], []
    for k, v in campos.items():
        sets.append(f"{k} = %s")
        valores.append(_limpar(v))
    sets.append("atualizado_em = CURRENT_TIMESTAMP")
    valores.append(item_id)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE raci_itens SET {', '.join(sets)} WHERE id = %s", valores)
        afetados = cursor.rowcount
        conn.commit()
    if not afetados:
        return {"error": "item não encontrado", "id": item_id}
    return {"ok": True, "uid": f"{FONTE_INTEL}:{item_id}", "fonte": FONTE_INTEL}


def _update_conselhoos(item_uuid: str, data: Dict) -> Dict:
    """
    `UPDATE` no banco do ConselhoOS. Escreve na fonte, não espelha.

    As duas diferenças de schema que precisam morrer aqui, e não no banco:
    os NOT NULL (mandar `None` viraria `IntegrityError` genérico, e o Renato
    leria "erro ao salvar" sem saber que aquele campo nunca foi opcional) e o
    ENUM de status. `concluido_relatado_em` fica intocado de propósito — é do
    `raci_weekly_report`, ver cabeçalho do módulo.
    """
    campos = {k: data[k] for k in _CAMPOS_CONSELHOOS if k in data}
    if not campos:
        return {"error": "nada a atualizar"}
    erro = _validar_status(campos)
    if erro:
        return {"error": erro}

    for k in _OBRIGATORIOS_CONSELHOOS:
        if k in campos and _limpar(campos[k]) in (None, ""):
            rotulo = "prazo" if k == "prazo" else k
            return {"error": f"o ConselhoOS não aceita item sem {rotulo} — "
                             f"preencha ou edite lá"}

    url = _conselhoos_url()
    if not url:
        return {"error": "CONSELHOOS_DATABASE_URL não configurada"}

    sets, valores = [], []
    for k, v in campos.items():
        # O cast é obrigatório: psycopg2 manda `status` como texto e o Postgres
        # não converte pro enum sozinho num UPDATE parametrizado.
        sets.append(f"{k} = %s::raci_status" if k == "status" else f"{k} = %s")
        valores.append(_limpar(v))
    sets.append("updated_at = NOW()")
    valores.append(item_uuid)

    try:
        import psycopg2
        import psycopg2.extras

        conn = psycopg2.connect(url, connect_timeout=5)
        try:
            cur = conn.cursor()
            cur.execute(
                f"UPDATE raci_itens SET {', '.join(sets)} WHERE id = %s::uuid",
                valores)
            afetados = cur.rowcount
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"RACI ConselhoOS: falha ao gravar {item_uuid}: {e}")
        return {"error": f"não consegui gravar no ConselhoOS: {e}"}

    if not afetados:
        return {"error": "item não encontrado", "id": item_uuid}
    return {"ok": True, "uid": f"{FONTE_CONSELHOOS}:{item_uuid}",
            "fonte": FONTE_CONSELHOOS}


def delete_item(item_uid: Any) -> Dict:
    """
    Remove item — só do lado INTEL. Ver cabeçalho: apagar linha de RACI de
    conselho é destruir registro de ata de uma empresa, por um caminho que não
    é o dela.
    """
    fonte, ident = _split_uid(item_uid)
    if not fonte:
        return {"error": f"identificador de item inválido: {item_uid!r}"}
    if fonte == FONTE_CONSELHOOS:
        return {"error": "item de conselho não se remove pelo INTEL — "
                         "apague no ConselhoOS"}

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM raci_itens WHERE id = %s", (int(ident),))
        afetados = cursor.rowcount
        conn.commit()
    if not afetados:
        return {"error": "item não encontrado", "id": ident}
    return {"ok": True, "uid": f"{FONTE_INTEL}:{ident}", "fonte": FONTE_INTEL}
