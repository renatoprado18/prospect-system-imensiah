#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""Recupera uma JANELA de WhatsApp varrendo a Evolution, não a lista de contatos.

POR QUE EXISTE, com o preço (15/10/2026). A instância `rap-whatsapp` caiu às
18h59 de 14/10 (`close`/403 — o Renato re-registrou o número no WhatsApp comum
e isso invalida todo dispositivo vinculado) e voltou às 11h16 de 15/10: 16,3h
sem ingestão. O polling de grupo recuperou 145 mensagens. **As DMs
recuperadas foram ZERO** — e não por não existirem: medido na Evolution,
**33 DMs da janela estavam lá**, e o INTEL não tinha nenhuma.

O `wa_catchup` não as alcança por DESENHO, e o desenho é razoável para o
problema dele: ele itera `_list_priority_contacts` (200 contatos, ordenados por
`ultimo_contato`) e para cada um chama `findMessages` com
`where={key:{remoteJid:<telefone>@s.whatsapp.net}}`, montado a partir de
`contacts.telefones`. Serve para o caso de origem — uma mensagem isolada que o
webhook perdeu de alguém conhecido (Felipe Orioli, 11/jun/26). Não serve para
uma QUEDA, porque numa queda o conjunto do que falta não é "mensagens de quem
eu já conheço": é **tudo que chegou na janela**, inclusive de quem não tem
ficha, de quem está fora dos 200 mais recentes e de quem tem o JID em formato
que o telefone não reconstrói.

A inversão é o ponto deste script: **pergunta-se à fonte o que ela tem na
janela** e confere-se contra o INTEL, em vez de perguntar "o que falta de cada
contato que eu listei". O denominador passa a ser o da fonte, não o da minha
lista ([[feedback_mecanismo_que_nao_mede_o_denominador]]).

🔴 O QUE ESTE SCRIPT DESCOBRIU, E QUE É A CAUSA-RAIZ DA PERDA DE DM. Rodado
sobre a janela de 14-15/10 (16,3h): a Evolution tinha **235** mensagens, o
INTEL já tinha 145, faltavam **90 (DM 33 · grupo 57)**. Os 90 replays
devolveram **0 linhas novas**, e o `webhook_audit` disse por quê, com o nome
que o próprio sistema dá:

    skipped | group_message    | 60   ← corretos: já estão em `group_messages`
    skipped | unresolvable_jid | 33   ← AS 33 DMs, todas
    skipped | unhandled_event  |  5
    processed | ok:outgoing    |  1

As 33 DMs vêm com `remoteJid = <N>@lid` e **`remoteJidAlt` VAZIO**. O
[[reference_whatsapp_lid_migration]] ensina que o telefone real vive em
`key.remoteJidAlt` — e isso vale para o **webhook ao vivo**, não para o
`findMessages`, que lê o banco da Evolution e não guarda o `alt`. Sem telefone
não há como resolver contato, e o handler descarta: `unresolvable_jid`.

**Consequência: DM em formato LID é IRRECUPERÁVEL por este caminho.** Ao vivo
ela entra (o webhook traz o `alt`); em replay, não. ⏭️ O conserto exige
resolver LID→telefone por outra via — provavelmente `/chat/findContacts` da
Evolution — e é frente própria, não emenda daqui.

⚠️ E cuidado com a régua que me enganou antes: contar `webhook_audit.remote_jid
LIKE '%@lid'` dava **1 JID em 30 dias**, o que me fez descartar LID como risco.
O audit só registra o que ENTROU; mensagem LID que não entra não aparece lá.
Medir o efeito do bug como se fosse a ausência do bug é o furo do denominador
([[feedback_mecanismo_que_nao_mede_o_denominador]]). Nesta janela havia 3 LIDs
distintos.

GRAVAÇÃO PELO MESMO CAMINHO DE SEMPRE. Reusa `wa_catchup._replay_through_webhook`,
que simula o payload e chama `handle_evolution_webhook` com `_catchup: True`
(⇒ `replay=True`). Não há INSERT próprio aqui de propósito: um segundo caminho
de gravação divergiria do primeiro em silêncio, e a telemetria em
`webhook_audit` — que é como se audita o que entrou — sairia diferente.

Uso:
    # mede, não grava (DEFAULT):
    DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 scripts/wa_recupera_janela.py \
        --de "2026-10-14 19:00" --ate "2026-10-15 11:16"

    # grava:
    ... --de ... --ate ... --executar

Datas em BRT (é como se lê um incidente), convertidas para epoch internamente.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.request
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

from database import get_db  # noqa: E402
from services.tz import BRT, UTC  # noqa: E402

PAGINAS_MAX = 40
POR_PAGINA = 100


def _evolution():
    base = (os.getenv("EVOLUTION_API_URL") or "").strip().strip('"').rstrip("/")
    key = (os.getenv("EVOLUTION_API_KEY") or "").strip().strip('"')
    inst = (os.getenv("EVOLUTION_INSTANCE") or "rap-whatsapp").strip()
    return base, key, inst


def varre_evolution(ini: int, fim: int):
    """Mensagens da Evolution com `messageTimestamp` dentro da janela.

    Varredura GLOBAL: sem `where`, porque filtrar por contato é exatamente o
    que faz o catchup perder quem não está na lista. Para em página vazia ou
    no teto — e o teto é anunciado no fim, nunca silencioso.
    """
    base, key, inst = _evolution()
    if not base or not key:
        print("🔴 EVOLUTION_API_URL/KEY ausentes — nada medido")
        return [], False

    achadas, paginas, truncou = [], 0, False
    for page in range(1, PAGINAS_MAX + 1):
        paginas = page
        req = urllib.request.Request(
            f"{base}/chat/findMessages/{inst}",
            data=json.dumps({"page": page, "offset": POR_PAGINA}).encode(),
            method="POST",
            headers={"apikey": key, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read())
        except Exception as e:
            print(f"   ⚠️ página {page} falhou ({e}) — parando a varredura")
            truncou = True
            break

        if isinstance(payload, list):
            registros = payload
        else:
            registros = (payload.get("messages") or {}).get("records") or []
        if not registros:
            break

        for m in registros:
            try:
                ts = int(m.get("messageTimestamp") or 0)
            except (TypeError, ValueError):
                continue
            if ini <= ts < fim:
                achadas.append(m)

        if page == PAGINAS_MAX:
            truncou = True

    print(f"   varri {paginas} página(s) de até {POR_PAGINA}")
    return achadas, truncou


# Tipos de nó que NUNCA viram linha em `messages`/`group_messages`, por
# natureza — não são perda e não se recuperam. Medido em 16/10/26 numa janela
# de 5 dias: **725 de 2.574 mensagens de grupo (28%)** eram destes tipos, com
# `reactionMessage` sozinho em 596. Contá-los como faltando foi o que fez um
# "258 perdidas" virar "1.116" e depois "690": três números, todos inflados
# pelo mesmo erro de denominador em camadas diferentes (escopo de grupo, tipo
# de nó). Perda só é perda contra o que geraria linha.
TIPOS_SEM_LINHA = {
    "reactionMessage",
    "pollUpdateMessage",
    "pollCreationMessageV3",
    "protocolMessage",
    "senderKeyDistributionMessage",
    "messageContextInfo",
    "secretEncryptedMessage",
}


def _tipo_de_no(registro) -> str:
    msg = registro.get("message") or {}
    for chave in msg.keys():
        if chave != "messageContextInfo":
            return chave
    return next(iter(msg.keys()), "(vazio)") if msg else "(vazio)"


def gera_linha(registro) -> bool:
    return _tipo_de_no(registro) not in TIPOS_SEM_LINHA and bool(registro.get("message"))


def grupos_sincronizados() -> set:
    """JIDs de grupo que o INTEL REALMENTE ingere.

    🔴 SEM ISTO O "FALTANDO" É FICÇÃO (medido 16/10/26). Uma varredura de
    09→14/10 acusou **1.116 faltando, 1.081 de grupo** — e a maior parte não
    era perda: `persist_group_message_realtime` só grava grupo com
    `social_groups_cache.sync_enabled = TRUE`, e são **49 de 127**. Os outros
    78 estão fora por DECISÃO. Replayar aquilo geraria 1.081 linhas de ruído
    em `webhook_audit` e zero gravação — o handler descartaria tudo.

    "Faltando" só quer dizer algo contra o que DEVERIA estar lá. Contar o que
    nunca foi para ser ingerido infla a perda e manda recuperar o que ninguém
    pediu — o furo do denominador na direção oposta à de sempre.
    """
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT group_jid FROM social_groups_cache WHERE sync_enabled IS TRUE"
        )
        return {r["group_jid"] for r in cur.fetchall() if r["group_jid"]}


def ja_no_intel(ids):
    """IDs já presentes no INTEL — nas DUAS tabelas, conforme o destino.

    🔴 A 1ª versão consultava só `messages.external_id` e reportou **202
    mensagens de grupo "faltando"** que estavam todas em `copilot.group_messages`
    (medidas: 145 na janela). Grupo NUNCA entra em `messages` — tem caminho
    próprio (`persist_group_message_realtime`). Executar sobre aquele veredito
    teria replayado 202 mensagens existentes: trabalho inútil, 202 linhas de
    ruído em `webhook_audit` e risco de redisparar automação de grupo (RACI,
    digest). Perguntar a UMA tabela sobre um dado que vive em DUAS é o furo do
    denominador outra vez — e aqui ele inflava o que falta, não o que sobra.
    """
    if not ids:
        return set()
    lista = list(ids)
    presentes = set()
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT external_id AS id FROM messages WHERE external_id = ANY(%s)",
            (lista,),
        )
        presentes |= {r["id"] for r in cur.fetchall()}
        cur.execute(
            "SELECT message_id AS id FROM copilot.group_messages WHERE message_id = ANY(%s)",
            (lista,),
        )
        presentes |= {r["id"] for r in cur.fetchall()}
    return presentes


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--de", required=True, help='início BRT, "YYYY-MM-DD HH:MM"')
    ap.add_argument("--ate", required=True, help='fim BRT, "YYYY-MM-DD HH:MM"')
    ap.add_argument("--executar", action="store_true",
                    help="grava (sem isto, só mede)")
    args = ap.parse_args()

    def _epoch(txt):
        return int(datetime.strptime(txt, "%Y-%m-%d %H:%M")
                   .replace(tzinfo=BRT).astimezone(UTC).timestamp())

    ini, fim = _epoch(args.de), _epoch(args.ate)
    if fim <= ini:
        print("🔴 --ate tem de ser depois de --de")
        return 1

    horas = (fim - ini) / 3600.0
    print(f"╔═ RECUPERAÇÃO DE JANELA — {args.de} → {args.ate} BRT ({horas:.1f}h) ═╗")
    print(f"  modo: {'EXECUTAR (grava)' if args.executar else 'MEDIR (não grava)'}")

    achadas, truncou = varre_evolution(ini, fim)
    if not achadas:
        print("  🟢 a Evolution não tem nada nesta janela — nada a recuperar")
        return 0

    por_id = {}
    for m in achadas:
        mid = (m.get("key") or {}).get("id")
        if mid:
            por_id[mid] = m

    existentes = ja_no_intel(por_id.keys())
    faltando = {k: v for k, v in por_id.items() if k not in existentes}

    def _tipo(m):
        return "grupo" if ((m.get("key") or {}).get("remoteJid") or "").endswith("@g.us") else "DM"

    # Separa PERDIDO de FORA DE ESCOPO: grupo sem `sync_enabled` nunca foi
    # para ser ingerido, e contá-lo como perda manda recuperar o que ninguém
    # pediu (ver `grupos_sincronizados`).
    sincronizados = grupos_sincronizados()

    def _no_escopo(m):
        jid = (m.get("key") or {}).get("remoteJid") or ""
        if not str(jid).endswith("@g.us"):
            return True                      # DM: sempre no escopo
        return str(jid) in sincronizados

    fora = {k: v for k, v in faltando.items() if not _no_escopo(v)}
    faltando = {k: v for k, v in faltando.items() if _no_escopo(v)}

    # 2ª camada do mesmo erro: reação, enquete e nó vazio não viram linha.
    sem_linha_por_tipo = {k: v for k, v in faltando.items() if not gera_linha(v)}
    faltando = {k: v for k, v in faltando.items() if gera_linha(v)}

    # 🔴 3ª camada: GRUPO NÃO GRAVA EM REPLAY. Medido em 16/10 — 145 replays
    # de grupo saíram todos `skipped | group_message`: o handler trata grupo
    # como decisão TERMINAL no caminho de replay. Grupo entra ao vivo
    # (`persist_group_message_realtime`) ou pelo pull `run-group-messages`, e
    # por mais nenhum. Deixar esses registros na fila de replay produziria
    # "162 processadas, 0 linhas" — número que parece falha e é desenho.
    grupo_so_por_pull = {k: v for k, v in faltando.items()
                         if str((v.get("key") or {}).get("remoteJid") or "").endswith("@g.us")}
    faltando = {k: v for k, v in faltando.items() if k not in grupo_so_por_pull}

    n_dm = sum(1 for m in faltando.values() if _tipo(m) == "DM")
    n_gr = len(faltando) - n_dm
    print(f"  na janela, a Evolution tem : {len(por_id)}")
    print(f"  o INTEL já tem             : {len(existentes)}")
    if fora:
        print(f"  fora de escopo (grupo s/ sync): {len(fora)}  ← não é perda, é decisão")
    if sem_linha_por_tipo:
        print(f"  tipo que não vira linha        : {len(sem_linha_por_tipo)}"
              f"  ← reação/enquete/vazio, não é perda")
    if grupo_so_por_pull:
        print(f"  grupo (só o PULL recupera)     : {len(grupo_so_por_pull)}"
              f"  ← replay não grava grupo")
        print("     ⏭️ rode: .venv/bin/python3 scripts/_grupo_ciclo.py")
    print(f"  FALTANDO recuperável AQUI  : {len(faltando)}  (DM {n_dm} · grupo {n_gr})")

    if not faltando:
        print("  🟢 nada a recuperar — a janela já está coberta")
        return 0

    # 🔴 RESOLVE O LID AQUI, ANTES do ramo dry-run — e isso é de propósito.
    # O `findMessages` devolve `<N>@lid` com `remoteJidAlt` VAZIO (ao vivo a
    # Evolution já resolve; na leitura do banco dela, não), e sem telefone o
    # handler descarta com `unresolvable_jid` — foi o que deu 0 linhas em 33
    # DMs nas duas quedas. O resolvedor cruza `message_id` com o que já entrou
    # ao vivo, onde o JID é o telefone, e preenche o campo que o handler já
    # sabe preferir ([[reference_whatsapp_lid_migration]]).
    #
    # ⚠️ Fica FORA do ramo `--executar` porque é read-only (findMessages +
    # SELECT) e porque, dentro dele, o dry-run deixava de informar exatamente
    # o que hoje decide a recuperação: quantas DMs têm dono. Dry-run que não
    # exercita o passo decisivo mede outra coisa — a classe de erro que já
    # custou uma frente inteira em 07/10.
    from services.wa_lid_resolver import injeta_alt

    lid_resolvidos = lid_sem_saida = 0
    with get_db() as _conn:
        for m in faltando.values():
            if "@lid" not in str((m.get("key") or {}).get("remoteJid") or ""):
                continue
            if injeta_alt(m, _conn):
                lid_resolvidos += 1
            else:
                lid_sem_saida += 1

    if lid_resolvidos or lid_sem_saida:
        print(f"\n  LID com dono resolvido: {lid_resolvidos} · sem saída: {lid_sem_saida}")
        if lid_sem_saida:
            print("     (sem saída = nenhuma mensagem daquele chat entrou ao vivo,")
            print("      ou o cruzamento ficou ambíguo. NÃO chuto o dono.)")

    if not args.executar:
        print("\n  Amostra do que entraria:")
        for m in list(faltando.values())[:8]:
            ts = datetime.fromtimestamp(int(m["messageTimestamp"]), UTC).astimezone(BRT)
            jid = (m.get("key") or {}).get("remoteJid", "?")
            corpo = json.dumps(m.get("message") or {}, ensure_ascii=False)[:58]
            print(f"   {ts:%d/%m %H:%M} | {_tipo(m):5} | {jid[:26]:26} | {corpo}")
        print("\n  ⚠️ MEDIÇÃO, nada gravado. Rode com --executar para recuperar.")
        if truncou:
            print("  ⚠️ a varredura foi TRUNCADA (teto de páginas ou erro):")
            print("     pode haver mais que isto na janela — não leia como total.")
        return 0

    from services.wa_catchup import _replay_through_webhook

    _, _, inst = _evolution()
    ok = erro = sem_linha = 0

    for mid, m in faltando.items():
        try:
            r = await _replay_through_webhook(m, inst)
        except Exception as e:
            erro += 1
            print(f"   ⚠️ {mid[:18]}: {e}")
            continue
        if isinstance(r, dict) and r.get("error"):
            erro += 1
        else:
            ok += 1

    # EFEITO, não contagem de chamadas: reconfere no banco quantas existem
    # agora. O handler pode processar e NÃO gerar linha (grupo, vazia,
    # revogada) — contar "replays bem-sucedidos" como "mensagens recuperadas"
    # é o erro de instrumento que já custou uma frente inteira em 07/10.
    agora_no_intel = ja_no_intel(por_id.keys())
    recuperadas = len(agora_no_intel) - len(existentes)
    sem_linha = ok - recuperadas if ok > recuperadas else 0

    print(f"\n  replays sem erro        : {ok}")
    print(f"  erros                   : {erro}")
    print(f"  ✅ LINHAS NOVAS medidas  : {recuperadas}")
    if sem_linha:
        print(f"  ⚠️ {sem_linha} processadas sem virar linha em `messages` —")
        print("     normal para grupo (grava em `group_messages`) e para vazia/revogada.")
    if truncou:
        print("  ⚠️ varredura TRUNCADA: rode de novo para o resto da janela.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
