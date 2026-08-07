"""Atos que podem ter resolvido UMA task — a pergunta "ele já fez isso?".

POR QUE EXISTE (07/08/2026, pedido da sessão CoS). Causa-raiz de uma classe
inteira de defeito: o sistema sabe o que a CAMADA cria (draft, task, evento) e é
cego ao que o RENATO faz direto. Resultado: a triagem propõe o que ele já fez —
5 falhas medidas num único dia (06/08).

A view `atos_que_resolvem` (064) já responde "houve ato", mas é indexada por
`pessoa_id`. Quem tria uma task olha as pessoas citadas no TEXTO dela e pode
nunca chegar à pessoa sob a qual o ato foi roteado. O caso que originou:

    task #999732 "Avaliar inscrição no Concurso de Qualidade — Sudoeste de Minas"
    descrição nomeia Andressa e Orestes  ·  task.contact_id = 16187 (Reginaldo)
    ato real: 05/08 16:47, WhatsApp ao Reginaldo, "Chegou aqui o regulamento do
              Concurso de Qualidade do Sudoeste de Minas..."

O ato estava a um passo — sob o próprio `contact_id` da task — e a triagem
julgou sem olhar. Este módulo faz esse passo virar chamada, não disciplina.

DUAS DECISÕES DE DESENHO:

1. O CONJUNTO DE PESSOAS é `{task.contact_id} ∪ {membros do projeto}`. Só o
   contact_id perde o ato roteado a um terceiro (a Andressa confirma o pagamento
   que a contraparte não confirma); o projeto inteiro sem filtro traz a agenda
   alheia.

2. RELEVÂNCIA POR SOBREPOSIÇÃO DE TERMOS, e ela não é opcional. No mesmo dia e
   no mesmo contato do exemplo há "o controle do portão não funcionou" e uma
   figurinha. Sem filtro, qualquer conversa vira prova de que a task foi
   resolvida — e dar por feito o que não foi feito é o erro caro desta família.

O QUE ELE NÃO FAZ: não fecha task, não decide. Devolve evidência ordenada para
quem julga — a mesma regra do chip "mexeu nisso" no cockpit: é PERGUNTA, nunca
veredito ([[feedback_cos_autonomy_policy]]: desfazer o dado por concluído nunca
é ação automática).
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List

from database import get_db

logger = logging.getLogger(__name__)

# Palavras que aparecem em qualquer título e não distinguem nada. Sem esta
# lista, "de/para/com" casam com toda mensagem e o score deixa de significar.
_VAZIAS = {
    "para", "com", "sem", "por", "que", "uma", "uns", "das", "dos", "nas", "nos",
    "pra", "pro", "ate", "sobre", "como", "quando", "onde", "qual", "quais",
    "task", "tarefa", "fazer", "avaliar", "verificar", "confirmar", "enviar",
    "mandar", "ver", "checar", "falar", "conversar", "definir", "decidir",
    "esta", "este", "isso", "aquilo", "mais", "menos", "muito", "pouco",
}


def _termos(texto: str) -> set:
    """Termos significativos de um texto — 4+ letras, sem acento, sem as vazias."""
    t = (texto or "").lower()
    for de, para in (("á", "a"), ("à", "a"), ("ã", "a"), ("â", "a"), ("é", "e"),
                     ("ê", "e"), ("í", "i"), ("ó", "o"), ("ô", "o"), ("õ", "o"),
                     ("ú", "u"), ("ç", "c")):
        t = t.replace(de, para)
    return {p for p in re.findall(r"[a-z]{4,}", t) if p not in _VAZIAS}


def atos_da_task(task_id: int, dias: int = 45, minimo: int = 1) -> Dict:
    """Atos posteriores à criação da task, das pessoas ligadas a ela.

    `minimo` é quantos termos precisam bater para o ato ser considerado
    relacionado. 1 é deliberadamente permissivo: aqui o falso positivo custa uma
    leitura, e o falso negativo custa cobrar o que já foi feito — que é o defeito
    que este módulo existe pra matar. Os atos vêm ordenados por relevância, então
    quem lê decide onde parar.
    """
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT t.id, t.titulo, t.contexto, t.contact_id, t.project_id,
                      t.data_criacao, t.status, c.nome AS contato
                 FROM tasks t LEFT JOIN contacts c ON c.id = t.contact_id
                WHERE t.id = %s""",
            (task_id,),
        )
        task = cur.fetchone()
        if not task:
            return {"task_id": task_id, "erro": "task nao encontrada"}
        task = dict(task)

        # Pessoas: o contato da task + os membros do projeto. A ficha do próprio
        # Renato fica FORA: mensagem que a camada manda pra ele conta como ato
        # dele e o sistema passa a ouvir o próprio eco (4 casos em 2 dias de
        # agosto, [[feedback_maquina_ouve_o_proprio_eco]]).
        cur.execute(
            """SELECT DISTINCT p FROM (
                   SELECT %(contact)s::int AS p
                   UNION
                   SELECT pm.contact_id FROM project_members pm
                    WHERE pm.project_id = %(proj)s
               ) x
               WHERE p IS NOT NULL
                 AND p <> COALESCE((SELECT contact_id FROM users WHERE id = 1), -1)""",
            {"contact": task["contact_id"], "proj": task["project_id"]},
        )
        pessoas = [r["p"] if isinstance(r, dict) else r[0] for r in cur.fetchall()]
        if not pessoas:
            return {"task_id": task_id, "titulo": task["titulo"], "pessoas": [],
                    "atos": [], "nota": "task sem contato e sem membros de projeto — "
                                        "nao ha por onde cruzar"}

        cur.execute(
            """SELECT a.quem, a.papel, a.canal, a.pessoa_id, a.o_que, a.evidencia,
                      a.quando, a.declara_execucao, c.nome AS pessoa
                 FROM atos_que_resolvem a
                 LEFT JOIN contacts c ON c.id = a.pessoa_id
                WHERE a.pessoa_id = ANY(%(pessoas)s)
                  AND a.quando > %(desde)s
                  AND a.quando > now() - make_interval(days => %(dias)s)
                ORDER BY a.quando DESC
                LIMIT 300""",
            {"pessoas": pessoas, "desde": task["data_criacao"], "dias": dias},
        )
        brutos = [dict(r) for r in cur.fetchall()]

    alvo = _termos(f"{task['titulo']} {task.get('contexto') or ''}")
    atos: List[Dict] = []
    for a in brutos:
        casados = alvo & _termos(a.get("evidencia") or "")
        if len(casados) < minimo:
            continue
        a["termos_em_comum"] = sorted(casados)
        a["score"] = len(casados) + (1 if a.get("declara_execucao") else 0)
        a["quando"] = a["quando"].isoformat() if a.get("quando") else None
        atos.append(a)

    atos.sort(key=lambda x: (-x["score"], x["quando"] or ""), reverse=False)
    return {
        "task_id": task_id,
        "titulo": task["titulo"],
        "status": task["status"],
        "contato": task["contato"],
        "pessoas": pessoas,
        # O denominador vai junto: "0 atos relevantes" só significa alguma coisa
        # ao lado de quantos atos existiam pra filtrar. Sem isso, filtro com
        # vocabulário errado devolve zero e parece ausência de ato.
        "atos_no_periodo": len(brutos),
        "atos": atos[:12],
        "nota": ("nenhum ato bateu com o assunto da task — mas houve "
                 f"{len(brutos)} ato(s) com estas pessoas no periodo; se o titulo "
                 "usa vocabulario diferente do que voces falam, leia os brutos")
                if brutos and not atos else None,
    }
