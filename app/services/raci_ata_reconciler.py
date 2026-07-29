"""
Reconciliação de RACI por ATA — a 3ª peça (29/07/2026).

O QUE ESTAVA QUEBRADO
---------------------
Cada reunião empilhava um conjunto novo de itens de RACI e nunca olhava para o
que já estava aberto. A Vallen acumulou 61 itens em 5 reuniões; os de junho só
foram fechados em 29/07, à mão, pela sessão CoS. O mecanismo é sempre o mesmo:
a ata nova retoma um assunto que já era um item aberto, alguém cadastra o
assunto como item NOVO, e o antigo vira fóssil.

E, ao investigar, o buraco era maior: NÃO HAVIA pipeline ata -> RACI nenhum.
`saveRaciFromAta` existe no repo ConselhoOS mas só é chamada por um script de
teste; a geração real de ata foi delegada ao worker Railway, que apenas grava
`reunioes.ata_md`. Os itens existentes nasceram de inserção em lote. Então esta
peça não "conserta" um pipeline — ela é o pipeline, com a reconciliação embutida
desde o primeiro dia.

A REGRA QUE O RENATO CRAVOU (29/07): **(a)**
--------------------------------------------
Item aberto que a ata nova NÃO menciona **fica aberto, intocado**. Nada fecha
por silêncio — silêncio numa ata não é entrega. Este módulo por isso NUNCA
escreve nesses itens; ele só os LISTA em `nao_tratados`, pra que o silêncio seja
visível em vez de virar fóssil. As outras duas leituras (fechar sozinho / propor
fechamento) foram recusadas explicitamente.

O QUE ELE FAZ
-------------
Depois que a ata é gerada, uma chamada ao LLM cruza a ata com os itens abertos
das reuniões ANTERIORES e devolve duas coisas:
  - `atualizacoes`: item aberto que a ata menciona (avançou, fechou, mudou prazo);
  - `novos`: ação da reunião que NÃO corresponde a nenhum item aberto.
Ambas viram PROPOSTA em `raci_group_proposals` (status pending_review) — a mesma
fila human-gated que já existe pro caminho de grupo, com tela, apply/dismiss/
reopen e o atalho "aplica o RACI-N" no WhatsApp. Nada toca o RACI do cliente sem
o Renato aprovar: governança de cliente não muda sozinha (mesma regra do shadow).

O que este módulo NÃO faz, de propósito: aplicar, fechar, rebaixar ou apagar
qualquer item.
"""
import json
import logging
import os
import re
from datetime import date, timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx

from database import get_db
from services import llm, llm_usage
from services.raci_smart_updates import _apply_guardrails, _conselhoos_url

log = logging.getLogger("raci_ata_reconciler")

ANTHROPIC_API_KEY = (os.getenv("ANTHROPIC_API_KEY") or "").strip()

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

# Prazo é NOT NULL no ConselhoOS (ao contrário do RACI do INTEL, onde ele pode
# faltar de propósito). Quando a ata não define data, o item novo precisa de
# alguma — e a escolha honesta é a data em que o conselho volta a olhar para
# ele: a próxima reunião agendada. Sem reunião futura cadastrada, cai neste
# fallback. Em qualquer um dos dois casos a proposta diz de onde veio o prazo,
# pra ninguém confundir convenção com compromisso assumido na reunião (data
# inventada silenciosamente vira falso-atrasado, que foi o que corroeu a
# credibilidade do RACI da Vallen em 13/07).
PRAZO_FALLBACK_DAYS = 30

# Teto de itens abertos no prompt. Acima disso o contexto encarece sem melhorar
# o casamento — e nenhuma empresa passou de 61 itens (a Vallen inteira, contando
# os concluídos). Ordenado por prazo, então o corte descarta o que vence mais
# longe. Se cortar, o run REPORTA (nunca silencia truncagem).
MAX_ITENS_CONTEXTO = 80

# A ata gerada tem 8-15k chars por desenho. 40k cobre com folga inclusive as
# atas longas com tabelas, sem mandar transcrição inteira (que chega a 80k).
MAX_ATA_CHARS = 40_000


class AtaReconcileError(Exception):
    """Falha que impede a reconciliação (reunião inexistente, sem ata, sem env)."""


# ─────────────────────────────────────────────────────────────────────────────
# Leitura (ConselhoOS)
# ─────────────────────────────────────────────────────────────────────────────
def _cos_conn():
    url = _conselhoos_url()
    if not url:
        raise AtaReconcileError("CONSELHOOS_DATABASE_URL ausente ou vazia")
    import psycopg2
    import psycopg2.extras
    return psycopg2.connect(url)


def load_reuniao(reuniao_id: str) -> Dict[str, Any]:
    """Reunião + empresa. Levanta AtaReconcileError se não existe ou não tem ata."""
    conn = _cos_conn()
    try:
        import psycopg2.extras
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute(
            """SELECT r.id, r.empresa_id, r.data, r.ata_md, r.transcricao, e.nome AS empresa_nome
                 FROM reunioes r JOIN empresas e ON e.id = r.empresa_id
                WHERE r.id = %s""",
            (reuniao_id,),
        )
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        raise AtaReconcileError(f"reuniao {reuniao_id} nao encontrada no ConselhoOS")
    d = dict(row)
    if not (d.get("ata_md") or "").strip():
        raise AtaReconcileError(f"reuniao {reuniao_id} ainda nao tem ata gerada")
    return d


def open_items(empresa_id: str, reuniao_id: str) -> List[Dict[str, Any]]:
    """TODOS os itens abertos da empresa, marcando quais já são desta reunião.

    A primeira versão excluía os itens da própria reunião do contexto — parecia
    óbvio (são os itens que a ata está criando agora). O dry-run contra a Vallen
    de 28/07 provou o contrário: aquela reunião já tinha 9 itens cadastrados, o
    modelo não os via, e re-propôs cinco deles como "novos" (mapeamento de
    processos, modelagem financeira, PJ × CLT, recepção, vídeos de médicos).
    Ou seja: a exclusão reproduzia exatamente o empilhamento que esta peça
    existe pra matar, só que dentro da mesma reunião.

    Então o contexto leva tudo que está aberto. `desta_reuniao` serve pra
    separar depois: só item de reunião ANTERIOR pode ser "não tratado" — dizer
    que um item criado nesta reunião não foi tratado nela não quer dizer nada.

    Traz também os itens JÁ CONCLUÍDOS desta reunião — segundo achado do mesmo
    dry-run. O "Mapa do phase-out ao grupo" da Vallen já existia e já estava
    concluído; como o contexto só levava abertos, o modelo não o via e propunha
    de novo, agora como pendente. Duplicar um compromisso já entregue é pior que
    duplicar um aberto: reabre trabalho fechado. Concluído de reunião ANTERIOR
    fica de fora de propósito — a Vallen tem 50, e carregar todo o histórico
    encareceria o contexto pra proteger contra um caso muito mais raro.

    Inclui item com `reuniao_id` NULL (avulso, cadastrado fora de ata): também é
    passivo aberto e também merece reconciliação.
    """
    conn = _cos_conn()
    try:
        import psycopg2.extras
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute(
            """SELECT i.id, i.acao, i.area, i.status, i.prazo, i.responsavel_r,
                      r.data AS reuniao_data,
                      (i.reuniao_id = %s) AS desta_reuniao
                 FROM raci_itens i
            LEFT JOIN reunioes r ON r.id = i.reuniao_id
                WHERE i.empresa_id = %s
                  AND (i.status <> 'concluido' OR i.reuniao_id = %s)
             ORDER BY i.prazo NULLS LAST""",
            (reuniao_id, empresa_id, reuniao_id),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def next_meeting_date(empresa_id: str, after: date) -> Optional[date]:
    """Data da próxima reunião agendada depois de `after`. None se não houver."""
    conn = _cos_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT data FROM reunioes
                WHERE empresa_id = %s AND data > %s AND status = 'agendada'
             ORDER BY data ASC LIMIT 1""",
            (empresa_id, after),
        )
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return None
    d = row[0]
    return d.date() if hasattr(d, "date") else d


# ─────────────────────────────────────────────────────────────────────────────
# Prompt + parsing (puros — testáveis sem banco nem rede)
# ─────────────────────────────────────────────────────────────────────────────
def build_prompt(empresa_nome: str, data_reuniao: str, ata_md: str,
                 itens: List[Dict[str, Any]]) -> str:
    if itens:
        itens_str = "\n".join(
            f"[{i+1}] id={it['id']} | {it['acao']}\n"
            f"    Área: {it.get('area') or '?'} | Responsável: {it.get('responsavel_r') or '?'}"
            f" | Prazo: {it.get('prazo')} | Status: {it['status']}"
            f"{' ✅ JÁ CONCLUÍDO' if it['status'] == 'concluido' else ''}"
            f" | Origem: {'ESTA reunião' if it.get('desta_reuniao') else (it.get('reuniao_data') or 'avulso')}"
            for i, it in enumerate(itens)
        )
    else:
        itens_str = "(nenhum item aberto)"

    return f"""Você é analista de governança corporativa reconciliando a matriz RACI de um conselho consultivo.

**Empresa:** {empresa_nome}
**Reunião:** {data_reuniao}

## ITENS RACI JÁ ABERTOS DESTA EMPRESA
Cada um tem um id UUID. São compromissos ainda não concluídos. Alguns podem já
ter sido cadastrados a partir DESTA reunião (marcados "Origem: ESTA reunião") —
esses também contam: se a ata fala deles, é atualização, e nunca item novo.

{itens_str}

## ATA DA REUNIÃO DE AGORA
\"\"\"
{ata_md[:MAX_ATA_CHARS]}
\"\"\"

## TAREFA
Duas listas, em UM JSON:

1. `atualizacoes` — para cada item aberto acima que a ata MENCIONA: qual o novo estado.
2. `novos` — ações/compromissos que a reunião criou e que NÃO correspondem a nenhum item aberto acima.

Formato:

{{
  "atualizacoes": [
    {{
      "item_id": "<uuid exato da lista acima>",
      "action": "update_status" | "update_prazo" | "add_note" | "complete",
      "new_status": "concluido" | "em_andamento" | "pendente" | "cancelado" | null,
      "new_prazo": "YYYY-MM-DD" | null,
      "notes": "<o que a ata diz sobre este item, max 200 chars>" | null,
      "evidencia": "<trecho EXATO da ata que justifica, max 150 chars>",
      "confianca": "alta" | "media" | "baixa"
    }}
  ],
  "novos": [
    {{
      "area": "<Financeiro | Marketing | Operacional | RH | Comercial | Jurídico | Eq. Médica | TI/CRM | ...>",
      "acao": "<a ação, começando por verbo no infinitivo, max 150 chars>",
      "responsavel_r": "<quem EXECUTA, nome da ata>",
      "responsavel_a": "<quem RESPONDE pelo resultado>",
      "responsavel_c": "<quem é consultado>" | null,
      "responsavel_i": "<quem é informado>" | null,
      "prazo": "YYYY-MM-DD" | null,
      "evidencia": "<trecho EXATO da ata, max 150 chars>",
      "confianca": "alta" | "media" | "baixa"
    }}
  ]
}}

## REGRAS — a mais importante primeiro

1. **RETOMADA NÃO É ITEM NOVO.** Se a ata volta a um assunto que já está na lista
   de abertos, isso é `atualizacoes`, NUNCA `novos`. Mesmo que a ata use outras
   palavras, outro responsável ou outro prazo. Duplicar um compromisso que já
   existe é o defeito que esta tarefa existe para corrigir — na dúvida entre
   "novo" e "atualização de um item parecido", escolha ATUALIZAÇÃO.
2. **Item aberto que a ata não menciona: NÃO inclua em lugar nenhum.** Ele
   continua aberto por decisão do Renato; não invente conclusão nem cancelamento
   por ausência. Silêncio numa ata não é entrega.
2b. Item marcado **✅ JÁ CONCLUÍDO** existe no contexto só para você NÃO recriá-lo
   em `novos`. Não proponha mudança de status nele — trabalho entregue não volta
   a pendente porque a ata comentou o assunto. No máximo, uma nota.
3. `item_id` DEVE ser um UUID exato da lista. NUNCA invente id, nunca use número.
   Se não achar correspondência clara, não inclua a atualização.
4. `evidencia` é trecho LITERAL da ata. Se você não consegue citar a ata, a
   proposta não deveria existir.
5. **concluido** só quando a ata registra a entrega em si ("apresentado",
   "assinado", "enviado", "implementado", "decidido"). Discussão sobre o assunto,
   reafirmação do compromisso ou promessa de fazer NÃO fecham item —
   isso é `em_andamento`.
6. Item de REVISÃO/APROVAÇÃO/AVALIAÇÃO ("revisar", "aprovar", "validar"): só
   `concluido` se a ata comentar o DOCUMENTO/entregável em si. Na dúvida,
   confianca "media".
7. `novos` só para compromisso com responsável identificável. Tema discutido sem
   dono não é item de RACI — deixe de fora.
8. Prefira devolver menos e certo. Lista vazia é resposta válida nas duas.

Responda APENAS o JSON."""


def parse_llm_output(raw: str, valid_ids: set,
                     concluidos: Optional[set] = None) -> Dict[str, List[Dict[str, Any]]]:
    """Extrai/valida o JSON do LLM. Nunca levanta — devolve listas vazias.

    Filtra alucinação de id (o caminho de grupo já pegou o modelo inventando
    `item_id="13"`; a defesa é a mesma aqui e vale o dobro, porque uma proposta
    de ata carrega mais autoridade na hora de aprovar).

    `concluidos` = ids que já estão fechados e só entraram no contexto pra evitar
    recriação. Proposta que tente mexer no STATUS de um deles é derrubada aqui, e
    não só pedida no prompt: a aprovação humana passa `allow_downgrade=True`, ou
    seja, ninguém segura essa reabertura depois. Nota sobre item concluído passa.
    """
    concluidos = concluidos or set()
    out: Dict[str, List[Dict[str, Any]]] = {"atualizacoes": [], "novos": []}
    start, end = raw.find("{"), raw.rfind("}") + 1
    if start < 0 or end <= start:
        log.warning("raci_ata_reconciler: resposta sem JSON (%r)", raw[:120])
        return out
    try:
        data = json.loads(raw[start:end])
    except Exception as e:
        log.warning("raci_ata_reconciler: JSON invalido: %s", e)
        return out
    if not isinstance(data, dict):
        return out

    for p in data.get("atualizacoes") or []:
        if not isinstance(p, dict):
            continue
        iid = str(p.get("item_id") or "")
        if not UUID_RE.match(iid):
            log.info("raci_ata_reconciler: drop atualizacao item_id invalido %r", iid[:40])
            continue
        if iid not in valid_ids:
            log.info("raci_ata_reconciler: drop atualizacao item_id alucinado %s", iid)
            continue
        if not p.get("confianca") or not (p.get("evidencia") or "").strip():
            log.info("raci_ata_reconciler: drop atualizacao sem confianca/evidencia (%s)", iid)
            continue
        if iid in concluidos and (p.get("new_status") or p.get("new_prazo")):
            # Só a nota sobrevive; status/prazo de item entregue não se mexe por
            # menção em ata. Ver docstring.
            log.info("raci_ata_reconciler: item %s ja concluido — mantem so a nota", iid)
            p = {**p, "new_status": None, "new_prazo": None, "action": "add_note"}
            if not (p.get("notes") or "").strip():
                continue
        out["atualizacoes"].append(p)

    for n in data.get("novos") or []:
        if not isinstance(n, dict):
            continue
        acao = (n.get("acao") or "").strip()
        if not acao:
            continue
        if not (n.get("evidencia") or "").strip():
            log.info("raci_ata_reconciler: drop item novo sem evidencia (%r)", acao[:50])
            continue
        if not (n.get("responsavel_r") or "").strip():
            # Regra 7: compromisso sem dono não é item de RACI. Sem isto o RACI
            # enche de tema discutido, que é ruído com cara de pendência.
            log.info("raci_ata_reconciler: drop item novo sem responsavel_r (%r)", acao[:50])
            continue
        out["novos"].append(n)

    return out


# Palavras que aparecem em quase toda ação de RACI e não distinguem nada — se
# entrassem na conta, "enviar o relatório" e "enviar a proposta" pareceriam o
# mesmo item.
_STOPWORDS = {
    "de", "da", "do", "das", "dos", "e", "o", "a", "os", "as", "para", "pra",
    "com", "em", "no", "na", "nos", "nas", "por", "ao", "aos", "um", "uma",
    "que", "se", "sobre", "ate", "the", "of", "definir", "fazer", "realizar",
    "novo", "nova", "novos", "novas",
}


def _tokens(texto: str) -> set:
    from services.raci_smart_updates import _norm
    return {t for t in re.findall(r"[a-z0-9]{3,}", _norm(texto)) if t not in _STOPWORDS}


def duplicate_of_context(acao: str, itens: List[Dict[str, Any]],
                         threshold: float = 0.5) -> Optional[Dict[str, Any]]:
    """Item do contexto que o `acao` proposto provavelmente repete, ou None.

    Rede determinística por cima da regra 1 do prompt. O dry-run mostrou o
    modelo oscilando entre atualizar "Contratar recepção nova" e criar
    "Realizar entrevista com candidata para a recepção" — e, entre as duas
    leituras, a que empilha é sempre a pior. Jaccard sobre tokens significativos
    com corte alto: erra pro lado de deixar passar (duplicata escapa e o Renato
    descarta na revisão) em vez de engolir compromisso legítimo.
    """
    t_novo = _tokens(acao)
    if not t_novo:
        return None
    melhor, melhor_score = None, 0.0
    for it in itens:
        t_item = _tokens(it.get("acao") or "")
        if not t_item:
            continue
        inter = len(t_novo & t_item)
        if not inter:
            continue
        score = inter / len(t_novo | t_item)
        if score > melhor_score:
            melhor, melhor_score = it, score
    return melhor if melhor_score >= threshold else None


def resolve_prazo_novo(prazo_raw: Any, data_reuniao: date,
                       proxima_reuniao: Optional[date]) -> tuple:
    """(prazo, motivo) para item novo. `prazo` nunca é None — o ConselhoOS exige.

    O motivo vai junto na proposta pra distinguir data COMBINADA na reunião de
    data de CONVENÇÃO nossa.
    """
    if prazo_raw:
        try:
            y, m, d = str(prazo_raw)[:10].split("-")
            return date(int(y), int(m), int(d)), "prazo definido na ata"
        except Exception:
            log.info("raci_ata_reconciler: prazo invalido do LLM (%r) — usa convencao", prazo_raw)
    if proxima_reuniao:
        return proxima_reuniao, "prazo não definido na ata — usa a próxima reunião do conselho"
    return (data_reuniao + timedelta(days=PRAZO_FALLBACK_DAYS),
            f"prazo não definido na ata e sem próxima reunião agendada — usa +{PRAZO_FALLBACK_DAYS} dias")


# ─────────────────────────────────────────────────────────────────────────────
# Escrita (INTEL — fila de revisão)
# ─────────────────────────────────────────────────────────────────────────────
def existing_proposals_for(reuniao_id: str) -> int:
    """Quantas propostas esta reunião já gerou (idempotência). Conta só as que
    ainda 'valem': descartada não impede uma segunda tentativa depois que a ata
    foi regerada."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT count(*) AS n FROM raci_group_proposals
                WHERE reuniao_id = %s AND status IN ('pending_review','approved','applied')""",
            (reuniao_id,),
        )
        row = cur.fetchone()
    return int((row["n"] if isinstance(row, dict) else row[0]) or 0)


def _store_ata_proposal(*, reuniao_id: str, empresa_id: str, empresa_nome: str,
                        action: str, item_id: Optional[str], item_acao: str,
                        new_status: Optional[str], new_prazo: Any,
                        notes: Optional[str], evidencia: Optional[str],
                        confianca: Optional[str],
                        item_payload: Optional[Dict[str, Any]] = None) -> int:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO raci_group_proposals
                   (origem, reuniao_id, empresa_id, empresa_nome, item_id, item_acao,
                    action, new_status, new_prazo, notes, evidencia, confianca,
                    sender_name, item_payload)
               VALUES ('ata',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               RETURNING id""",
            (reuniao_id, empresa_id, empresa_nome, item_id, item_acao[:500],
             action, new_status, new_prazo, notes, evidencia, confianca,
             "ata da reunião", json.dumps(item_payload) if item_payload else None),
        )
        row = cur.fetchone()
    return row["id"] if isinstance(row, dict) else row[0]


# ─────────────────────────────────────────────────────────────────────────────
# Orquestração
# ─────────────────────────────────────────────────────────────────────────────
# Retry de sobrecarga. A reconciliação roda UMA vez por reunião, logo depois da
# ata — se ela cai num 529 ("Overloaded", que aconteceu no primeiro dry-run
# real), não há segundo tick que a repesque: a ata fica pronta e o RACI não é
# reconciliado, em silêncio. Mesma lição do retry do briefing (`c348618`), onde
# um 500 transiente custou um dia inteiro.
_RETRY_STATUS = (429, 500, 502, 503, 529)
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF_S = (5, 20)


async def _call_llm(prompt: str) -> str:
    import asyncio

    if not ANTHROPIC_API_KEY:
        raise AtaReconcileError("ANTHROPIC_API_KEY ausente")

    ultimo = ""
    for tentativa in range(_RETRY_ATTEMPTS):
        async with httpx.AsyncClient(timeout=120.0) as cli:
            r = await cli.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_API_KEY,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": llm.BALANCED, "max_tokens": 8000,
                      "messages": [{"role": "user", "content": prompt}]},
            )
        if r.status_code == 200:
            resp = r.json()
            llm_usage.record_response("raci.ata_reconcile", llm.BALANCED, resp)
            return resp["content"][0]["text"]

        ultimo = f"Anthropic API {r.status_code}: {r.text[:200]}"
        if r.status_code not in _RETRY_STATUS or tentativa == _RETRY_ATTEMPTS - 1:
            break
        espera = _RETRY_BACKOFF_S[min(tentativa, len(_RETRY_BACKOFF_S) - 1)]
        log.warning("raci_ata_reconciler: %s — retry em %ss (%s/%s)",
                    ultimo, espera, tentativa + 1, _RETRY_ATTEMPTS)
        await asyncio.sleep(espera)

    raise AtaReconcileError(ultimo)


async def reconcile_ata(
    reuniao_id: str,
    *,
    force: bool = False,
    dry_run: bool = False,
    call_llm: Optional[Callable[[str], Awaitable[str]]] = None,
) -> Dict[str, Any]:
    """Reconcilia o RACI da empresa contra a ata desta reunião.

    Não aplica nada: grava propostas em `raci_group_proposals` (pending_review).
    `dry_run=True` roda tudo e devolve o que faria, sem gravar.
    `force=True` re-propõe mesmo que a reunião já tenha propostas vivas (usar
    quando a ata foi regerada).
    """
    reuniao = load_reuniao(reuniao_id)
    empresa_id = str(reuniao["empresa_id"])
    empresa_nome = reuniao["empresa_nome"]
    data_reuniao = reuniao["data"]
    data_reuniao = data_reuniao.date() if hasattr(data_reuniao, "date") else data_reuniao

    ja = existing_proposals_for(reuniao_id)
    if ja and not force and not dry_run:
        log.info("raci_ata_reconciler: reuniao %s ja tem %s proposta(s) — skip", reuniao_id, ja)
        return {"skipped": "ja_reconciliada", "reuniao_id": reuniao_id,
                "propostas_existentes": ja, "empresa": empresa_nome}

    itens = open_items(empresa_id, reuniao_id)
    truncados = max(0, len(itens) - MAX_ITENS_CONTEXTO)
    itens_ctx = itens[:MAX_ITENS_CONTEXTO]
    if truncados:
        # Nunca silenciar truncagem: um teto invisível lê como "cobri tudo".
        log.warning("raci_ata_reconciler: %s itens abertos, contexto capado em %s (%s fora)",
                    len(itens), MAX_ITENS_CONTEXTO, truncados)

    prompt = build_prompt(empresa_nome, str(data_reuniao), reuniao["ata_md"] or "", itens_ctx)
    raw = await (call_llm or _call_llm)(prompt)
    concluidos = {str(it["id"]) for it in itens_ctx if it["status"] == "concluido"}
    parsed = parse_llm_output(raw, {str(it["id"]) for it in itens_ctx}, concluidos)

    # Rede determinística compartilhada com o caminho de grupo: derruba cortesia
    # e protege item de julgamento de ser fechado por menção lateral.
    acoes_by_id = {str(it["id"]): (it.get("acao") or "") for it in itens_ctx}
    atualizacoes = _apply_guardrails(parsed["atualizacoes"], acoes_by_id, reuniao["ata_md"] or "")

    # Segunda rede contra empilhamento: item "novo" que repete um item já
    # existente vira descarte reportado, não linha nova no RACI do cliente.
    novos, novos_dup = [], []
    for n in parsed["novos"]:
        dup = duplicate_of_context(n.get("acao") or "", itens_ctx)
        if dup:
            novos_dup.append({"acao": n.get("acao"), "parecido_com": dup.get("acao"),
                              "item_id": str(dup["id"])})
            log.info("raci_ata_reconciler: item novo %r descartado — parece o existente %r",
                     (n.get("acao") or "")[:60], (dup.get("acao") or "")[:60])
            continue
        novos.append(n)

    # "Não tratado" só faz sentido para item que já existia ANTES desta reunião:
    # um item cadastrado a partir da própria ata não deixou de ser tratado nela.
    mencionados = {str(p["item_id"]) for p in atualizacoes}
    nao_tratados = [
        {"item_id": str(it["id"]), "acao": it.get("acao"), "status": it["status"],
         "prazo": str(it.get("prazo")) if it.get("prazo") else None,
         "responsavel_r": it.get("responsavel_r"),
         "reuniao_data": str(it["reuniao_data"]) if it.get("reuniao_data") else None}
        for it in itens_ctx
        if str(it["id"]) not in mencionados and not it.get("desta_reuniao")
    ]

    proxima = next_meeting_date(empresa_id, data_reuniao)
    novos_prep = []
    for n in novos:
        prazo, motivo = resolve_prazo_novo(n.get("prazo"), data_reuniao, proxima)
        novos_prep.append({**n, "_prazo": prazo, "_prazo_motivo": motivo})

    result: Dict[str, Any] = {
        "reuniao_id": reuniao_id,
        "empresa": empresa_nome,
        "data_reuniao": str(data_reuniao),
        "abertos_no_contexto": len(itens),
        "abertos_anteriores": sum(1 for it in itens if not it.get("desta_reuniao")),
        "itens_truncados": truncados,
        "atualizacoes": len(atualizacoes),
        "novos": len(novos_prep),
        # Nunca silenciar o que foi podado: se a rede de duplicata começar a
        # comer item legítimo, é aqui que aparece.
        "novos_descartados_por_duplicata": novos_dup,
        "nao_tratados": nao_tratados,
        "dry_run": dry_run,
        "reproposta": bool(ja and force),
    }

    if dry_run:
        result["detalhe"] = {
            "atualizacoes": atualizacoes,
            "novos": [{k: (str(v) if k == "_prazo" else v) for k, v in n.items()} for n in novos_prep],
        }
        return result

    ids = []
    for p in atualizacoes:
        iid = str(p["item_id"])
        ids.append(_store_ata_proposal(
            reuniao_id=reuniao_id, empresa_id=empresa_id, empresa_nome=empresa_nome,
            action=p.get("action") or "update_status", item_id=iid,
            item_acao=acoes_by_id.get(iid, ""), new_status=p.get("new_status"),
            new_prazo=p.get("new_prazo"), notes=p.get("notes"),
            evidencia=p.get("evidencia"), confianca=p.get("confianca"),
        ))
    for n in novos_prep:
        payload = {k: n.get(k) for k in
                   ("area", "acao", "responsavel_r", "responsavel_a", "responsavel_c", "responsavel_i")}
        payload["prazo"] = str(n["_prazo"])
        payload["prazo_motivo"] = n["_prazo_motivo"]
        ids.append(_store_ata_proposal(
            reuniao_id=reuniao_id, empresa_id=empresa_id, empresa_nome=empresa_nome,
            action="create_item", item_id=None, item_acao=n.get("acao") or "",
            new_status="pendente", new_prazo=n["_prazo"],
            notes=n["_prazo_motivo"], evidencia=n.get("evidencia"),
            confianca=n.get("confianca"), item_payload=payload,
        ))

    result["proposal_ids"] = ids
    log.info("raci_ata_reconciler: %s — %s atualizacao(oes), %s novo(s), %s nao tratado(s)",
             empresa_nome, len(atualizacoes), len(novos_prep), len(nao_tratados))
    return result
