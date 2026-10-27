"""Acesso ao RACI por um lugar só — e o kill-switch que escolhe qual lugar.

POR QUE ESTE MÓDULO EXISTE (27/10/2026)
---------------------------------------
A consolidação "RACI fonte única" de 06/10 virou a LEITURA da matriz para
só-INTEL (`raci_matrix.get_matrix`) e deixou TODA a escrita automática gravando
no ConselhoOS. O resultado, medido em 26/10 na Vallen: `max(atualizado_em)` do
INTEL parado em 13/10 enquanto o ConselhoOS tinha 29 itens movidos entre 14 e
23/10 — a reunião de conselho inteira. O grupo do cliente exibia `✅ Atualizado`
em 15, 19, 21 e 22/10 e nada daquilo chegava à matriz que o Renato publica.
Ninguém viu por 13 dias, porque não havia nada para ver: as duas escritas
funcionavam perfeitamente, só não no banco que alguém lê.

O defeito não foi distração — foi ARQUITETURA. A escolha do banco estava
espalhada por seis pontos em quatro arquivos, cada um abrindo sua própria
conexão com `_conselhoos_url()`. Mudar "onde o RACI mora" exigia achar os seis;
achar cinco deixava o sistema num estado pior que o inicial. Este módulo é o
conserto estrutural: a escolha passa a existir em UM lugar, e os chamadores
pedem operações ("liste os abertos", "atualize este item") em vez de conexões.

O KILL-SWITCH, e por que ele não é um shadow
--------------------------------------------
`RACI_WRITE_TARGET` aceita `intel` (default) ou `conselhoos` (o comportamento
antigo). Default no destino CERTO de propósito: flag que nasce desligada é
consumidor morto esperando alguém lembrar dela ([[feedback_consumidor_morto_wiring]]),
e o custo desta já foi pago. O valor do switch é a REVERSÃO — se a semana da
decisão da Vallen (conselho 29/10, gate da Aptus 30/10) der problema, voltar é
mudar uma variável no Railway, não reverter commit e esperar deploy.

A ASSIMETRIA QUE SE MANTÉM
--------------------------
O ConselhoOS continua sendo a casa das ATAS e das REUNIÕES — é governança de
cliente e o histórico vive lá. Este módulo nunca apaga nem reescreve o
`raci_itens` de lá; com target `intel` ele simplesmente não escreve lá. Quem
precisa de ata/reunião (`raci_ata_reconciler.load_reuniao`) continua indo ao
ConselhoOS direto, e isso é correto.

A TRADUÇÃO DE REFERÊNCIA, que é o que não quebra a fila
-------------------------------------------------------
Os ids divergem: ConselhoOS usa `uuid`, INTEL usa `integer`. As 92 propostas em
`raci_group_proposals` com `status='pending_review'` (39 de outubro, a mais nova
de 27/10) carregam UUID — e medido em 27/10, **todas as 92** têm par no INTEL
via `conselhoos_raci_id`. Então `resolve_item_ref` traduz na hora da aplicação,
nos dois sentidos, e nenhuma linha da fila precisou ser reescrita em lote.
Proposta nova já nasce com o id nativo do target.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from database import get_db

log = logging.getLogger(__name__)

TARGET_INTEL = "intel"
TARGET_CONSELHOOS = "conselhoos"

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)
_INT_RE = re.compile(r"^[0-9]+$")

# `cancelado` não existe no CHECK de `status` em NENHUM dos dois lados (medido
# em 27/10: ambos aceitam pendente | em_andamento | concluido | atrasado). Quem
# pedir cancelamento recebe o motivo, não um UPDATE que o banco recusa.
STATUS_VALIDOS = ("pendente", "em_andamento", "concluido", "atrasado")


class RaciStoreError(Exception):
    """Falha de acesso ao RACI."""


class RaciScopeError(RaciStoreError):
    """Não foi possível resolver o escopo (projeto/empresa) para o target ativo.

    NÃO é o mesmo que "não há itens": é "não sei em que banco nem sob que chave
    procurar". Devolver lista vazia aqui seria a abstenção que vira fábrica de
    diagnóstico falso ([[feedback_guarda_abstencao_vira_fabrica]]) — este
    caminho já gastou um "item não encontrado no RACI" que era, na verdade,
    conexão ausente.
    """


def write_target() -> str:
    """O destino das escritas de RACI. `intel` (default) ou `conselhoos`.

    Lido em CALL-TIME e com `.strip()` — convenção do repo: Railway/Vercel
    colam '\\n' no valor ([[feedback_env_var_whitespace]]) e constante de módulo
    lida no import fica velha para quem seta a env depois.

    Valor desconhecido cai no default e GRITA no log: silenciar um typo
    (`RACI_WRITE_TARGET=Intel `) mandaria a escrita para o lugar que este
    módulo existe para consertar.
    """
    raw = (os.getenv("RACI_WRITE_TARGET") or "").strip().lower()
    if not raw:
        return TARGET_INTEL
    if raw in (TARGET_INTEL, TARGET_CONSELHOOS):
        return raw
    log.warning(
        "RACI_WRITE_TARGET=%r desconhecido — usando %r. Valores aceitos: %r | %r",
        raw, TARGET_INTEL, TARGET_INTEL, TARGET_CONSELHOOS,
    )
    return TARGET_INTEL


def conselhoos_url() -> str:
    """URL do ConselhoOS, call-time + strip. Mesmo motivo de `write_target`."""
    return (os.getenv("CONSELHOOS_DATABASE_URL") or "").strip()


def _cos_conn():
    url = conselhoos_url()
    if not url:
        raise RaciScopeError("CONSELHOOS_DATABASE_URL ausente ou vazia")
    import psycopg2
    import psycopg2.extras
    return psycopg2.connect(url)


# ─────────────────────────────────────────────────────────────────────────────
# Escopo
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class RaciScope:
    """Onde o RACI desta frente mora, resolvido para o target ativo.

    Carrega os DOIS lados de propósito, mesmo que só um seja usado para
    escrever: `empresa_uuid` continua sendo a chave de dedup de notificação e de
    `scope_ref` no livro-razão, e perdê-la na troca de target faria o aviso
    duplicar justamente na semana em que o sistema está sob observação.
    """

    target: str
    project_id: Optional[int]
    empresa_uuid: Optional[str]
    nome: str

    @property
    def chave(self) -> str:
        """A chave estável para dedup/audit — independe do target.

        Prefere o UUID da empresa quando existe, porque é ele que está gravado
        nas 92 propostas e nas dedup-keys já emitidas; trocar a chave junto com
        o target reemitiria aviso que o Renato já viu.
        """
        return str(self.empresa_uuid or f"project:{self.project_id}")

    def exige_project(self) -> int:
        if self.project_id is None:
            raise RaciScopeError(
                f"target={self.target} exige project_id e '{self.nome}' não tem "
                "vínculo de projeto no INTEL"
            )
        return self.project_id

    def exige_empresa(self) -> str:
        if not self.empresa_uuid:
            raise RaciScopeError(
                f"target={self.target} exige empresa_id do ConselhoOS e "
                f"'{self.nome}' não tem vínculo (empresas.conselhoos_empresa_id)"
            )
        return str(self.empresa_uuid)


def _scope_de_projeto(cur, project_id: int) -> Optional[RaciScope]:
    cur.execute(
        """SELECT p.id, p.nome, e.conselhoos_empresa_id
             FROM projects p
             LEFT JOIN empresas e ON e.id = p.empresa_id
            WHERE p.id = %s""",
        (project_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    row = dict(row)
    return RaciScope(
        target=write_target(),
        project_id=int(row["id"]),
        empresa_uuid=(str(row["conselhoos_empresa_id"])
                      if row.get("conselhoos_empresa_id") else None),
        nome=row.get("nome") or f"projeto {project_id}",
    )


def _scope_de_empresa_uuid(cur, empresa_uuid: str) -> Optional[RaciScope]:
    """UUID do ConselhoOS -> escopo, pela ponte da migration 056.

    `empresas.conselhoos_empresa_id` é o caminho CERTO e já existia; o que o
    código fazia era pior — resolvia por `ILIKE '%nome do projeto%'` na tabela
    `empresas` do ConselhoOS, o que casa "Alba" com qualquer empresa cujo nome
    contenha Alba e falha calado quando o nome do projeto diverge do da empresa.
    """
    cur.execute(
        """SELECT p.id, p.nome, e.nome_canonico
             FROM empresas e
             LEFT JOIN projects p ON p.empresa_id = e.id
            WHERE e.conselhoos_empresa_id = %s
            ORDER BY p.id
            LIMIT 1""",
        (empresa_uuid,),
    )
    row = cur.fetchone()
    if not row:
        return None
    row = dict(row)
    return RaciScope(
        target=write_target(),
        project_id=int(row["id"]) if row.get("id") else None,
        empresa_uuid=str(empresa_uuid),
        nome=row.get("nome") or row.get("nome_canonico") or "?",
    )


def resolve_scope(chave: Any) -> RaciScope:
    """Resolve escopo a partir de `project_id` (int) OU `empresa_id` (UUID).

    Aceita as duas formas porque os chamadores estão em meio de travessia: a
    borda do webhook já tem `project_id` em mão (e jogava fora para ir buscar um
    UUID por nome), mas as 92 propostas na fila e as dedup-keys emitidas
    carregam UUID. Exigir uma só forma obrigaria a migrar a fila em lote, que é
    exatamente o que se decidiu não fazer.
    """
    raw = str(chave or "").strip()
    if not raw:
        raise RaciScopeError("chave de escopo vazia")

    with get_db() as conn:
        cur = conn.cursor()
        if _INT_RE.match(raw):
            scope = _scope_de_projeto(cur, int(raw))
            if not scope:
                raise RaciScopeError(f"projeto {raw} não existe no INTEL")
        elif _UUID_RE.match(raw):
            scope = _scope_de_empresa_uuid(cur, raw)
            if not scope:
                raise RaciScopeError(
                    f"empresa {raw} do ConselhoOS não tem vínculo no INTEL "
                    "(empresas.conselhoos_empresa_id) — a migration 056 é a ponte"
                )
        else:
            raise RaciScopeError(f"chave de escopo não é int nem uuid: {raw[:60]!r}")
    return scope


def resolve_scope_by_group_jid(group_jid: str) -> Optional[RaciScope]:
    """Grupo de WhatsApp -> escopo. None quando o grupo não mapeia projeto.

    Substitui o `group_jid -> projects.nome -> ILIKE em empresas (ConselhoOS)`
    que a borda fazia. O `project_id` está em `project_whatsapp_groups`: a
    viagem ao outro banco só existia porque o destino era lá.
    """
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT project_id FROM project_whatsapp_groups
                WHERE group_jid = %s AND ativo = TRUE LIMIT 1""",
            (group_jid,),
        )
        row = cur.fetchone()
        if not row:
            return None
        pid = row["project_id"] if isinstance(row, dict) else row[0]
        return _scope_de_projeto(cur, int(pid))


# ─────────────────────────────────────────────────────────────────────────────
# Tradução de referência de item
# ─────────────────────────────────────────────────────────────────────────────
# Projeto com caminho PRÓPRIO de leitura de grupo. O #28 tem `jabo_group_raci`,
# que aplica sem gate humano por decisão do Renato de 16/08 e já escreve em
# `raci_itens`. Deixá-lo entrar também no caminho genérico daria DOIS escritores
# para o mesmo RACI, cada um com política diferente de confiança.
PROJETOS_COM_CAMINHO_PROPRIO = (28,)


def grupo_raci_habilitado(scope: RaciScope) -> bool:
    """Este projeto pode ter o RACI mexido por mensagem de grupo?

    POR QUE ESTE GATE NASCEU JUNTO COM A FONTE ÚNICA (27/10/26). Até aqui o
    alcance do caminho de grupo era um ACIDENTE: a borda resolvia a empresa por
    `ILIKE '%nome do projeto%'` na tabela `empresas` do ConselhoOS, então só
    rodava onde havia empresa de conselho — #24 e #26. Ao trocar a resolução
    para `project_whatsapp_groups`, o alcance passaria a ser "todo projeto com
    RACI e grupo ativo", e medido em 27/10 isso incluiria o **#47 (RENATAO JURID
    CONTAB)**: um grupo de advogados e contadores, com 8 itens de RACI, que
    passaria a receber `✅ Atualizado` automático sem ninguém ter pedido.

    Ampliar o alcance é decisão de DOMÍNIO, não efeito colateral de refatoração
    — a mesma regra que o `jabo_group_raci` cravou ao abrir mão do gate humano
    só no Jabô: *"a exceção é do domínio, não da tecnologia; não generalizar"*.
    Por isso o default preserva exatamente o conjunto que já rodava, e crescer
    exige dizer o número do projeto em `RACI_GRUPO_PROJECTS`.
    """
    if scope.project_id in PROJETOS_COM_CAMINHO_PROPRIO:
        return False
    if scope.empresa_uuid:
        return True
    extra = (os.getenv("RACI_GRUPO_PROJECTS") or "").strip()
    if not extra:
        return False
    permitidos = {p.strip() for p in extra.replace(";", ",").split(",") if p.strip()}
    return str(scope.project_id) in permitidos


def resolve_item_ref(scope: RaciScope, ref: Any) -> Optional[str]:
    """Referência de item (int do INTEL ou UUID do ConselhoOS) -> id NATIVO do target.

    Devolve None quando a referência não tem par do lado do target — que é
    informação, não erro: é o caso do item que nasceu só no INTEL (3 abertos na
    Vallen em 27/10, entre eles o domínio `vallenclinic`) e das propostas cujo
    gêmeo de ata nunca foi importado.
    """
    raw = str(ref or "").strip()
    if not raw:
        return None

    nativo_int = bool(_INT_RE.match(raw))
    nativo_uuid = bool(_UUID_RE.match(raw))
    if not (nativo_int or nativo_uuid):
        return None

    if scope.target == TARGET_INTEL:
        if nativo_int:
            return raw
        # UUID -> id do INTEL pelo ponteiro da 084.
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT id FROM raci_itens
                    WHERE conselhoos_raci_id = %s::uuid
                      AND (%s IS NULL OR project_id = %s)
                    LIMIT 1""",
                (raw, scope.project_id, scope.project_id),
            )
            row = cur.fetchone()
        return str(row["id"] if isinstance(row, dict) else row[0]) if row else None

    # target = conselhoos
    if nativo_uuid:
        return raw
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT conselhoos_raci_id FROM raci_itens WHERE id = %s", (int(raw),))
        row = cur.fetchone()
    val = (row["conselhoos_raci_id"] if isinstance(row, dict) else row[0]) if row else None
    return str(val) if val else None


def item_id_parece_valido(scope: RaciScope, ref: Any) -> bool:
    """O id tem o FORMATO que o target usa? (filtro de alucinação do LLM)

    Existe porque o prompt pede ao modelo um id do contexto e ele às vezes
    inventa — em 08/06 devolveu `item_id="13"` e UUIDs inéditos. O formato é a
    primeira peneira; a segunda (pertencer ao contexto) continua no chamador.
    """
    raw = str(ref or "").strip()
    if scope.target == TARGET_INTEL:
        return bool(_INT_RE.match(raw))
    return bool(_UUID_RE.match(raw))


# ─────────────────────────────────────────────────────────────────────────────
# Leitura
# ─────────────────────────────────────────────────────────────────────────────
def _norm_row(row: Dict, target: str) -> Dict:
    """Uma linha dos dois bancos no MESMO formato.

    `id` sai sempre como `str` (os consumidores já fazem `str(...)` e a fila
    guarda `item_id` como `text`), e `updated_at` é o nome único do carimbo de
    movimento — no INTEL a coluna é `atualizado_em`. Normalizar aqui é o que
    permite `generate_raci_report` não saber de qual banco veio.
    """
    d = dict(row)
    return {
        "id": str(d.get("id")),
        "area": d.get("area"),
        "acao": d.get("acao") or "",
        "status": (d.get("status") or "pendente"),
        "prazo": d.get("prazo"),
        "notas": d.get("notas") or "",
        "responsavel_r": d.get("responsavel_r"),
        "responsavel_a": d.get("responsavel_a"),
        "responsavel_c": d.get("responsavel_c"),
        "responsavel_i": d.get("responsavel_i"),
        "updated_at": d.get("updated_at"),
        "concluido_relatado_em": d.get("concluido_relatado_em"),
        "fonte": target,
    }


_COLS_INTEL = """
    id, area, acao, prazo, status, notas,
    responsavel_r, responsavel_a, responsavel_c, responsavel_i,
    atualizado_em AS updated_at, concluido_relatado_em
"""
_COLS_COS = """
    id, area, acao, prazo, status, notas,
    responsavel_r, responsavel_a, responsavel_c, responsavel_i,
    updated_at, concluido_relatado_em
"""


def list_open_items(scope: RaciScope) -> List[Dict]:
    """Itens NÃO-concluídos do escopo — o contexto que o classificador recebe.

    É a metade esquecida do conserto: apontar só a escrita para o INTEL deixaria
    o parser procurando candidatos do outro lado, e resposta de responsável
    sobre um item que só existe aqui não casaria com nada e se perderia calada.
    """
    if scope.target == TARGET_INTEL:
        pid = scope.exige_project()
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""SELECT {_COLS_INTEL} FROM raci_itens
                     WHERE project_id = %s AND status != 'concluido'
                     ORDER BY prazo NULLS LAST, id""",
                (pid,),
            )
            rows = cur.fetchall()
        return [_norm_row(r, scope.target) for r in rows]

    import psycopg2.extras
    conn = _cos_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute(
            f"""SELECT {_COLS_COS} FROM raci_itens
                 WHERE empresa_id = %s AND status != 'concluido'
                 ORDER BY prazo NULLS LAST""",
            (scope.exige_empresa(),),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    return [_norm_row(r, scope.target) for r in rows]


def report_rows(scope: RaciScope) -> List[Dict]:
    """Linhas do relatório semanal: abertos + concluídos ainda não relatados.

    O filtro de `concluido_relatado_em` é o que faz o item concluído aparecer
    UMA vez e sumir. A coluna existia só no ConselhoOS; a 088 a criou no INTEL
    porque sem ela o relatório não distingue "fechou agora" de "fechou em
    setembro" e a seção Concluídas vira lista histórica toda segunda.
    """
    if scope.target == TARGET_INTEL:
        pid = scope.exige_project()
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""SELECT {_COLS_INTEL} FROM raci_itens
                     WHERE project_id = %s
                       AND (status != 'concluido' OR concluido_relatado_em IS NULL)""",
                (pid,),
            )
            rows = cur.fetchall()
        return [_norm_row(r, scope.target) for r in rows]

    import psycopg2.extras
    conn = _cos_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute(
            f"""SELECT {_COLS_COS} FROM raci_itens
                 WHERE empresa_id = %s
                   AND (status != 'concluido' OR concluido_relatado_em IS NULL)""",
            (scope.exige_empresa(),),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    return [_norm_row(r, scope.target) for r in rows]


def get_item(scope: RaciScope, ref: Any) -> Optional[Dict]:
    """Um item pela referência (traduzida). None se não existe do lado do target."""
    nativo = resolve_item_ref(scope, ref)
    if not nativo:
        return None

    if scope.target == TARGET_INTEL:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT {_COLS_INTEL} FROM raci_itens WHERE id = %s", (int(nativo),)
            )
            row = cur.fetchone()
        return _norm_row(row, scope.target) if row else None

    import psycopg2.extras
    conn = _cos_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute(f"SELECT {_COLS_COS} FROM raci_itens WHERE id = %s", (nativo,))
        row = cur.fetchone()
    finally:
        conn.close()
    return _norm_row(row, scope.target) if row else None


def item_acoes(scope: RaciScope, refs: List[Any]) -> Dict[str, str]:
    """{referência original -> ação}. Snapshot para a tela de review.

    Chaveia pela referência que o CHAMADOR passou, não pelo id nativo: quem
    pediu tem UUID na mão (é o que está gravado na fila) e precisa reencontrar
    a sua própria linha no dicionário.
    """
    out: Dict[str, str] = {}
    for ref in refs or []:
        item = get_item(scope, ref)
        if item:
            out[str(ref)] = item["acao"]
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Escrita
# ─────────────────────────────────────────────────────────────────────────────
def update_item(scope: RaciScope, ref: Any, *, status: Optional[str] = None,
                prazo: Optional[Any] = None, append_note: Optional[str] = None) -> bool:
    """Aplica a mudança no item. True se escreveu, False se não havia o que mudar.

    `append_note` CONCATENA (`notas = COALESCE(notas,'') || ...`) — nunca
    sobrescreve. Campo de texto curado reescrito inteiro foi o dano da cascata
    de 14/08, e aqui o texto é de governança de cliente.

    `concluido_em` é carimbado pelo runner quando o status vira `concluido`, com
    `concluido_em_fonte='grupo_wa'` — a procedência é o que torna o auto-update
    visível para quem lê a matriz. No ConselhoOS esse par não é escrito: lá a
    coluna de procedência tem outro contrato e este módulo não mexe no que não
    consolidou.
    """
    nativo = resolve_item_ref(scope, ref)
    if not nativo:
        raise RaciStoreError(f"item {str(ref)[:60]!r} sem par no target {scope.target}")

    if status and status not in STATUS_VALIDOS:
        raise RaciStoreError(
            f"status {status!r} fora do CHECK do banco (aceitos: {', '.join(STATUS_VALIDOS)})"
        )

    sets: List[str] = []
    params: List[Any] = []
    if status:
        sets.append("status = %s")
        params.append(status)
    if prazo:
        sets.append("prazo = %s")
        params.append(prazo)
    if append_note:
        sets.append("notas = COALESCE(notas, '') || %s")
        params.append(append_note)
    if not sets:
        return False

    if scope.target == TARGET_INTEL:
        if status == "concluido":
            sets.append("concluido_em = COALESCE(concluido_em, NOW())")
            sets.append("concluido_em_fonte = COALESCE(concluido_em_fonte, 'grupo_wa')")
        sets.append("atualizado_em = NOW()")
        params.append(int(nativo))
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                f"UPDATE raci_itens SET {', '.join(sets)} WHERE id = %s", tuple(params)
            )
            conn.commit()
        return True

    sets.append("updated_at = NOW()")
    params.append(nativo)
    conn = _cos_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE raci_itens SET {', '.join(sets)} WHERE id = %s", tuple(params)
        )
        conn.commit()
    finally:
        conn.close()
    return True


def create_item(scope: RaciScope, payload: Dict,
                reuniao_id: Optional[str] = None) -> str:
    """Cria item novo e devolve o id (str). Levanta se o payload não basta.

    `prazo` é NOT NULL no ConselhoOS e OPCIONAL no INTEL — e a diferença é
    deliberada: item sem prazo não vira atrasado, que é o estado certo das
    responsabilidades permanentes ("coordenação da cadência"). Por isso a
    exigência de prazo é do TARGET, não da função: cobrar prazo no INTEL
    inventaria data onde a ausência é informação.

    `reuniao_id` não tem coluna no INTEL (as reuniões moram no ConselhoOS).
    Viaja em `origem` como `ata:<uuid>` para o rastro não morrer na travessia.
    """
    acao = (payload.get("acao") or "").strip()
    if not acao:
        raise RaciStoreError("item novo sem 'acao'")
    prazo = payload.get("prazo")
    area = (payload.get("area") or "Geral")[:100]
    notas = payload.get("prazo_motivo") or payload.get("notas") or None

    if scope.target == TARGET_INTEL:
        pid = scope.exige_project()
        origem = f"ata:{reuniao_id}" if reuniao_id else "wa_grupo"
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO raci_itens
                       (project_id, area, acao, prazo, status, origem,
                        responsavel_r, responsavel_a, responsavel_c, responsavel_i,
                        notas, sem_par_declarado_em)
                   VALUES (%s,%s,%s,%s,'pendente',%s,%s,%s,%s,%s,%s, NOW())
                   RETURNING id""",
                (pid, area, acao, prazo, origem[:100],
                 payload.get("responsavel_r"), payload.get("responsavel_a"),
                 payload.get("responsavel_c"), payload.get("responsavel_i"), notas),
            )
            row = cur.fetchone()
            conn.commit()
        # `sem_par_declarado_em` vem preenchido: item nascido de leitura de grupo
        # NÃO tem gêmeo de ata, e deixar os dois campos nulos o devolveria para a
        # fila de pareamento como "ainda não decidi" (a lição da 085). O CHECK
        # `chk_raci_sem_par_exclusivo` garante que não convive com um ponteiro.
        return str(row["id"] if isinstance(row, dict) else row[0])

    if not prazo:
        raise RaciStoreError(
            "item novo sem 'prazo' (coluna NOT NULL no ConselhoOS)"
        )
    import psycopg2.extras
    conn = _cos_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute(
            """INSERT INTO raci_itens
                   (empresa_id, reuniao_id, area, acao, prazo, status,
                    responsavel_r, responsavel_a, responsavel_c, responsavel_i, notas)
               VALUES (%s,%s,%s,%s,%s,'pendente',%s,%s,%s,%s,%s)
               RETURNING id""",
            (scope.exige_empresa(), reuniao_id, area, acao, prazo,
             payload.get("responsavel_r"), payload.get("responsavel_a"),
             payload.get("responsavel_c"), payload.get("responsavel_i"), notas),
        )
        new_id = str(cur.fetchone()["id"])
        conn.commit()
    finally:
        conn.close()
    return new_id


def mark_reported(scope: RaciScope) -> int:
    """Marca os concluídos não-relatados como relatados. Devolve a contagem."""
    if scope.target == TARGET_INTEL:
        pid = scope.exige_project()
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """UPDATE raci_itens SET concluido_relatado_em = NOW()
                    WHERE project_id = %s AND status = 'concluido'
                      AND concluido_relatado_em IS NULL""",
                (pid,),
            )
            n = cur.rowcount
            conn.commit()
        return n

    conn = _cos_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE raci_itens SET concluido_relatado_em = NOW()
                WHERE empresa_id = %s AND status = 'concluido'
                  AND concluido_relatado_em IS NULL""",
            (scope.exige_empresa(),),
        )
        n = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return n


def undo_hint_update(scope: RaciScope, ref: Any, status_antigo: Optional[str]) -> str:
    """O SQL que desfaz, com o CAST certo para o target.

    Existia como literal com `::uuid` grudado (`raci_smart_updates` ~502 e
    `raci_weekly_report` ~1040). Com id inteiro aquele hint virava SQL que não
    roda — e hint de undo que não roda é pior que hint ausente: quem precisar
    dele vai precisar sob pressão.
    """
    nativo = resolve_item_ref(scope, ref) or str(ref)
    alvo = f"'{nativo}'::uuid" if scope.target == TARGET_CONSELHOOS else str(nativo)
    col = "atualizado_em" if scope.target == TARGET_INTEL else "updated_at"
    return (
        f"UPDATE raci_itens SET status='{status_antigo}', {col}=NOW() "
        f"WHERE id={alvo};  -- target={scope.target}"
    )


def escopos_com_grupo() -> List[RaciScope]:
    """Os escopos elegíveis ao relatório semanal: têm RACI e grupo de WA ativo.

    Substitui a varredura antiga, que listava `empresas` do ConselhoOS e depois
    procurava o projeto por `ILIKE '%nome%'`. Com a fonte no INTEL a pergunta
    inverte: quem tem itens de RACI e um grupo para onde mandar. O efeito
    prático é incluir projeto de governança que nunca foi empresa do ConselhoOS
    — era o caso do Jabô, que precisava de um caminho próprio só por isso.
    """
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT DISTINCT p.id
                 FROM projects p
                 JOIN raci_itens ri ON ri.project_id = p.id
                 JOIN project_whatsapp_groups pwg
                      ON pwg.project_id = p.id AND pwg.ativo = TRUE
                ORDER BY p.id"""
        )
        ids = [(r["id"] if isinstance(r, dict) else r[0]) for r in cur.fetchall()]
        escopos = [s for s in (_scope_de_projeto(cur, int(i)) for i in ids) if s]
    # MESMO gate da leitura de grupo, e de propósito: um alcance só. Se o
    # `_preview_conselhos_enabled` for religado, o Renato recebe preview dos
    # projetos cujo RACI o sistema de fato alimenta — não de todo projeto que
    # por acaso tem itens e um grupo.
    return [s for s in escopos if grupo_raci_habilitado(s)]
