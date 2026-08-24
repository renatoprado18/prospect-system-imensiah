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

O QUE ESTE SCRIPT NÃO SABE, E É O PONTO. Ligar o evento responde metade da
pergunta. A outra metade é empírica e só o tempo responde:

  1. **Chamada que o Renato FAZ aparece?** O socket do Evolution é um device
     companion, e o nó `call` que o Baileys processa é o que o servidor entrega a
     ele. Se o WhatsApp só notifica companions de chamada RECEBIDA, as ligações
     que ele origina — que são a maioria, porque ele age direto — seguem invisíveis
     e o conserto não resolve o caso que o motivou.
  2. **Dá pra derivar duração?** Só se `accept` e `terminate` chegarem os dois.
     Com apenas `offer`, o placar sabe que houve toque, não que houve conversa.

⚠️ ZERO NÃO É RESPOSTA. Placar vazio significa "nenhuma chamada aconteceu" OU
"acontece e a camada é cega" — e os dois são indistinguíveis sem denominador
([[feedback_mecanismo_que_nao_mede_o_denominador]]). Quem separa é o CONTROLE
POSITIVO: peça a alguém pra te ligar no WhatsApp, ligue você para alguém, e rode
de novo. Uma chamada provocada de cada direção mata a ambiguidade em 2 minutos —
que é o que 54 dias de log não fizeram ([[feedback_controle_positivo_pega_o_furo_real]]).

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


def _fmt(td):
    if td is None:
        return "?"
    s = int(td.total_seconds())
    return f"{s // 60}min{s % 60:02d}s" if s >= 60 else f"{s}s"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dias", type=int, default=None,
                    help="janela em dias (padrão: desde a inscrição do evento)")
    ap.add_argument("--cru", action="store_true", help="despeja o payload de cada evento")
    a = ap.parse_args()

    cur = get_connection().cursor()

    if a.dias:
        corte_sql, params = "NOW() - (%s || ' days')::interval", (a.dias,)
        desde = f"últimos {a.dias} dias"
    else:
        corte_sql, params = "%s::timestamptz", (INSCRITO_EM,)
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
    for received_at, _dec, _mot, payload in linhas:
        d = (payload or {}).get("data") or {}
        chamadas[d.get("id") or f"sem-id:{received_at}"].append((received_at, d))

    efetuadas = recebidas = com_duracao = 0
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

        # Duração só existe se a chamada foi ATENDIDA e depois encerrada.
        t_accept = next((t for t, e in eventos if e.get("status") == "accept"), None)
        t_fim = next((t for t, e in reversed(eventos) if e.get("status") == "terminate"), None)
        dur = (t_fim - t_accept) if (t_accept and t_fim) else None
        com_duracao += dur is not None

        rotulo = "📤 EFETUADA" if saiu_dele else "📥 RECEBIDA"
        extra = " ·  vídeo" if video else ""
        extra += " · grupo" if grupo else ""
        print(f"  {rotulo}  {eventos[0][0]:%d/%m %H:%M} UTC  {de}{extra}")
        print(f"     status: {' → '.join(trilha)}")
        print(f"     duração: {_fmt(dur) if dur else 'NÃO DERIVÁVEL (falta accept e/ou terminate)'}")
        if a.cru:
            print(f"     payload: {eventos[0][1]}")
        print()

    print("  ── o que isto responde ──")
    print(f"  recebidas: {recebidas} · efetuadas: {efetuadas}")
    if efetuadas == 0:
        print("  ⚠️  ZERO efetuadas. Se ele ligou pra alguém nesta janela e não")
        print("      aparece aqui, está confirmado: o companion só enxerga chamada")
        print("      RECEBIDA — e o caso de 24/08 (ele ligando) segue sem cobertura.")
    print(f"  com duração derivável: {com_duracao} de {len(chamadas)}")
    if com_duracao == 0:
        print("  ⚠️  Nenhuma trouxe accept+terminate: dá pra marcar QUE houve chamada,")
        print("      não QUANTO durou. Um marcador sem duração ainda mata o silêncio.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
