"""
Group Digest Service — o que os grupos de WhatsApp produziram que interessa ao Renato.

REESCRITO EM 29/07/2026, a pedido dele: "esse digest está gerando zero de valor.
Poderia ser bem mais resumido e somente o que realmente pode me interessar."

O QUE ESTAVA ERRADO (medido no digest de 28/07, 6.450 chars, 15 grupos)
----------------------------------------------------------------------
  1. **Ordenava por VOLUME**, e volume é inversamente proporcional a relevância.
     No topo: ACFICTOR (45 msgs, especulação sobre a fraude), Alumni CELINT (43,
     aniversário), CHARUTO CAP (32, vídeos musicais). O **Conselho Vallen, com 6
     mensagens, era o único que tocava uma frente ativa** — e aparecia depois de
     ~5.000 caracteres.
  2. **Não filtrava nada.** Sete dos quinze blocos diziam, com todas as letras,
     "Menção a Renato: Nenhuma" — o resumo declarava a própria irrelevância e
     ocupava espaço do mesmo jeito. Tortinhas de frango do condomínio e
     indicação de gráfica entravam.
  3. **3-4 linhas por grupo**, sem teto. Estourava os ~4 KB do Web Push — e é
     por isso que o teto diário de interrupção nunca segurava o digest: ele
     falhava no push e a escada o devolvia inteiro pro WhatsApp.
     (⚠️ Correção 29/07: eu também afirmei que ele passava do corte de 4.096 da
     Evolution e chegava truncado no WhatsApp. **Não chegava** — medido contra a
     API dela, que devolve o entregue: digests de 6.463 e 6.017 chars estão
     inteiros, terminando em frase completa. O limite do Web Push é real e
     medido; o da Evolution era convenção nossa tratada como fato.)

A RÉGUA NOVA
------------
Grupo vinculado a projeto (`project_whatsapp_groups`) é **frente de trabalho** —
11 dos 49 com sync. Ali, movimento é notícia. Grupo não vinculado é convívio:
só entra se alguém **pediu algo ao Renato**, marcou compromisso que o envolve,
decidiu algo que o afeta, ou tocou em dinheiro/contrato/prazo dele. O critério é
explícito no prompt e o modelo devolve a decisão em JSON, item a item.

Sem nada relevante, **não sai digest nenhum** — silêncio é resposta válida, e um
digest que chega todo dia dizendo "nada aqui" treina a ignorar o canal.

Teto duro de 1.500 chars, para caber no push — assim o rebaixamento do teto
diário finalmente funciona para ele.
"""
import os
from services import llm
from services import llm_usage
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List
import httpx

from database import get_db
from services.tz import now_utc, to_brt

logger = logging.getLogger(__name__)

# Cabe no Web Push (~4 KB de payload cifrado, e o payload leva título + JSON além
# do corpo). O digest velho tinha 6.450 e por isso nunca cabia no push.
MAX_DIGEST_CHARS = 1500

# Uma linha por grupo. Se o fato não cabe aqui, ele não é um fato — é um resumo.
MAX_LINHA_CHARS = 180
MAX_ACAO_CHARS = 90
# Nome de grupo é etiqueta, não conteúdo: "ACFICTOR - Canal Interativo:
# Perguntas e Respostas" comia metade da linha no primeiro ensaio.
MAX_NOME_GRUPO = 34

# Verbos de espera. O primeiro ensaio devolveu "Aguardar respostas do grupo" e
# "Analisar implicações legais" como coisas a fazer — a primeira não é ação
# nenhuma, a segunda foi o modelo inventando tarefa que ninguém pediu. Como
# `acao_sua` é o que decide entre interromper no WhatsApp (8) e push (6), deixar
# passar transforma o digest inteiro em interrupção outra vez.
_NAO_E_ACAO = (
    "aguardar", "acompanhar", "monitorar", "ficar atento", "ficar de olho",
    "observar", "esperar", "analisar implicac", "avaliar impacto", "considerar",
)

# Abaixo disso não há conversa, há ruído solto.
MIN_MSGS_PARA_RESUMIR = 3


async def generate_daily_group_digests(days: int = 1, dry_run: bool = False) -> Dict:
    """Varre os grupos com sync, guarda só o que interessa e notifica — se houver.

    `dry_run=True` faz todo o trabalho e devolve o texto sem notificar nada.
    """
    results = {"groups_processed": 0, "relevantes": 0, "digests_sent": 0, "errors": []}

    with get_db() as conn:
        cursor = conn.cursor()
        # O LEFT JOIN é a régua: `projeto` preenchido = frente de trabalho.
        cursor.execute("""
            SELECT sg.group_jid, sg.group_name, p.nome AS projeto
              FROM social_groups_cache sg
         LEFT JOIN project_whatsapp_groups pwg
                ON pwg.group_jid = sg.group_jid AND pwg.ativo = TRUE
         LEFT JOIN projects p ON p.id = pwg.project_id
             WHERE sg.sync_enabled = TRUE AND sg.group_name IS NOT NULL
        """)
        groups = [dict(r) for r in cursor.fetchall()]

    if not groups:
        return {"skipped": "no groups with sync enabled"}

    if not dry_run:
        # Pulado no dry-run de propósito: o sync ESCREVE em group_messages, e um
        # ensaio não deve mexer no banco. O webhook já grava em tempo real, então
        # o dry-run lê dado atual mesmo sem ele.
        try:
            from services.group_message_sync import sync_group_messages
            sync_result = await sync_group_messages(limit_per_group=100)
            logger.info(f"Group sync: {sync_result}")
        except Exception as e:
            logger.error(f"Group sync error: {e}")

    avaliados: List[Dict] = []
    for group in groups:
        try:
            item = await _avaliar_grupo(group['group_jid'], group['group_name'],
                                        group.get('projeto'), days)
            if item:
                avaliados.append(item)
                results["groups_processed"] += 1
        except Exception as e:
            results["errors"].append(f"{group['group_name']}: {e}")

    relevantes = [d for d in avaliados if d.get("relevante")]
    results["relevantes"] = len(relevantes)

    if not relevantes:
        # Silêncio deliberado. Um digest diário de "nada aqui" é o que ensina a
        # ignorar o canal — e foi metade do problema do digest velho.
        logger.info("group_digest: %s grupos avaliados, nenhum relevante — nao notifica",
                    len(avaliados))
        results["skipped"] = "nada relevante"
        return results

    wa_text, cortados = _format_digest(relevantes, avaliados)
    results["chars"] = len(wa_text)
    if cortados:
        results["cortados_por_tamanho"] = cortados

    if dry_run:
        results["preview"] = wa_text
        return results

    try:
        from services.notification_router import notify
        # Graduação por CONTEÚDO, que é o que o roteador existe pra fazer: se
        # algum item espera algo do Renato, isso interrompe (8); se é só
        # movimento das frentes, vira push (6). O digest velho mandava 8 sempre.
        tem_acao = any(d.get("acao_sua") for d in relevantes)
        urgencia = 8 if tem_acao else 6
        await notify(
            "group_digest",
            "Grupos — o que pede você" if tem_acao else "Grupos — movimento das frentes",
            wa_text,
            urgencia,
            msg_type="group_digest",
            dedup=f"group_digest:{to_brt(now_utc()).strftime('%Y%m%d')}",
        )
        results["digests_sent"] = 1
        results["urgencia"] = urgencia
    except Exception as e:
        results["errors"].append(f"WhatsApp send: {e}")

    return results


async def _avaliar_grupo(group_jid: str, group_name: str, projeto: str | None,
                         days: int = 1) -> Dict | None:
    """Lê as mensagens do dia e decide se há algo que interessa ao Renato.

    Devolve None quando o grupo não teve conversa suficiente; devolve
    `{"relevante": False}` quando teve conversa mas nada que valha o espaço dele
    (esse caso é contado, pra que o digest possa dizer quantos grupos varreu).
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT sender_name, content, message_type, timestamp, from_me
            FROM group_messages
            WHERE group_jid = %s AND timestamp >= CURRENT_DATE - INTERVAL '%s days'
            ORDER BY timestamp ASC
        """, (group_jid, days))
        messages = [dict(r) for r in cursor.fetchall()]

    if not messages or len(messages) < MIN_MSGS_PARA_RESUMIR:
        return None

    msgs_text = "\n".join([
        f"[{m['timestamp'].strftime('%H:%M') if hasattr(m['timestamp'], 'strftime') else '?'}] "
        f"{'Renato' if m['from_me'] else (m['sender_name'] or '?')}: "
        f"{m['content'][:300]}"
        for m in messages
    ])
    if len(msgs_text) > 4000:
        msgs_text = msgs_text[:4000] + "\n... (truncado)"

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return {"group": group_name, "projeto": projeto, "messages": len(messages),
                "relevante": False, "erro": "sem ANTHROPIC_API_KEY"}

    prompt = _build_prompt(group_name, projeto, msgs_text, len(messages))

    # Grupo de FRENTE ganha o modelo forte; convívio fica no barato. O Haiku
    # oscilou justamente onde custa: numa passada resumiu o Conselho Vallen como
    # "Thalita pediu a lista de VIPs, Lara analisa hoje" (útil) e na seguinte
    # como "Renato compartilhou documentos" (inócuo). São ~11 grupos de frente
    # contra ~38 sociais, então o gasto extra é pequeno e está onde importa.
    modelo = llm.BALANCED if projeto else llm.FAST

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": modelo, "max_tokens": 400,
                      "messages": [{"role": "user", "content": prompt}]}
            )
        if resp.status_code == 200:
            _llm_resp = resp.json()
            llm_usage.record_response("group_digest.summary", modelo, _llm_resp)
            parsed = _parse_avaliacao(_llm_resp["content"][0]["text"])
            return {"group": group_name, "projeto": projeto,
                    "messages": len(messages), **parsed}
        logger.warning("group_digest: API %s para %s", resp.status_code, group_name)
    except Exception as e:
        logger.error(f"Digest AI error for {group_name}: {e}")

    # Falha de IA não vira linha no digest: um "resumo indisponível" ocupando
    # espaço é exatamente o tipo de ruído que esta reescrita foi feita pra tirar.
    return {"group": group_name, "projeto": projeto, "messages": len(messages),
            "relevante": False, "erro": "resumo indisponivel"}


def _build_prompt(group_name: str, projeto: str | None, msgs_text: str, n: int) -> str:
    if projeto:
        contexto = (
            f"Este grupo é a FRENTE DE TRABALHO do projeto **{projeto}**. "
            "Aqui, movimento da frente é notícia: entrega, decisão, prazo, "
            "impedimento, número, documento, mudança de plano."
        )
        criterio = (
            "Marque relevante=true se houve QUALQUER movimento concreto na frente. "
            "Marque false se foi só conversa social, confirmação de presença, "
            "agradecimento ou repasse de link sem discussão."
        )
    else:
        contexto = (
            "Este grupo NÃO é frente de trabalho do Renato — é convívio "
            "(família, clube, associação, alumni, notícias)."
        )
        criterio = (
            "Marque relevante=true SOMENTE se: (a) alguém pediu/perguntou algo "
            "diretamente ao Renato e ainda não foi respondido; (b) foi marcado "
            "compromisso com DATA que o envolve; (c) foi decidido algo que o "
            "afeta; ou (d) o assunto tocou dinheiro/contrato/prazo dele. "
            "Caso contrário, relevante=false — mesmo que a conversa tenha sido "
            "longa ou animada. Aniversário, vídeo, notícia, indicação de "
            "fornecedor, almoço que ele já recusou: relevante=false."
        )

    return f"""Você filtra o que chega ao Renato. O tempo dele é o recurso escasso; sua função é PROTEGER esse tempo, não resumir conversa.

GRUPO: {group_name}
{contexto}

MENSAGENS DE HOJE ({n}):
{msgs_text}

CRITÉRIO:
{criterio}

Responda APENAS este JSON:

{{
  "relevante": true | false,
  "motivo": "frente" | "pedido" | "compromisso" | "decisao" | "financeiro" | "nada",
  "linha": "<o FATO, em no máximo {MAX_LINHA_CHARS} caracteres>",
  "quem_espera": "<NOME da pessoa que está esperando algo do Renato>" | null,
  "acao_sua": "<o que essa pessoa espera, em até {MAX_ACAO_CHARS} chars>" | null
}}

REGRAS:
- `linha` é o FATO, não o tema. "Thalita pediu a lista de VIPs; Lara tem os dados e analisa hoje" serve. "Discussão sobre o clube VIP" não serve — isso é assunto, não notícia.
- Nomeie quem fez o quê. Sem nome, o Renato não sabe com quem falar.
- `acao_sua` é só quando uma PESSOA espera algo dele — alguém perguntou, pediu, delegou ou depende de uma resposta dele. NÃO é onde você sugere o que ele deveria fazer: "analisar as implicações", "avaliar o impacto", "considerar a estratégia" são tarefas que VOCÊ inventou, e cada uma delas custa uma interrupção. Se ninguém está esperando nada dele, use null.
- "Aguardar", "acompanhar", "monitorar" e "ficar atento" NÃO são ação — quem aguarda não faz nada. Nesses casos, null.
- `quem_espera` é o teste: se você não consegue NOMEAR a pessoa que está esperando, então ninguém está esperando, e os dois campos vão null. Não escreva "o grupo", "o conselho" nem "a equipe" — nome de gente.
- Atenção à DIREÇÃO: `quem_espera` é quem está parado esperando o Renato agir. Se é o Renato que já perguntou/respondeu e está esperando a resposta de outra pessoa, então ele não tem nada a fazer — os dois campos vão null. Esperar não é tarefa dele.
- Na dúvida entre relevante e irrelevante, escolha **irrelevante**. Um item a menos custa pouco; um digest que ele para de ler custa tudo.
- Nunca escreva "nenhuma menção a Renato" ou "sem pendências" — isso é relevante=false, não texto.

Responda só o JSON."""


def _parse_avaliacao(raw: str) -> Dict:
    """Extrai o veredito. Qualquer falha vira irrelevante — na dúvida, silêncio."""
    start, end = raw.find("{"), raw.rfind("}") + 1
    if start < 0 or end <= start:
        return {"relevante": False, "erro": "resposta sem JSON"}
    try:
        d = json.loads(raw[start:end])
    except Exception:
        return {"relevante": False, "erro": "JSON invalido"}
    if not isinstance(d, dict) or not d.get("relevante"):
        return {"relevante": False, "motivo": (d or {}).get("motivo") if isinstance(d, dict) else None}
    linha = (d.get("linha") or "").strip()
    if not linha:
        # "Relevante" sem fato é o mesmo que irrelevante.
        return {"relevante": False, "erro": "relevante sem linha"}
    return {"relevante": True, "motivo": d.get("motivo"),
            "linha": _corta(linha, MAX_LINHA_CHARS),
            "quem_espera": (d.get("quem_espera") or "").strip() or None,
            "acao_sua": _limpa_acao(d.get("acao_sua"), d.get("quem_espera"))}


def _corta(texto: str, limite: int) -> str:
    """Corta na palavra, não no meio dela. O primeiro ensaio terminou uma linha
    em 'estratégia de recuperaç', que faz o leitor parar pra decifrar."""
    texto = (texto or "").strip()
    if len(texto) <= limite:
        return texto
    cortado = texto[:limite].rsplit(" ", 1)[0].rstrip(" ,;:.")
    return (cortado or texto[:limite]) + "…"


# Coletivos não esperam nada — pessoas esperam. Sem um nome, "o grupo aguarda
# seu retorno" é o modelo preenchendo campo, não alguém cobrando o Renato.
_NAO_E_PESSOA = ("o grupo", "grupo", "o conselho", "conselho", "a equipe",
                 "equipe", "todos", "os membros", "a diretoria", "ninguem", "n/a")


def _limpa_acao(acao: str | None, quem: str | None) -> str | None:
    """Só sobrevive ação que uma PESSOA NOMEADA espera dele.

    Dois filtros, porque o ensaio furou os dois jeitos: o verbo de espera
    ("Aguardar respostas do grupo") e a tarefa inventada ("Avaliar implicações
    legais da ordem judicial" — ninguém pediu isso). Exigir o nome de quem
    espera mata a segunda classe na raiz: o modelo não consegue nomear alguém
    para uma tarefa que ele mesmo criou.
    """
    from services.raci_smart_updates import _norm

    acao = (acao or "").strip()
    if not acao:
        return None

    quem_norm = _norm(quem or "")
    if not quem_norm or quem_norm in _NAO_E_PESSOA:
        logger.info("group_digest: descarta acao_sua sem pessoa que espera (%r / quem=%r)",
                    acao[:60], quem)
        return None

    acao_norm = _norm(acao)
    if any(acao_norm.startswith(v) for v in _NAO_E_ACAO):
        logger.info("group_digest: descarta acao_sua nao-acionavel (%r)", acao[:60])
        return None

    # Direção invertida: "Renato perguntou X — aguarda resposta dela" põe o
    # Renato na ponta que espera, e quem espera não tem tarefa. Custa perder um
    # caso raro ("responder o João, que aguarda desde ontem") — mas esse fato
    # continua aparecendo na linha; só não marca interrupção.
    if "aguarda" in acao_norm or "aguardando" in acao_norm:
        logger.info("group_digest: descarta acao_sua de espera (%r)", acao[:60])
        return None
    return _corta(acao, MAX_ACAO_CHARS)


def _format_digest(relevantes: List[Dict], avaliados: List[Dict]) -> tuple:
    """Monta o texto e devolve (texto, quantos ficaram de fora por tamanho).

    Ordem: o que pede algo do Renato primeiro, depois frente de trabalho, depois
    o resto. Volume de mensagens NÃO entra na ordenação — foi o que jogou o
    Conselho Vallen (6 msgs) para o fim de um texto de 6.450 caracteres.
    """
    def rank(d: Dict) -> tuple:
        return (0 if d.get("acao_sua") else 1, 0 if d.get("projeto") else 1,
                (d.get("group") or "").lower())

    ordenados = sorted(relevantes, key=rank)
    total_varridos = len(avaliados)

    cabecalho = f"📱 *Grupos* — {len(ordenados)} de {total_varridos} com algo pra você\n"
    linhas, usados, cortados = [], len(cabecalho), 0

    for d in ordenados:
        bloco = f"\n*{_corta(d['group'], MAX_NOME_GRUPO)}*"
        if d.get("projeto"):
            bloco += f" _{_corta(d['projeto'], MAX_NOME_GRUPO)}_"
        bloco += f"\n{d['linha']}\n"
        if d.get("acao_sua"):
            bloco += f"↳ você: {d['acao_sua']}\n"
        if usados + len(bloco) > MAX_DIGEST_CHARS:
            cortados += 1
            continue
        linhas.append(bloco)
        usados += len(bloco)

    texto = cabecalho + "".join(linhas)
    if cortados:
        # Truncar em silêncio lê como "era só isso".
        texto += f"\n(+{cortados} não coube — veja nos grupos)"
    return texto, cortados
