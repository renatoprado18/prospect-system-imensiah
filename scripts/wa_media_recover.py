# -*- coding: utf-8 -*-
"""Recupera midia de WhatsApp (audio/imagem/documento) a partir do payload bruto
guardado em webhook_audit, contornando o pipeline da Evolution (404 desde 19/09).

Uso:
  python3 scripts/wa_media_recover.py <external_id> [...]     # ids especificos
  python3 scripts/wa_media_recover.py --pendentes 2026-09-19  # tudo sem texto desde a data

Audio -> Groq whisper-large-v3. PDF -> pdftotext. Grava em wa_attachments.extracted_text.
"""
import base64, hashlib, hmac, json, os, subprocess, sys, urllib.request

SCR = os.environ.get("WA_RECOVER_DIR", "/tmp/wa_recover")
os.makedirs(SCR, exist_ok=True)
INFO = {"audioMessage": (b"WhatsApp Audio Keys", ".ogg"),
        "imageMessage": (b"WhatsApp Image Keys", ".jpg"),
        "videoMessage": (b"WhatsApp Video Keys", ".mp4"),
        "documentMessage": (b"WhatsApp Document Keys", ".pdf"),
        "documentWithCaptionMessage": (b"WhatsApp Document Keys", ".pdf")}
VOCAB = ("gotejamento, gotejador, emissor, outorga, captacao, vazao, lamina, adutora, "
         "motobomba, filtro, setor, talhao, hectare, ART, CREA, Netafim, Rivulis, "
         "Netasul, IrrigaSul, Coopercitrus, Jaboticabeiras, Guaxupe")


def psql(sql, quiet=False):
    r = subprocess.run(["psql", os.environ["DATABASE_URL"], "-At", "-c", sql],
                       capture_output=True, text=True)
    if r.returncode and not quiet:
        raise RuntimeError(r.stderr[:300])
    return r.stdout


def hkdf(key, length, info):
    prk = hmac.new(b"\0" * 32, key, hashlib.sha256).digest()
    out, t, i = b"", b"", 1
    while len(out) < length:
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        out += t
        i += 1
    return out[:length]


def find_media(node):
    """Desce na arvore do payload ate achar um no com url + mediaKey."""
    if not isinstance(node, dict):
        return None
    for k, v in node.items():
        if k in INFO and isinstance(v, dict) and "url" in v and "mediaKey" in v:
            return k, v
    for v in node.values():
        if isinstance(v, dict):
            got = find_media(v.get("message") or v)
            if got:
                return got
    return None


def grava(wid, texto, modelo):
    texto = texto.replace("\x00", "").strip()
    if not texto:
        return False
    sql = ("UPDATE wa_attachments SET extracted_text=$T$%s$T$, extraction_model=%s, "
           "error=NULL WHERE message_id=%s;" % (texto, repr(modelo).replace('"', "'"), repr(wid)))
    r = subprocess.run(["psql", os.environ["DATABASE_URL"], "-c", sql],
                       capture_output=True, text=True)
    return r.returncode == 0


def recupera(wid):
    # busca rapida pela coluna indexada; LIKE no payload so como ultimo recurso
    rows = psql("select payload::text from webhook_audit where message_id=%s;" % repr(wid), True)
    if not rows.strip():
        rows = psql("select payload::text from webhook_audit "
                    "where message_id is null and payload::text like '%%%s%%' limit 3;" % wid, True)
    node = None
    for line in rows.splitlines():
        try:
            p = json.loads(line)
        except Exception:
            continue
        node = find_media((p.get("data") or {}).get("message") or {})
        if node:
            break
    if not node:
        return wid, "?", "SEM PAYLOAD", ""

    kind, m = node
    info, ext = INFO[kind]
    mk = m["mediaKey"]
    mk = bytes(mk[str(i)] for i in range(len(mk))) if isinstance(mk, dict) else base64.b64decode(mk)
    enc, dec = os.path.join(SCR, wid + ".enc"), os.path.join(SCR, wid + ext)
    try:
        req = urllib.request.Request(m["url"], headers={"User-Agent": "WhatsApp/2.23"})
        data = urllib.request.urlopen(req, timeout=120).read()
    except Exception as e:
        return wid, kind, "DOWNLOAD FALHOU: %s" % str(e)[:60], ""
    open(enc, "wb").write(data[:-10])
    k = hkdf(mk, 112, info)
    r = subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-K", k[16:48].hex(),
                        "-iv", k[0:16].hex(), "-in", enc, "-out", dec],
                       capture_output=True, text=True)
    os.remove(enc)
    if r.returncode:
        return wid, kind, "DECRIPT FALHOU", ""
    label = m.get("fileName") or (m.get("caption") or "")[:40] or kind

    if kind == "audioMessage":
        out = subprocess.run(["curl", "-s", "--max-time", "180",
            "-H", "Authorization: Bearer " + os.environ["GROQ_API_KEY"],
            "-F", "file=@" + dec, "-F", "model=whisper-large-v3", "-F", "language=pt",
            "-F", "temperature=0", "-F", "response_format=text", "-F", "prompt=" + VOCAB,
            "https://api.groq.com/openai/v1/audio/transcriptions"], capture_output=True, text=True)
        txt = out.stdout.strip()
        ok = grava(wid, txt, "whisper-large-v3 (recuperado de webhook_audit)")
        return wid, "audio", ("OK" if ok else "TRANSCRICAO VAZIA"), txt[:220]

    if ext == ".pdf":
        out = subprocess.run(["pdftotext", "-layout", dec, "-"], capture_output=True, text=True)
        txt = (out.stdout or "").strip()
        if len(txt) < 40:
            return wid, kind, "PDF SEM TEXTO (escaneado, precisa OCR) -> " + dec, label
        ok = grava(wid, txt, "pdftotext (recuperado de webhook_audit)")
        return wid, "pdf", ("OK" if ok else "FALHA AO GRAVAR"), (label + " | " + txt[:180])

    return wid, kind, "SALVO (precisa leitura visual) -> " + dec, label


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--pendentes":
        desde = args[1] if len(args) > 1 else "2026-09-19"
        ids = [x for x in psql("select message_id from wa_attachments where extracted_text is null "
                               "and criado_em >= '%s' order by criado_em;" % desde).split() if x]
    else:
        ids = args
    print("%d anexos a processar\n" % len(ids), flush=True)
    for i, wid in enumerate(ids, 1):
        try:
            w, kind, status, preview = recupera(wid)
        except Exception as e:
            w, kind, status, preview = wid, "?", "ERRO: " + str(e)[:80], ""
        print("[%d/%d] %s | %s | %s" % (i, len(ids), w, kind, status), flush=True)
        if preview:
            print("        " + preview.replace("\n", " ")[:220], flush=True)
