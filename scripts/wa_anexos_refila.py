#!/usr/bin/env -S /Users/rap/prospect-system/.venv/bin/python
"""Re-dispara pro worker os anexos WA que ficaram sem extração.

POR QUE EXISTE (06/08/2026). Em 30 dias, **50 de 52 áudios DM incoming** não
deixaram uma linha em `wa_attachments`: o extrator do DM rodava dentro do
`intel-api`, onde `GROQ_API_KEY` nunca existiu, e a função retornava calada.
A causa foi corrigida (`a4e57f2` — um extrator só, o do worker). Corrigir o
cano não traz o que caiu fora dele: isto traz.

A ORDEM IMPORTAVA. Esta fila só foi escrita DEPOIS de achar a causa — antes
disso ela nasceria cega, porque a lista de "o que reprocessar" se monta contra
`wa_attachments`, e o que nunca foi registrado não aparece em lista nenhuma.
Por isso a busca aqui parte das MENSAGENS (`messages` + `group_messages`), não
da tabela de anexos: a fonte é o denominador, a tabela é o resultado.

O que entra:
  · mensagem de anexo (áudio/imagem/documento) sem linha em `wa_attachments`;
  · linha com `error` preenchido e sem texto extraído (falha registrada).

Limite conhecido e medido: a mídia vive no Baileys, que a expira. Áudio velho
volta `download_failed` — e agora isso fica GRAVADO, então a segunda passada
não tenta de novo às cegas.

Uso:
    ./wa_anexos_refila.py                      # dry-run, 30 dias
    ./wa_anexos_refila.py --apply              # dispara
    ./wa_anexos_refila.py --dias 7 --apply     # janela menor
    ./wa_anexos_refila.py --kind audio --apply # só um tipo
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

import psycopg2

ROOT = "/Users/rap/prospect-system"

APPLY = "--apply" in sys.argv
PAUSA = 1.5          # respiro entre dispatches — o worker processa em background


def arg(nome, padrao):
    return sys.argv[sys.argv.index(nome) + 1] if nome in sys.argv else padrao


DIAS = int(arg("--dias", 30))
KIND = arg("--kind", None)


def env(k):
    for l in open(f"{ROOT}/.env"):
        if l.startswith(k + "="):
            return l.split("=", 1)[1].strip().strip('"')
    return os.getenv(k, "")


def railway_var(servico, chave):
    """WORKER_SECRET/AUDIO_WORKER_URL vivem no Railway, não no .env local."""
    import subprocess
    try:
        out = subprocess.run(
            ["railway", "variables", "--service", servico, "--kv"],
            capture_output=True, text=True, timeout=60,
        ).stdout
    except Exception:
        return ""
    for linha in out.splitlines():
        if linha.startswith(chave + "="):
            return linha.split("=", 1)[1].strip()
    return ""


ENDPOINT = {"audio": "/transcribe", "pdf": "/analyze-pdf", "image": "/analyze-image"}


def candidatos(cur):
    """Anexos sem extração, dos DOIS canais. Devolve (kind, message_id, phone, jid, quando, quem)."""
    # DM: o tipo vive em metadata->>'type'; o texto do corpo e so o rotulo ([Áudio], [Imagem]).
    cur.execute("""
        SELECT COALESCE(m.metadata->>'type','?') AS tipo, m.external_id,
               m.metadata->>'phone' AS phone, NULL::text AS jid,
               m.criado_em, COALESCE(ct.nome, m.metadata->>'phone') AS quem
        FROM messages m
        LEFT JOIN contacts ct ON ct.id = m.contact_id
        LEFT JOIN wa_attachments a ON a.message_id = m.external_id
        WHERE m.criado_em > NOW() - make_interval(days => %s)
          AND m.metadata->>'type' IN ('audio','image','document')
          AND m.external_id IS NOT NULL
          AND (a.message_id IS NULL
               OR (a.extracted_text IS NULL OR a.extracted_text = ''))
        ORDER BY m.criado_em DESC
    """, (DIAS,))
    linhas = list(cur.fetchall())

    cur.execute("""
        SELECT gm.message_type, gm.message_id, gm.sender_phone, gm.group_jid,
               gm.timestamp, COALESCE(gm.sender_name, gm.sender_phone)
        FROM group_messages gm
        LEFT JOIN wa_attachments a ON a.message_id = gm.message_id
        WHERE gm.timestamp > NOW() - make_interval(days => %s)
          AND gm.message_type IN ('audio','image','document')
          AND (a.message_id IS NULL
               OR (a.extracted_text IS NULL OR a.extracted_text = ''))
        ORDER BY gm.timestamp DESC
    """, (DIAS,))
    linhas += list(cur.fetchall())

    saida = []
    for tipo, mid, phone, jid, quando, quem in linhas:
        kind = {"document": "pdf"}.get(tipo, tipo)
        if kind not in ENDPOINT:
            continue
        if KIND and kind != KIND:
            continue
        saida.append((kind, mid, phone or "", jid, quando, quem or "?"))
    return saida


def dispara(url, segredo, kind, mid, phone, jid):
    key = {"id": mid, "fromMe": False,
           "remoteJid": jid or f"{''.join(filter(str.isdigit, phone))}@s.whatsapp.net"}
    if jid:
        key["participant"] = f"{''.join(filter(str.isdigit, phone))}@s.whatsapp.net"
    corpo = {
        "key": key, "phone": phone, "message_id": mid, "secret": segredo,
        "source": "refila", "instance": "rap-whatsapp", "silent": True,
    }
    if kind == "pdf":
        corpo["filename"] = "documento.pdf"
    req = urllib.request.Request(
        f"{url}{ENDPOINT[kind]}", data=json.dumps(corpo).encode(),
        method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, ""
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:120].decode(errors="replace")
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def main():
    url = railway_var("intel-api", "AUDIO_WORKER_URL") or env("AUDIO_WORKER_URL")
    segredo = railway_var("intel-api", "WORKER_SECRET") or env("WORKER_SECRET")
    if not url or not segredo:
        sys.exit("AUDIO_WORKER_URL/WORKER_SECRET não encontrados (Railway nem .env)")

    conn = psycopg2.connect(env("DATABASE_URL"), connect_timeout=30)
    cur = conn.cursor()
    fila = candidatos(cur)

    por_kind = {}
    for kind, *_ in fila:
        por_kind[kind] = por_kind.get(kind, 0) + 1
    print(f"janela: {DIAS} dias · sem extração: {len(fila)}  "
          f"({', '.join(f'{k}={v}' for k, v in sorted(por_kind.items())) or '—'})")

    if not APPLY:
        for kind, mid, phone, jid, quando, quem in fila[:20]:
            print(f"  · {quando:%d/%m %H:%M} {kind:5} {quem[:28]:28} {mid[:22]}")
        if len(fila) > 20:
            print(f"  … e mais {len(fila) - 20}")
        print("\ndry-run — nada disparado. Use --apply.")
        return

    ok = falhou = 0
    for i, (kind, mid, phone, jid, quando, quem) in enumerate(fila, 1):
        status, detalhe = dispara(url, segredo, kind, mid, phone, jid)
        if status in (200, 201, 202):
            ok += 1
        else:
            falhou += 1
            print(f"  ⚠ {kind} {mid[:22]} → {status} {detalhe}")
        if i % 25 == 0:
            print(f"  … {i}/{len(fila)}")
        time.sleep(PAUSA)

    print(f"\ndisparados: {ok} · recusados no dispatch: {falhou}")
    print("O worker processa em background — confira o EFEITO em ~2min:")
    print(f"  extraídos: SELECT count(*) FROM wa_attachments "
          f"WHERE criado_em > NOW() - INTERVAL '10 minutes' AND extracted_text <> '';")
    print(f"  com erro : SELECT error, count(*) FROM wa_attachments "
          f"WHERE criado_em > NOW() - INTERVAL '10 minutes' AND error IS NOT NULL GROUP BY 1;")


if __name__ == "__main__":
    main()
