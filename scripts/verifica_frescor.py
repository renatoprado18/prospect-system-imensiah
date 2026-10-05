#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""Verificador de FRESCOR das fontes — roda na ABERTURA da sessão (`/dev` e `/cos`).

POR QUE EXISTE, com a data e o preço. Em 28/09/26 às 20:47 UTC a ENTRADA do
WhatsApp parou: `webhook_audit`, as DMs em `messages` e `copilot.group_messages`
travaram todas no mesmo ponto. Ninguém foi avisado. A abertura de 29/09 leu os
boards, rodou os verificadores de modelo e de teto, tudo verde — e o board ainda
dizia *"a Evolution está VIVA (429 webhooks/6h)"*, frase verdadeira no dia em que
foi escrita e falsa na manhã em que foi lida. A sessão de 30/09 só descobriu as
**38 horas** de cegueira porque um recado manual na passagem mandava retestar o
host à mão. Recado manual não é alarme: depende de alguém lembrar.

O que este script acrescenta aos outros verificadores é a dimensão que faltava.
O `verifica_modelo` pergunta *"a ESTRUTURA bate com o contrato?"* e o
`verifica_boards`, *"o que eu li caberia numa leitura?"*. Nenhum dos dois olha se
o DADO ainda está chegando. Estrutura íntegra e board legível convivem
perfeitamente com uma fonte morta há dois dias, e é justamente essa combinação
que produz sessão confiante decidindo sobre retrato velho.

MEDIR O CANAL, NÃO A TABELA — a pegadinha que quase passou em 30/09. Naquela
manhã `messages` tinha 19 linhas frescas, a mais nova de 1,4h antes: pela tabela,
saudável. As 19 eram **todas `email`/`incoming`**; de WhatsApp, zero. Um medidor
que olhasse `max(criado_em)` da tabela certificaria frescor no exato dia em que o
WhatsApp estava mudo há 38h. Por isso cada fonte aqui é definida pelo CANAL
(join em `conversations.canal`), nunca pelo nome da tabela
([[feedback_medir_o_consumidor_certo]]).

E O E-MAIL É O CONTROLE POSITIVO, de propósito. Foi ele que, em 30/09, provou que
o silêncio do WhatsApp não era "dia fraco", nem alvo de banco errado, nem
medidor quebrado: uma fonte viva ao lado das mortas separa "a fonte X caiu" de
"eu estou olhando para o lugar errado". Sem controle positivo, todo alarme de
silêncio é ambíguo ([[feedback_controle_positivo_pega_o_furo_real]]).

HORAS ÚTEIS, NÃO HORAS CORRIDAS. O limiar de 3h em horas corridas gritaria todo
sábado de manhã — e alarme que grita no fim de semana é alarme que se aprende a
ignorar, que é como se perde o que importa na segunda. A idade das fontes de
conversa é contada só nas horas de dia útil (seg–sex, 8h–20h BRT): última
mensagem sexta 19h, aberto sábado 10h, dá 1h útil e fica quieto; a mesma pausa
atravessando segunda estoura.

FALHA DE MEDIÇÃO NÃO SAI VERDE. Foi assim que a coluna errada apareceu em 30/09:
`copilot.group_messages` não tem `criado_em` (é `timestamp`), e uma query
`except: pass` transformaria isso em "fresco". Aqui, fonte que não pôde ser
medida sai 🔴 com o erro e conta como estouro — um verificador que abstém
certifica conformidade que nunca checou
([[feedback_guarda_abstencao_vira_fabrica]], [[feedback_medidor_que_nao_mede_a_si_mesmo]]).

SÓ TEM SENTIDO CONTRA PROD. O banco local é snapshot do `./dev.sh sync`: medir
frescor nele afere a hora do último sync, não a saúde da ingestão, e daria
alarme todo dia por desenho. Com `DB_TARGET=local` o script diz isso e sai 0 sem
fingir que mediu.

Uso:  DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 ./verifica_frescor.py
      ... ./verifica_frescor.py --quiet    # só o que estourou (abertura)
"""
import argparse
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from database import get_connection  # noqa: E402
from services.tz import now_utc, to_brt, to_utc  # noqa: E402

# Janela de dia útil em BRT. Fora dela o relógio do alarme não anda.
HORA_INICIO, HORA_FIM = 8, 20

# Teto de horas a varrer ao contar horas úteis. Fonte parada há mais de um mês
# já estourou qualquer limiar — não há informação em continuar contando, e o cap
# impede que uma tabela vazia/antiga transforme a abertura em loop.
CAP_HORAS = 24 * 45

# Cada fonte é (rótulo, SQL devolvendo 1 timestamp, limiar, régua, nota).
# `uteis=True` conta só horas de dia útil; False conta horas corridas.
FONTES = [
    {
        "rotulo": "WA · webhooks (ingestão bruta)",
        "sql": "SELECT max(received_at) FROM webhook_audit",
        "limiar": 3,
        "uteis": True,
        "nota": "a porta de entrada da Evolution; para aqui, para tudo de WhatsApp",
    },
    {
        "rotulo": "WA · DMs",
        "sql": """SELECT max(m.criado_em) FROM messages m
                    JOIN conversations c ON c.id = m.conversation_id
                   WHERE c.canal ILIKE '%whats%'""",
        "limiar": 3,
        "uteis": True,
        "nota": "pelo CANAL, não pela tabela — `messages` fica fresca de e-mail com o WA mudo",
    },
    {
        "rotulo": "WA · grupos",
        "sql": "SELECT max(timestamp) FROM copilot.group_messages",
        "limiar": 3,
        "uteis": True,
        "nota": "a coluna é `timestamp`, não `criado_em` (erro real em 30/09)",
    },
    {
        "rotulo": "E-mail (CONTROLE POSITIVO)",
        "sql": """SELECT max(m.criado_em) FROM messages m
                    JOIN conversations c ON c.id = m.conversation_id
                   WHERE c.canal ILIKE '%mail%'""",
        "limiar": 12,
        "uteis": True,
        "nota": "fonte viva prova que o silêncio das outras é delas, não do alvo/medidor",
    },
    {
        "rotulo": "Tasks criadas",
        "sql": "SELECT max(data_criacao) FROM tasks",
        "limiar": 48,
        "uteis": False,
        "nota": "corridas, não úteis: registro nasce a qualquer hora",
    },
    {
        # 05/10/26 — a fonte que faltava, e o preço de não tê-la: a conta Anthropic
        # ficou SEM SALDO em 04/10 ~08h UTC e o sistema passou ~30h com TODA função
        # LLM parada (reconciler, triagem, visão, signal router, briefing). A
        # abertura daquela manhã e a de hoje passaram verdes em modelo, boards e
        # frescor — porque nenhuma das três olhava o subsistema mais caro. Eu só
        # descobri por acidente, quando uma chamada minha de validação voltou 400.
        #
        # ⚠️ CORRIDAS, NÃO ÚTEIS — e esta é a decisão que importa aqui. Medida em
        # horas úteis, a queda atual tem 3h (caiu num sábado) contra um máximo
        # histórico de 4,25h: a régua de dia útil NÃO consegue vê-la, e só acusaria
        # na segunda. Horas úteis existem porque conversa HUMANA pausa no fim de
        # semana; consumo de LLM é de MÁQUINA e o cron roda 24/7 — o reconciler das
        # 15h UTC foi perdido no sábado e no domingo igual. Aplicar a régua de gente
        # a um canal de máquina é o mesmo erro de categoria que medir a tabela em vez
        # do canal, uma camada acima.
        #
        # Limiar 12h, medido e não estimado: em 60 dias e 17.180 intervalos, o
        # p99 é 1,58h e o MÁXIMO 7,17h (as caudas são a janela morta 00h–05h UTC
        # entre crons). 12h fica acima do pior caso real com folga de 1,7× e teria
        # disparado em 04/10 por volta das 18h UTC — 10h de queda em vez de 30.
        "rotulo": "Chamadas LLM (o subsistema mais caro)",
        "sql": "SELECT max(ts) FROM tonia_llm_usage",
        "limiar": 12,
        "uteis": False,
        "nota": "silêncio aqui = reconciler, triagem, visão e briefing parados juntos",
    },
]

# Canários de ESTADO — não respondem "há quanto tempo não chega dado?", e sim "há
# um alarme ligado agora?". Ficam fora de FONTES de propósito: ali a lógica é
# "antigo = ruim", e aqui é o inverso — a presença de um registro RECENTE é que é
# a má notícia, então enfiá-los na mesma tabela inverteria o sinal.
#
# Por que DOIS detectores para a mesma queda, este e a fonte "Chamadas LLM" acima:
# eles falham por motivos diferentes. O flag é direto e imediato (diz "sem saldo",
# com a hora), mas depende do `anthropic-canary` estar de pé e do modo de falha
# ser um que ele reconheça — chave rotacionada, rate limit ou worker fora não
# levantam flag nenhum. O frescor é indireto e mais lento, e justamente por não
# depender do canário é o que sobra quando o próprio canário quebra
# ([[feedback_medidor_que_nao_mede_a_si_mesmo]]).
CANARIOS = [
    {
        "rotulo": "Anthropic — saldo/chave",
        "sql": """SELECT max(criado_em) FROM system_memories
                   WHERE titulo = 'anthropic_credit_down' AND tipo = 'credit_canary'""",
        "aceso": "SEM SALDO ou CHAVE INVÁLIDA — toda função LLM está caída",
        "nota": ("o `anthropic-canary` alerta UMA vez e depois só repete "
                 "`already_alerted` até recuperar; o flag é limpo na recuperação"),
    },
]


def _primeira_coluna(row):
    """Primeiro valor da linha, seja o cursor de tupla ou de dicionário.

    `get_connection()` devolve `RealDictCursor`: ali `row[0]` levanta KeyError: 0,
    e o `except` do chamador transformaria isso em "NÃO MEDIDO" nas cinco fontes
    de uma vez — foi exatamente o que aconteceu na 1ª execução contra prod.
    O alarme apontou para si mesmo em vez de certificar frescor, que é o desenho
    querido; esta função só lhe tira o motivo de estar certo.
    """
    if row is None:
        return None
    return next(iter(row.values())) if hasattr(row, "values") else row[0]


def horas_uteis(desde: datetime, ate: datetime) -> float:
    """Horas entre os dois instantes que caem em dia útil BRT, 8h–20h.

    Conta por fatias horárias em vez de fórmula fechada: é o cálculo que dá para
    ler em voz alta e conferir num caso concreto ("sexta 19h → sábado 10h = 1h"),
    e a precisão de hora basta para um limiar de 3h.
    """
    if ate <= desde:
        return 0.0
    ini, fim = to_brt(desde), to_brt(ate)
    total, cursor, gasto = 0.0, ini, 0
    while cursor < fim and gasto < CAP_HORAS:
        prox = min(cursor + timedelta(hours=1), fim)
        if cursor.weekday() < 5 and HORA_INICIO <= cursor.hour < HORA_FIM:
            total += (prox - cursor).total_seconds() / 3600
        cursor, gasto = prox, gasto + 1
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true", help="só imprime o que estourou")
    a = ap.parse_args()

    alvo = (os.getenv("DB_TARGET") or "").strip().lower()
    if alvo != "prod":
        # Não é falha: é escopo. Dizer alto, porque "rodou e passou" contra o
        # local seria a mesma certificação vazia que este script existe para evitar.
        print("╔═ FRESCOR DAS FONTES ═╗")
        print(f"  ⚪ NÃO MEDIDO — DB_TARGET={alvo or '(vazio)'}. Frescor só tem sentido contra prod:")
        print("     o banco local é snapshot do `./dev.sh sync` e mediria a hora do sync.")
        print("     Rode: DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 scripts/verifica_frescor.py")
        return 0

    try:
        conn = get_connection()
    except Exception as e:
        print(f"╔═ FRESCOR DAS FONTES ═╗\n  🔴 SEM CONEXÃO — nada foi medido: {e}")
        return 1

    agora = now_utc()
    linhas, estouros, nao_medidas = [], [], []
    cur = conn.cursor()
    for f in FONTES:
        try:
            cur.execute(f["sql"])
            ultimo = _primeira_coluna(cur.fetchone())
        except Exception as e:
            conn.rollback()
            nao_medidas.append((f["rotulo"], str(e).strip().splitlines()[0]))
            continue

        if ultimo is None:
            nao_medidas.append((f["rotulo"], "fonte vazia — nenhum registro"))
            continue

        # Colunas TIMESTAMP voltam naive do psycopg2; `to_utc` as trata como UTC,
        # que é a convenção de storage do INTEL. Subtrair naive de aware levantaria
        # TypeError — e no `except` acima isso viraria "não medido" por um detalhe
        # de tipo, escondendo uma fonte que estava só a um cast de ser medida.
        ultimo = to_utc(ultimo)
        idade = (horas_uteis(ultimo, agora) if f["uteis"]
                 else (agora - ultimo).total_seconds() / 3600)
        regua = "úteis" if f["uteis"] else "corridas"
        estourou = idade > f["limiar"]
        linhas.append((estourou, f["rotulo"], ultimo, idade, regua, f["limiar"], f["nota"]))
        if estourou:
            estouros.append(f["rotulo"])

    # Canários de estado. Mesma regra dos outros: não poder medir NÃO sai verde —
    # um canário que abstém certifica saúde que nunca checou.
    acesos, canarios_nao_medidos = [], []
    for c in CANARIOS:
        try:
            cur.execute(c["sql"])
            desde = _primeira_coluna(cur.fetchone())
        except Exception as e:
            conn.rollback()
            canarios_nao_medidos.append((c["rotulo"], str(e).strip().splitlines()[0]))
            continue
        if desde is not None:
            acesos.append((c["rotulo"], to_utc(desde), c["aceso"], c["nota"]))

    conn.close()

    if a.quiet and not estouros and not nao_medidas and not acesos and not canarios_nao_medidos:
        return 0

    print("╔═ FRESCOR DAS FONTES (o dado ainda está chegando?) ═╗")
    for estourou, rotulo, ultimo, idade, regua, limiar, nota in linhas:
        if a.quiet and not estourou:
            continue
        icone = "🔴" if estourou else "🟢"
        print(f"  {icone} {rotulo}: último {to_brt(ultimo):%d/%m %H:%M} BRT "
              f"— {idade:.1f}h {regua} (limiar {limiar}h)")
        if estourou:
            print(f"      ↳ {nota}")
    for rotulo, erro in nao_medidas:
        print(f"  🔴 {rotulo}: NÃO MEDIDO — {erro}")
        print("      ↳ não medir não é estar fresco; consertar a query antes de confiar na abertura")

    # O FLAG ATRASA A RECUPERAÇÃO — descoberto 05/10, uma hora depois de armar
    # isto. O saldo foi recarregado ~15h UTC e as chamadas voltaram no mesmo
    # minuto (`task_reconciler.judge`, `worker.pdf_analyze`); mas o canário roda
    # de hora em hora (:33) e só limpa o flag quando RODA, então por até 1h a
    # abertura dizia "toda função LLM está caída" com o sistema já funcionando.
    #
    # É o caso em que ter DOIS detectores paga na direção oposta à prevista: eu
    # os pus para que um cobrisse a falha do outro na QUEDA, e o que apareceu
    # primeiro foi a divergência na VOLTA. Aqui o veredito é do frescor, não do
    # flag — dado fresco é evidência de agora, flag é evidência de quando foi
    # escrito. Um alarme que grita depois de resolvido é como se aprende a
    # ignorar alarme ([[feedback_medidor_que_nao_mede_a_si_mesmo]]).
    _llm_fresco = any(
        (not estourou) and "Chamadas LLM" in rotulo
        for estourou, rotulo, *_ in linhas
    )
    for rotulo, desde, aceso, nota in acesos:
        idade = (agora - desde).total_seconds() / 3600
        if _llm_fresco:
            print(f"  🟡 CANÁRIO ACESO MAS PROVAVELMENTE OBSOLETO · {rotulo}")
            print(f"      ↳ o flag está de pé desde {to_brt(desde):%d/%m %H:%M} BRT "
                  f"({idade:.0f}h), MAS há chamada de LLM recente — ou seja, voltou.")
            print("      ↳ o canário roda 1×/h (:33) e só limpa o flag quando roda; "
                  "confirme na próxima rodada antes de avisar queda.")
            continue
        print(f"  🔴 CANÁRIO ACESO · {rotulo}: {aceso}")
        print(f"      ↳ desde {to_brt(desde):%d/%m %H:%M} BRT ({idade:.0f}h) — {nota}")
    for rotulo, erro in canarios_nao_medidos:
        print(f"  🔴 CANÁRIO NÃO MEDIDO · {rotulo} — {erro}")
        print("      ↳ canário que não pôde ser lido não é canário apagado")

    if not estouros and not nao_medidas and not acesos and not canarios_nao_medidos:
        print("  🟢 todas as fontes dentro do limiar e nenhum canário aceso.")
        return 0

    if estouros or nao_medidas:
        print("\n  ⚠️ FONTE PARADA É CEGUEIRA SEM RASTRO. Em 28/09/26 a entrada do WhatsApp")
        print("  morreu às 17:47 BRT e a abertura do dia seguinte não disse nada: 38h de")
        print("  decisão sobre retrato velho. ANTES de propor frente, descubra se é queda")
        print("  (host fora, cron parado) ou desligamento deliberado que ninguém registrou.")
    if any("CONTROLE POSITIVO" in r for r in estouros):
        print("  🔎 O CONTROLE POSITIVO também estourou ⇒ suspeite do ALVO ou do medidor,")
        print("     não de uma fonte só: é improvável que tudo caia junto por coincidência.")
    if acesos and not _llm_fresco:
        # Canário aceso pede recado, não investigação: a causa já está nomeada e a
        # ação é de FORA do código. Em 04/10 o alerta de WhatsApp saiu e o Renato
        # foi avisado às 08:33 — mesmo assim passaram 30h, porque o aviso chegou
        # uma vez, no meio de uma viagem, e nada mais o repetiu. A abertura é o
        # lugar onde ele reaparece ([[feedback_superficie_nova_mata_o_aviso]]).
        print("\n  ⚠️ CANÁRIO ACESO É ESTADO CONHECIDO, NÃO INVESTIGAÇÃO: a causa já está")
        print("  nomeada e a ação é fora do código (recarregar saldo, trocar chave). Diga")
        print("  ao Renato ANTES de propor frente — frente que dependa de LLM não sai do")
        print("  chão enquanto isso, e estimar custo/prazo sobre ela é estimar no vazio.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
