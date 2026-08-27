#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""Placar das chamadas de voz do WhatsApp — o que o evento `call` está trazendo.

POR QUE EXISTE (24/08/2026, pedido da CoS). Em 24/08 duas ligações destravaram o
dia — 26 min com Jonas Fagundes e Daniel Guidolin (Premix, 14h00–14h26) e 4 min
com o Piccino às 18h35 — e **não há uma linha de nenhuma das duas em `messages`**.
A `project_notes` virou fonte primária, o que só funcionou porque a CoS estava na
sessão. O board hunt e o check F1 leem silêncio exatamente onde aconteceu a
conversa mais importante do dia. [[feedback_camada_cega_a_email_planilha_pje]]

A APURAÇÃO (24/08): o evento existe e não estava ligado. `CALL = 'call'` está no
enum `Events` do Evolution v2.3.7 (a versão em produção) e o Baileys v7.0.0-rc.9
emite `offer/accept/reject/timeout/terminate` com `from`, `date` e `isVideo`. A
instância `rap-whatsapp` estava inscrita em cinco eventos, nenhum deles `CALL`.
Foi inscrito em 24/08 23:30 UTC — **só isso, sem código de escrita**: o handler do
INTEL joga evento desconhecido no `else`, que grava o payload cru em
`webhook_audit` como `unhandled_event:call`. Nada entra em `messages`.

🔴 O LIMITE, MEDIDO EM 27/08 — CHAMADA EFETUADA NÃO APARECE. O Renato ligou para
alguém pelo WhatsApp e **nenhum evento chegou**, com a escuta comprovadamente
viva no mesmo instante: `CALL` inscrito, instância `open` e 198 `messages.upsert`
nas 4 h anteriores, o último 5 min antes. O socket do Evolution é um device
**companion**, e o WhatsApp só entrega a ele o nó `call` de chamada RECEBIDA.

Então a cobertura é assimétrica, e vale escrever o que isso custa: as ligações
que **ele origina** seguem invisíveis — e eram exatamente o caso que motivou tudo
(os 26 min com o Jonas e os 4 min com o Piccino, em 24/08). Metade do buraco
continua aberta, e não há conserto pelo lado do Evolution: é limite da plataforma,
não configuração faltando. Quem for tentar de novo, tente por outro caminho.

⚠️ O QUE FOI PROVADO, E COMO. Não por ausência de dado — por **controle positivo**
([[feedback_controle_positivo_pega_o_furo_real]]). Zero no placar, sozinho,
significa "nenhuma chamada aconteceu" OU "acontece e a camada é cega", e os dois
são indistinguíveis sem denominador ([[feedback_mecanismo_que_nao_mede_o_denominador]]).
O que separou foi uma chamada provocada de propósito, com a escuta verificada
viva no mesmo minuto. **Antes de reabrir esta questão, refaça esse par** — não
conclua nada de um placar vazio.

⏭️ O QUE SEGUE SEM TESTE: `accept`. As chamadas capturadas até aqui tocaram e
ninguém atendeu, então a duração de CONVERSA nunca foi exercitada. Atender uma
recebida decide — e é barato, porque recebida a gente sabe que chega.

Uso:  ./placar_chamadas_wa.py              # desde a inscrição do evento
      ./placar_chamadas_wa.py --dias 7
      ./placar_chamadas_wa.py --cru        # despeja o payload de cada evento
"""
import argparse
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

from database import get_connection  # noqa: E402

# Momento em que `CALL` entrou na lista de eventos do webhook da instância
# `rap-whatsapp`. Antes disto o silêncio é explicado — não era escutado.
INSCRITO_EM = "2026-08-24 23:30:00+00"

# Dono da instância `rap-whatsapp` (ownerJid). Serve pra separar chamada
# EFETUADA de RECEBIDA — que é a pergunta nº 1 lá de cima.
DONO_JID = "5511984153337"

# Encerramentos: presença de `accept` seguido de `terminate` é o que torna a
# duração derivável. Sem isso o placar só sabe que o telefone tocou.
FIM = {"accept", "reject", "timeout", "terminate"}


def _quem_ligou(lid: str) -> str:
    """Nome por trás de um `@lid`. Sem isto o marcador diz 'alguém ligou'.

    O evento `call` traz `from` como LID e `callerPn` vazio, e o LID não casa com
    nada no INTEL — as fichas guardam telefone. A ponte existe nas mensagens que a
    Evolution já tem: `key.remoteJidAlt` traz o número canônico do mesmo chat.
    Resolve-se lá, casa-se aqui por dígitos ([[reference_telefone_br_normalizacao]]).

    Falha graciosa: sem rede ou sem match, devolve o próprio LID. Um placar que
    quebra por não achar o nome é pior que um que mostra o número cru.
    """
    import json
    import urllib.request

    base = (os.getenv("EVOLUTION_API_URL") or "").rstrip("/")
    key = os.getenv("EVOLUTION_API_KEY") or ""
    inst = os.getenv("EVOLUTION_INSTANCE", "rap-whatsapp")
    if not base or not key:
        return lid

    try:
        req = urllib.request.Request(
            f"{base}/chat/findMessages/{inst}",
            data=json.dumps({"where": {"key": {"remoteJid": lid}}, "page": 1, "offset": 3}).encode(),
            headers={"apikey": key, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=25) as r:
            d = json.load(r)
        recs = (d.get("messages") or {}).get("records") or d.get("records") or []
        alt = next((m.get("key", {}).get("remoteJidAlt") for m in recs
                    if m.get("key", {}).get("remoteJidAlt")), None)
        if not alt:
            return lid
        digitos = "".join(ch for ch in alt.split("@")[0] if ch.isdigit())
    except Exception:
        return lid

    # Sem DDI: a ficha pode guardar com ou sem o 55.
    cur = get_connection().cursor()
    cur.execute("SELECT id, nome FROM contacts WHERE telefones::text LIKE %s LIMIT 1",
                (f"%{digitos[2:]}%",))
    row = cur.fetchone()
    return f"{row['nome']} (#{row['id']})" if row else f"+{digitos}"


def _fmt(td):
    """Duração legível. Chamada se mede em minutos; escuta, em dias."""
    if td is None:
        return "?"
    s = int(td.total_seconds())
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}min{s % 60:02d}s"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}min"
    return f"{s // 86400}d{(s % 86400) // 3600:02d}h"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dias", type=int, default=None,
                    help="janela em dias (padrão: desde a inscrição do evento)")
    ap.add_argument("--cru", action="store_true", help="despeja o payload de cada evento")
    a = ap.parse_args()

    cur = get_connection().cursor()

    # Parênteses obrigatórios: `NOW() - NOW() - interval` (sem eles) dá o
    # intervalo NEGATIVO, e o cabeçalho anunciava "escutando há -86400s".
    if a.dias:
        corte_sql, params = "(NOW() - (%s || ' days')::interval)", (a.dias,)
        desde = f"últimos {a.dias} dias"
    else:
        corte_sql, params = "(%s::timestamptz)", (INSCRITO_EM,)
        desde = f"a inscrição do evento ({INSCRITO_EM[:16]} UTC)"

    cur.execute(
        f"""
        SELECT received_at, decision, decision_reason, payload
          FROM webhook_audit
         WHERE event_type = 'call'
           AND received_at >= {corte_sql}
         ORDER BY received_at
        """,
        params,
    )
    # RealDictCursor: as linhas vêm como dict, não tupla.
    linhas = [(r["received_at"], r["payload"]) for r in cur.fetchall()]

    # Quanto tempo de escuta o placar cobre — sem isso "0" não é interpretável.
    cur.execute(f"SELECT NOW() - {corte_sql} AS escutando", params)
    escutando = cur.fetchone()["escutando"]

    print("╔═ CHAMADAS DE VOZ DO WHATSAPP — o que o evento `call` trouxe ═╗")
    print(f"  janela: desde {desde} · escutando há {_fmt(escutando)}")

    if not linhas:
        print(f"\n  ⚪ NENHUM evento `call` recebido em {_fmt(escutando)} de escuta.")
        print("     Isto NÃO decide nada: pode não ter havido chamada, ou a camada")
        print("     pode ser cega a ela. Só o controle positivo separa os dois —")
        print("     provoque UMA chamada recebida e UMA efetuada, e rode de novo.")
        return 0

    # Um `call.id` é uma chamada; cada status dela é um evento separado.
    chamadas = defaultdict(list)
    for received_at, payload in linhas:
        d = (payload or {}).get("data") or {}
        chamadas[d.get("id") or f"sem-id:{received_at}"].append((received_at, d))

    efetuadas = recebidas = com_duracao = 0
    atendidas = []
    print(f"\n  {len(linhas)} eventos · {len(chamadas)} chamada(s) distinta(s)\n")

    for cid, eventos in chamadas.items():
        eventos.sort(key=lambda e: e[0])
        primeiro = eventos[0][1]
        de = str(primeiro.get("from") or primeiro.get("chatId") or "?")
        # `from` é quem ORIGINOU. Igual ao dono = ele ligou; diferente = ligaram pra ele.
        saiu_dele = DONO_JID in de
        efetuadas += saiu_dele
        recebidas += not saiu_dele

        trilha = [(e[1].get("status") or "?") for e in eventos]
        video = primeiro.get("isVideo")
        grupo = primeiro.get("isGroup")

        # Duração de CONVERSA só existe se a chamada foi atendida e depois
        # encerrada. Sem `accept` no meio, `terminate` marca o fim do TOQUE —
        # e chamar isso de duração seria inventar conversa que não houve.
        t_offer = next((t for t, e in eventos if e.get("status") == "offer"), eventos[0][0])
        t_accept = next((t for t, e in eventos if e.get("status") == "accept"), None)
        t_fim = next((t for t, e in reversed(eventos) if e.get("status") in FIM), None)
        atendida = t_accept is not None
        dur = (t_fim - t_accept) if (atendida and t_fim) else None
        com_duracao += dur is not None
        atendidas.append(atendida)

        rotulo = "📤 EFETUADA" if saiu_dele else "📥 RECEBIDA"
        extra = " · vídeo" if video else ""
        extra += " · grupo" if grupo else ""
        print(f"  {rotulo}  {eventos[0][0]:%d/%m %H:%M} UTC  {_quem_ligou(de)}{extra}")
        print(f"     status: {' → '.join(trilha)}")
        if atendida:
            print(f"     ✅ ATENDIDA · conversa de {_fmt(dur)}")
        else:
            tocou = (t_fim - t_offer) if t_fim else None
            print(f"     📵 NÃO ATENDIDA · tocou {_fmt(tocou)} e encerrou ({trilha[-1]})")
        if a.cru:
            print(f"     payload: {eventos[0][1]}")
        print()

    n_atendidas = sum(atendidas)
    print("  ── o que isto responde ──")
    print(f"  recebidas: {recebidas} · efetuadas: {efetuadas}")
    print(f"  atendidas: {n_atendidas} de {len(chamadas)} · com duração de conversa: {com_duracao}")

    # As duas perguntas em aberto — e o cuidado de não fechar nenhuma sem prova.
    if efetuadas == 0:
        print("\n  🔴 EFETUADAS EM ZERO — e isto JÁ FOI DECIDIDO em 27/08, não é lacuna.")
        print("     O Renato ligou de propósito, com a escuta verificada viva no")
        print("     mesmo minuto, e nada chegou: o companion só enxerga chamada")
        print("     RECEBIDA. É limite da plataforma, não config faltando — as")
        print("     ligações que ELE origina seguem sem cobertura, por outro caminho.")
    if n_atendidas == 0:
        print("\n  ❓ NENHUMA FOI ATENDIDA, então `accept` continua sem ser exercitado.")
        print("     Isto NÃO é 'accept não vem' — é que ninguém atendeu. A duração")
        print("     de conversa segue não testada; atenda UMA e o placar decide.")
    elif com_duracao == n_atendidas:
        print("\n  ✅ `accept` + `terminate` chegam: duração de conversa CONFIRMADA.")

    if len(chamadas) and recebidas:
        print(f"\n  ✅ O QUE JÁ ESTÁ PROVADO: {recebidas} chamada(s) RECEBIDA(s) que não")
        print("     existem em `messages` agora têm registro. Mesmo sem duração,")
        print("     'fulano ligou e você não atendeu' é o silêncio que custava frente.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
