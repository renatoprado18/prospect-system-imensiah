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

Consequência aceita: o ConselhoOS é READ-ONLY por aqui. Editar item de
conselho continua sendo no ConselhoOS (ou pelo fluxo de propostas do
`raci_weekly_report`). O INTEL escreve só no que é dele.

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
        "editavel": fonte == FONTE_INTEL,
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


# ==================== escrita (só o lado INTEL) ====================

_CAMPOS = ("area", "acao", "responsavel_r", "responsavel_a", "responsavel_c",
           "responsavel_i", "prazo", "status", "notas", "task_id")


def create_item(project_id: int, data: Dict) -> Dict:
    """Cria um item de RACI no lado INTEL. `acao` é o único obrigatório."""
    acao = (data.get("acao") or "").strip()
    if not acao:
        return {"error": "acao é obrigatória"}

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


def update_item(item_id: int, data: Dict) -> Dict:
    """
    Atualiza um item do lado INTEL. Só mexe nos campos que vieram — um PATCH
    que zerasse o que não foi enviado apagaria responsável por omissão.
    """
    campos = {k: data[k] for k in _CAMPOS if k in data}
    if not campos:
        return {"error": "nada a atualizar"}

    sets, valores = [], []
    for k, v in campos.items():
        sets.append(f"{k} = %s")
        if isinstance(v, str):
            v = v.strip() or None
        valores.append(v)
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
    return {"ok": True, "id": item_id}


def delete_item(item_id: int) -> Dict:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM raci_itens WHERE id = %s", (item_id,))
        afetados = cursor.rowcount
        conn.commit()
    if not afetados:
        return {"error": "item não encontrado", "id": item_id}
    return {"ok": True, "id": item_id}
