"""Escrita da camada de inteligência — estreita, auditada e reversível.

A DIRETRIZ (Renato, 10/08/26). "Se eu envio uma msg de WA para o Pretola, a
inteligência deve interpretar essa mensagem e atualizar o conhecimento
(frentes, projetos, tarefas). Se houver dúvida, deve me acionar."

O QUE ESTE MÓDULO É. O único caminho pelo qual a camada muda estado. Não é um
helper de conveniência: é o portão. Toda operação permitida está declarada em
`OPERACOES`, e o que não está declarado não acontece — uma camada que pode
escrever "o que fizer sentido" é uma camada que um dia apaga o que não
entendeu.

POR QUE UMA LISTA FECHADA, e não permissão por tabela. Permissão por tabela
responde "onde pode escrever"; a pergunta que importa é "o que pode fazer".
`UPDATE em tasks` autoriza tanto marcar um follow-up quanto fechar em massa
tarefas que o Renato não resolveu. A lista fechada obriga cada capacidade nova
a passar por uma decisão explícita — que é o oposto de descobrir a capacidade
depois, no diff.

O PISO DE CONFIANÇA. Abaixo de `PISO_ESCRITA` a camada NÃO escreve: devolve
`PropostaPendente`, para virar pergunta ao Renato. Isto não é o mesmo que
abster — abster no ambíguo foi o que produziu o caso Pretola, onde a frente
ficou "reativada" no memo e inexistente no banco
([[feedback_guarda_abstencao_vira_fabrica]]). Aqui o ambíguo tem destino: vira
proposta, e proposta não atendida é visível.

O QUE ESTE MÓDULO NÃO RESOLVE. A trava dura continua sendo do Postgres. Hoje a
camada roda com `cos_agent_ro`, que não escreve nada; quando o papel de escrita
existir, ele deve conceder acesso APENAS às tabelas desta lista. Enquanto isso,
este módulo é a disciplina — e disciplina sem trava vale enquanto todo mundo
chamar por aqui.
"""
import contextlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

from database import get_db

logger = logging.getLogger(__name__)

# Abaixo disto a camada pergunta em vez de escrever. 0.75 é chute fundamentado,
# não medição — o gate é a primeira retro com escrita ligada. Registrado como
# chute de propósito: número que finge ser medido é pior que número declarado.
PISO_ESCRITA = 0.75


class PropostaPendente(Exception):
    """A camada não teve confiança suficiente: isto vira pergunta, não escrita."""

    def __init__(self, operacao: str, motivo: str, confianca: float, payload: Dict[str, Any]):
        self.operacao = operacao
        self.motivo = motivo
        self.confianca = confianca
        self.payload = payload
        super().__init__(f"{operacao}: confianca {confianca:.2f} < piso {PISO_ESCRITA}")


class OperacaoNaoPermitida(Exception):
    """Fora da lista fechada. Não é para ser contornável em runtime."""


@dataclass(frozen=True)
class Operacao:
    nome: str
    tabela: str
    tipo: str            # 'insert' | 'update'
    campos: tuple        # colunas que a operação pode tocar — nada além disto
    descricao: str


# ---------------------------------------------------------------------------
# A LISTA FECHADA.
#
# Critério de entrada: (a) o efeito é reversível por uma linha do livro-razão;
# (b) o erro é visível para o Renato antes de causar dano; (c) resolve um caso
# real já observado. Nada entra aqui "porque pode ser útil".
#
# Fora de propósito, e não por esquecimento: contacts (merge já provou que
# apagar ficha é irreversível), messages (é o registro do que aconteceu — a
# camada interpreta, não reescreve), users, qualquer DELETE.
# ---------------------------------------------------------------------------
OPERACOES = {
    "criar_frente_board_hunt": Operacao(
        nome="criar_frente_board_hunt",
        tabela="board_hunt_frentes",
        tipo="insert",
        campos=("nome", "subtitulo", "project_id", "contato_id",
                "originador_contact_id", "originador_rotulo", "fase", "status",
                "piso_alvo", "nota"),
        descricao="A frente existe no mundo e não no banco — o caso Orbiz/Pretola.",
    ),
    "atualizar_fase_frente": Operacao(
        nome="atualizar_fase_frente",
        tabela="board_hunt_frentes",
        tipo="update",
        # Só JULGAMENTO. Dias/temperatura/bola/próximo passo derivam a cada
        # rodada; gravá-los refaz o defeito que a página já perdeu uma vez.
        campos=("fase", "status", "nota", "piso_alvo"),
        descricao="O fato move a frente de fase (contato aceitou conversar, reunião marcada).",
    ),
    "ligar_contato_a_projeto": Operacao(
        nome="ligar_contato_a_projeto",
        tabela="project_members",
        tipo="insert",
        campos=("project_id", "contact_id", "papel"),
        descricao="A conversa mostra que a pessoa participa de uma frente onde não estava ligada.",
    ),
    "criar_task_followup": Operacao(
        nome="criar_task_followup",
        tabela="tasks",
        tipo="insert",
        campos=("titulo", "descricao", "contact_id", "project_id",
                "data_vencimento", "prioridade", "status", "origem"),
        descricao="Compromisso explícito no fio ('Qua 12/08?') vira tarefa com dono e data.",
    ),
    "atualizar_status_raci": Operacao(
        nome="atualizar_status_raci",
        # `public.` explícito: as views `copilot.*` traduzem nomes de coluna e
        # sem o schema a errada parece existir ([[feedback_copilot_view_verify_consumer]]).
        tabela="public.raci_itens",
        tipo="update",
        # POR QUE `raci_itens` E NÃO `tasks`. Medido em 16/08: a peça que vai ao
        # grupo é montada por `raci_matrix` a partir dos 10 itens curados de
        # `raci_itens` — e os 10 têm `task_id = NULL`. Atualizar `tasks` mexeria
        # no backlog do Renato sem mudar UMA linha do que a executora lê. É a
        # armadilha de escrever no consumidor errado
        # ([[feedback_medir_o_consumidor_certo]]): a escrita teria sucesso, o
        # livro-razão registraria, e o efeito pretendido não existiria.
        #
        # `notas` fica de fora: é texto curado à mão e seria sobrescrito inteiro
        # — o dano da cascata de 14/08. `concluido_em_fonte` é a PROCEDÊNCIA, e
        # existe desde a 073; é ela que deixa o auto-update visível a quem lê.
        campos=("status", "concluido_em", "concluido_em_fonte"),
        descricao="Quem executa reporta no grupo que fez, e o RACI ainda diz pendente.",
    ),
    "registrar_nota_projeto": Operacao(
        nome="registrar_nota_projeto",
        tabela="project_notes",
        tipo="insert",
        campos=("project_id", "tipo", "titulo", "conteudo", "autor", "metadata"),
        descricao="O que a camada entendeu do fato, em texto, para o próximo run e para o humano.",
    ),
}


def _serializa(valor: Any) -> Any:
    """JSON não conhece date/Decimal; o livro-razão não pode falhar por isso."""
    if valor is None or isinstance(valor, (str, int, float, bool)):
        return valor
    if isinstance(valor, (dict, list)):
        # `str()` num dict devolve repr de Python — aspas simples, `None` em vez
        # de `null`. O livro-razão passaria a guardar algo que não é JSON e que
        # ninguém consegue reler programaticamente.
        return valor
    return str(valor)


def _para_o_banco(valor: Any) -> Any:
    """Adapta o valor ao driver antes do INSERT/UPDATE.

    `psycopg2` não sabe adaptar `dict` sozinho: manda `can't adapt type 'dict'`.
    Duas das cinco operações aceitam coluna `jsonb` (`registrar_nota_projeto`
    tem `metadata`), então toda nota com metadados falhava — e falhava CALADA,
    porque até 11/08 o motivo da recusa não saía do processo. Descoberto na
    primeira rodada em que a recusa passou a dizer por quê.
    """
    if isinstance(valor, (dict, list)):
        return json.dumps(valor, ensure_ascii=False)
    return valor


def escrever(
    operacao: str,
    dados: Dict[str, Any],
    *,
    motivo: str,
    confianca: float,
    agente: str = "cos_agent_local",
    run_id: Optional[str] = None,
    fato_origem: Optional[str] = None,
    registro_id: Optional[int] = None,
    conn=None,
) -> int:
    """Executa UMA operação da lista fechada e grava o livro-razão.

    Devolve o id do registro criado/atualizado. Levanta `PropostaPendente` se a
    confiança for baixa e `OperacaoNaoPermitida` se a operação ou um campo não
    estiverem declarados.

    `motivo` é obrigatório e vai para a auditoria em português: o livro-razão
    tem que ser julgável por um humano sem ler código.

    `conn` existe para o runner do agente local (`scripts/cos_agent/run.py`),
    que roda fora do app e tem a própria conexão via COS_RW_URL. A alternativa
    seria ele reimplementar a lista fechada — que é exatamente o defeito que
    esta auditoria vem catalogando: duplicar a REGRA em vez de chamar por ela.
    Conexão injetada não é fechada aqui; quem abriu, fecha.
    """
    op = OPERACOES.get(operacao)
    if op is None:
        raise OperacaoNaoPermitida(
            f"{operacao!r} não está na lista fechada. Operação nova exige decisão "
            f"explícita, não improviso em runtime."
        )

    if not motivo or not motivo.strip():
        raise OperacaoNaoPermitida(
            f"{operacao}: escrita sem motivo é escrita inauditável."
        )

    extras = set(dados) - set(op.campos)
    if extras:
        raise OperacaoNaoPermitida(
            f"{operacao}: campos fora da operação: {sorted(extras)}. "
            f"Permitidos: {sorted(op.campos)}"
        )

    if confianca < PISO_ESCRITA:
        raise PropostaPendente(operacao, motivo, confianca, dados)

    if op.tipo == "update" and registro_id is None:
        raise OperacaoNaoPermitida(f"{operacao}: update exige registro_id")

    with (contextlib.nullcontext(conn) if conn is not None else get_db()) as _conn:
        cur = _conn.cursor()

        valor_anterior = None
        if op.tipo == "update":
            cols = ", ".join(op.campos)
            cur.execute(f"SELECT {cols} FROM {op.tabela} WHERE id = %s", (registro_id,))
            row = cur.fetchone()
            if row is None:
                raise OperacaoNaoPermitida(
                    f"{operacao}: registro {registro_id} não existe em {op.tabela}"
                )
            # Só o estado ANTERIOR dos campos que esta operação toca. Guardar a
            # linha inteira daria a impressão de que desfazer restaura tudo.
            valor_anterior = {k: _serializa(row[k]) for k in op.campos}

            sets = ", ".join(f"{k} = %s" for k in dados)
            cur.execute(
                f"UPDATE {op.tabela} SET {sets} WHERE id = %s RETURNING id",
                (*(_para_o_banco(v) for v in dados.values()), registro_id),
            )
            alvo_id = cur.fetchone()["id"]
        else:
            cols = ", ".join(dados)
            ph = ", ".join(["%s"] * len(dados))
            cur.execute(
                f"INSERT INTO {op.tabela} ({cols}) VALUES ({ph}) RETURNING id",
                tuple(_para_o_banco(v) for v in dados.values()),
            )
            alvo_id = cur.fetchone()["id"]

        cur.execute(
            """INSERT INTO agent_writes
                 (agente, run_id, operacao, tabela, registro_id,
                  valor_anterior, valor_novo, motivo, fato_origem, confianca)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                agente, run_id, operacao, op.tabela, alvo_id,
                json.dumps(valor_anterior) if valor_anterior else None,
                json.dumps({k: _serializa(v) for k, v in dados.items()}),
                motivo.strip(), fato_origem, round(float(confianca), 2),
            ),
        )

    logger.info(
        "agent_write: %s em %s#%s (confianca %.2f) — %s",
        operacao, op.tabela, alvo_id, confianca, motivo,
    )
    return alvo_id


def desfazer(write_id: int, por: str = "renato") -> Dict[str, Any]:
    """Reverte UMA escrita da camada.

    UPDATE volta ao valor anterior. INSERT **não é apagado**: apagar levaria
    junto o que tiver sido pendurado no registro desde então — o mesmo formato
    de dano do merge em CASCADE. A linha do livro-razão é marcada como desfeita
    e o registro fica para decisão humana, que é quem sabe se há dependente.
    """
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM agent_writes WHERE id = %s", (write_id,))
        w = cur.fetchone()
        if w is None:
            raise ValueError(f"escrita {write_id} não existe")
        if w["desfeito_em"]:
            return {"status": "ja_desfeito", "write_id": write_id}

        anterior = w["valor_anterior"]
        if anterior:
            if isinstance(anterior, str):
                anterior = json.loads(anterior)
            sets = ", ".join(f"{k} = %s" for k in anterior)
            cur.execute(
                f"UPDATE {w['tabela']} SET {sets} WHERE id = %s",
                (*(_para_o_banco(v) for v in anterior.values()), w["registro_id"]),
            )
            resultado = "revertido"
        else:
            resultado = "insert_marcado_nao_apagado"

        cur.execute(
            "UPDATE agent_writes SET desfeito_em = now(), desfeito_por = %s WHERE id = %s",
            (por, write_id),
        )

    return {"status": resultado, "write_id": write_id,
            "tabela": w["tabela"], "registro_id": w["registro_id"]}
