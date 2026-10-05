# -*- coding: utf-8 -*-
"""Backfill de midia de WhatsApp direto do CDN, contornando a Evolution.

POR QUE ESTE SCRIPT EXISTE, ao lado do `wa_media_recover.py`:
o `--pendentes` daquele varre `wa_attachments`, isto e, midia que JA TEM LINHA.
Medido em 29/09: das 799 midias cegas desde 19/09, **723 nunca geraram linha** —
sao mensagens de grupo, que o webhook marca `skipped: group_message`. Elas eram
gravadas normalmente antes de 19/09 (74% das midias de grupo tinham anexo), e
passaram a 7% depois. Um backfill que le `wa_attachments` nao as ve: enxerga 27
e ignora 523. Aqui a fonte de verdade e o `webhook_audit`, e a escrita e UPSERT.

A causa do buraco esta no dispatch: o worker (`AUDIO_WORKER_URL`) baixa a midia
DA EVOLUTION, e com `silent: True` ele devolve 200 em <500ms antes de tentar.
Quando o download 404 falha do lado dele, o INTEL ja recebeu o 200 e nao grava
linha nenhuma — nem de erro. Dai a cegueira nao ter deixado rastro.

CUSTO — o que este script mede e o que deliberadamente NAO mede (05/10/2026):

  ✅ VISAO (Anthropic) — registrada em `tonia_llm_usage` como
     `backfill.wa_image_vision`, com tokens e custo reais do `usage` da resposta.
     Era o furo: o medidor que o `capability_registry` le nao via nenhuma das
     chamadas de visao deste script. Ver `extrai_imagem`.

  ⬜ AUDIO (Groq whisper-large-v3) — NAO registrado, de proposito. O Groq cobra por
     SEGUNDO DE AUDIO, nao por token, e o sistema inteiro nao tem convencao de custo
     pra Groq (grep por "groq" + custo volta vazio em app/, workers/ e scripts/).
     As duas saidas erradas seriam: (a) passar o modelo pro `compute_cost`, onde
     `_tier_for_model("whisper-large-v3")` nao casa haiku/sonnet/opus e cai no
     **preco de Sonnet** — inventaria um numero com tres ordens de grandeza de erro;
     (b) gravar `cost_usd = 0`, que no medidor se le como "de graca". Custo ausente
     e uma lacuna conhecida; custo errado contamina a unica regua que temos.
     ⏭️ Decisao pendente: convencao de preco pro Groq (precisa da duracao do audio).

  ⬜ PDF/documento (`pdftotext`, local) — sem custo de API. Nada a registrar.

  ⚠️ `wa_attachments.extraction_cost_usd` NAO e preenchido aqui, e nao e esquecimento:
     a coluna **nao tem leitor**. Medido em 05/10 — existe na migration 031, tem UM
     escritor (o `/analyze-pdf` do worker, 424 linhas / US$ 30,78) e ZERO consumidores:
     nenhum SELECT no app, no worker ou em template a le, e ninguem faz `SELECT *`.
     Preenche-la seria wiring pra consumidor morto ([[feedback_consumidor_morto_wiring]]).
     O custo por chamada vive no `tonia_llm_usage`, que TEM leitor
     (`capability_registry`), e a rastreabilidade por anexo fica no `metadata.message_id`
     da propria linha de usage.

Uso:
  python3 scripts/wa_media_backfill.py --dry-run          # so mede, nao grava
  python3 scripts/wa_media_backfill.py --desde 2026-09-19
  python3 scripts/wa_media_backfill.py --kind image --limit 20

Requer o `.venv` (anthropic + cryptography):
  DB_TARGET=prod ALLOW_PROD_FROM_LOCAL=1 .venv/bin/python3 scripts/wa_media_backfill.py
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # noqa: E402

from database import get_db  # noqa: E402

# Modelo de visao: o MESMO que o worker usa em `/analyze-image`, de proposito.
# As 3.027 imagens ja extraidas gravaram `extraction_model='claude-haiku-4-5-vision'`;
# trocar o modelo aqui produziria texto de natureza diferente na mesma coluna, e o
# consumidor (a view `copilot.group_messages`) nao sabe distinguir os dois.
MODELO_VISAO = "claude-haiku-4-5-20251001"

# ⚠️ O prompt do worker pede "descreva o que voce ve... se for uma tela do sistema,
# identifique o que pode ser melhorado" — e por isso que o OCR do WA descrevia a
# INTERFACE em vez do conteudo (`wa_attachments#3895`). O defeito era o PROMPT, nao
# a fonte da imagem. Aqui o pedido e extracao, e a descricao e o caso de exceCAO.
PROMPT_VISAO = (
    "Extraia TODO o texto visivel nesta imagem, preservando a ordem de leitura e os "
    "numeros exatamente como aparecem (valores, datas, CNPJ, nomes proprios). "
    "Se for print de conversa, documento, planilha, contrato ou tabela, transcreva o "
    "conteudo — nao descreva a tela nem comente o layout. "
    "Somente se a imagem nao tiver texto algum (foto de pessoa, lugar, objeto), "
    "descreva em uma frase o que ela mostra."
)

# Vocabulario das frentes do Renato — melhora nome proprio e termo tecnico na transcricao.
VOCAB_AUDIO = (
    "gotejamento, outorga, captacao, vazao, lamina, adutora, motobomba, talhao, hectare, "
    "ART, CREA, Netafim, Rivulis, Netasul, IrrigaSul, Coopercitrus, Jaboticabeiras, Guaxupe, "
    "Vallen, Alba, Vibra, Assespro, Carambola, FLAMARPAR, JUCESP, cisao, acordo de socios"
)

# O CHECK de `wa_attachments.kind` aceita 4 valores: pdf, audio, image, documento.
# `video` NAO esta na lista — inserir video levanta erro no INSERT, que foi
# exatamente a falha calada do commit bf62296. Video fica FORA do lote e e
# contado no resumo: 131 cegos em 29/09, sem extrator e sem kind valido.
INFO_CRIPTO = {
    "imageMessage": (b"WhatsApp Image Keys", "image", ".jpg"),
    "audioMessage": (b"WhatsApp Audio Keys", "audio", ".ogg"),
    "documentMessage": (b"WhatsApp Document Keys", None, None),
    "documentWithCaptionMessage": (b"WhatsApp Document Keys", None, None),
    "videoMessage": (b"WhatsApp Video Keys", "video", ".mp4"),
}

TIPOS_MIDIA = ["imageMessage", "audioMessage", "documentMessage", "videoMessage"]

EXTENSOES_DOCUMENTO = (
    ".xlsx", ".xls", ".docx", ".doc", ".pptx", ".ppt",
    ".csv", ".txt", ".md", ".rtf", ".odt", ".ods",
)


def hkdf(key: bytes, length: int, info: bytes) -> bytes:
    """HKDF-SHA256 com salt zerado — o esquema de midia do WhatsApp."""
    prk = hmac.new(b"\0" * 32, key, hashlib.sha256).digest()
    out, t, i = b"", b"", 1
    while len(out) < length:
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        out += t
        i += 1
    return out[:length]


def as_bytes(v):
    """A `mediaKey` chega no payload como dict {'0':124,'1':90,...}, nao como bytes.

    Reconstruir por indice: `bytes(v.values())` depende da ordem de insercao do
    JSON e devolve chave errada em silencio — e chave errada nao levanta erro,
    produz lixo que o SHA-256 la embaixo pega.
    """
    if isinstance(v, dict):
        return bytes(v[str(i)] for i in range(len(v)))
    if isinstance(v, list):
        return bytes(v)
    if isinstance(v, str):
        return base64.b64decode(v)
    raise TypeError(type(v))


def find_media(node):
    """Desce na arvore do payload ate achar um no com url + mediaKey.

    ⚠️ NUNCA descer em `contextInfo`: uma resposta de texto puro carrega o
    `imageMessage` da mensagem CITADA dentro de `contextInfo.quotedMessage`
    ([[reference_contar_midia_wa_no_payload]]). Descer ali faz o script baixar a
    midia do vizinho e gravar como se fosse anexo de uma mensagem que e so texto.
    Por isso a descida e cega a esse ramo — o resto da arvore (ephemeralMessage,
    viewOnceMessageV2, templateMessage) e midia legitima em envelope.
    """
    if not isinstance(node, dict):
        return None
    for k, v in node.items():
        if k in INFO_CRIPTO and isinstance(v, dict) and "url" in v and "mediaKey" in v:
            return k, v
    for k, v in node.items():
        if k in ("contextInfo", "quotedMessage"):
            continue
        if isinstance(v, dict):
            got = find_media(v.get("message") or v)
            if got:
                return got
    return None


def detecta_kind(tipo: str, m: dict):
    """Devolve o `kind` do CHECK, ou None se nao houver kind valido pra este tipo."""
    _, kind_fixo, _ = INFO_CRIPTO[tipo]
    if kind_fixo:
        return kind_fixo
    mime = (m.get("mimetype") or "").lower()
    nome = (m.get("fileName") or "").lower()
    if "pdf" in mime or nome.endswith(".pdf"):
        return "pdf"
    if nome.endswith(EXTENSOES_DOCUMENTO) or "spreadsheet" in mime or "wordprocessing" in mime:
        return "documento"
    return None


def baixa_e_decifra(m: dict, info: bytes):
    """Baixa o .enc do CDN e devolve (plaintext, sha_confere). Levanta em falha de rede."""
    req = urllib.request.Request(m["url"], headers={"User-Agent": "WhatsApp/2.23"})
    enc = urllib.request.urlopen(req, timeout=180).read()

    chaves = hkdf(as_bytes(m["mediaKey"]), 112, info)
    iv, cipher_key = chaves[:16], chaves[16:48]
    corpo = enc[:-10]  # os 10 bytes finais sao MAC, nao ciphertext
    dec = Cipher(algorithms.AES(cipher_key), modes.CBC(iv)).decryptor()
    plain = dec.update(corpo) + dec.finalize()
    plain = plain[:-plain[-1]]  # unpad PKCS7

    # O SHA-256 e a unica prova de que o arquivo chegou inteiro e a chave era a
    # certa. Sem ele, um download truncado ou uma mediaKey mal reconstruida
    # produz bytes plausiveis que viram "texto extraido" e ninguem ve.
    esperado = m.get("fileSha256")
    confere = None
    if esperado:
        confere = hashlib.sha256(plain).digest() == as_bytes(esperado)
    return plain, confere


def extrai_imagem(caminho: str, mime: str, caption: str, message_id: str = "") -> str:
    """Visao da Anthropic. Registra a chamada em `tonia_llm_usage` ANTES de voltar.

    05/10/2026 — este script gastou centenas de chamadas de visao pagas sem
    aparecer em medidor nenhum. O pipeline vivo nao tem esse furo: o worker chama
    `llm_usage.record_response("worker.image_analyze", ...)` e por isso 1.060
    chamadas e US$ 2,79 dos ultimos 30 dias estao atribuidos. Rodando por fora do
    worker, o backfill saltava o unico ponto de gravacao do lado-INTEL, e o custo
    dele caia no balde anonimo `llm:anthropic-platform` do `capability_registry`.

    Isso nao e contabilidade: o registry deriva custo-POR-FUNCAO lendo o
    `endpoint` desta tabela, e e esse numero que falta como DENOMINADOR do
    `value_ratio` (P1 #1000266, NULL nas 23 funcoes). Consumo que nao aparece nao
    pode ser dividido por valor nenhum — a M5 ("kill em 30d sem ROI") nao dispara
    por falta do divisor, nao por falta de candidato.

    Endpoint proprio (`backfill.wa_image_vision`, nao `worker.image_analyze`) de
    proposito: somar o backfill ao caminho vivo esconderia que um e lote
    excepcional e o outro e regime. O registry mede funcao, e sao duas.
    """
    import anthropic
    from services import llm_usage

    b64 = base64.standard_b64encode(open(caminho, "rb").read()).decode()
    pedido = PROMPT_VISAO
    if caption:
        pedido += "\n\nLegenda que acompanhava a imagem: %s" % caption[:300]

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = client.messages.create(
        model=MODELO_VISAO,
        max_tokens=2000,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                {"type": "text", "text": pedido},
            ],
        }],
    )

    # `record_response` aceita dict de `response.json()` OU do SDK via
    # `.model_dump()` — e o SDK e o caminho daqui. Best-effort por contrato: se a
    # tabela sumir no alvo, engole e loga, nunca derruba o backfill no meio do lote.
    try:
        llm_usage.record_response(
            "backfill.wa_image_vision", MODELO_VISAO, resp.model_dump(),
            metadata={"message_id": message_id, "mime": mime, "origem": "wa_media_backfill"},
        )
    except Exception as e:  # noqa: BLE001 — telemetria nunca quebra a extracao
        print("  [aviso] usage nao registrado (%s: %s)" % (type(e).__name__, str(e)[:80]),
              flush=True)

    # `content[0].text` quebra quando o bloco 0 nao e texto (#1000265). Varrer por tipo.
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()


def extrai_audio(caminho: str) -> str:
    out = subprocess.run(
        ["curl", "-s", "--max-time", "300",
         "-H", "Authorization: Bearer " + os.environ["GROQ_API_KEY"],
         "-F", "file=@" + caminho, "-F", "model=whisper-large-v3",
         "-F", "language=pt", "-F", "temperature=0", "-F", "response_format=text",
         "-F", "prompt=" + VOCAB_AUDIO,
         "https://api.groq.com/openai/v1/audio/transcriptions"],
        capture_output=True, text=True)
    return (out.stdout or "").strip()


def extrai_pdf(caminho: str) -> str:
    out = subprocess.run(["pdftotext", "-layout", caminho, "-"],
                         capture_output=True, text=True)
    return (out.stdout or "").strip()


def grava(conn, message_id: str, phone: str, kind: str, mime: str,
          tamanho: int, texto: str, modelo: str, erro: str) -> None:
    """UPSERT em wa_attachments. UPDATE so pisa em linha SEM texto extraido.

    O INSERT e o ponto do exercicio: as ~523 midias de grupo nunca tiveram linha,
    e um UPDATE-only (como o `--pendentes` do wa_media_recover) nao alcanca nenhuma.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO wa_attachments
                (message_id, phone, kind, mime_type, size_bytes,
                 extracted_text, extraction_model, error, criado_em)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (message_id, kind) DO UPDATE
                SET extracted_text = EXCLUDED.extracted_text,
                    extraction_model = EXCLUDED.extraction_model,
                    error = EXCLUDED.error,
                    mime_type = COALESCE(EXCLUDED.mime_type, wa_attachments.mime_type),
                    size_bytes = COALESCE(EXCLUDED.size_bytes, wa_attachments.size_bytes)
                WHERE wa_attachments.extracted_text IS NULL
                   OR wa_attachments.extracted_text = ''
            """,
            (message_id, phone or "", kind, mime or None, tamanho or None,
             (texto or None), (modelo or None), (erro or None)),
        )
    conn.commit()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--desde", default="2026-09-19")
    ap.add_argument("--kind", choices=["image", "audio", "pdf", "documento"],
                    help="processa so um kind (default: todos os 4)")
    ap.add_argument("--limit", type=int, default=0, help="0 = sem limite")
    ap.add_argument("--dry-run", action="store_true",
                    help="baixa, decifra e confere SHA, mas NAO extrai nem grava")
    args = ap.parse_args()

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT w.message_id, w.remote_jid, w.payload, w.received_at
        FROM webhook_audit w
        WHERE w.received_at >= %s
          AND w.message_id IS NOT NULL
          -- ⚠️ NAO usar `payload::text LIKE '%%imageMessage%%'`: resposta citando
          -- midia carrega o bloco dentro de contextInfo.quotedMessage e o LIKE
          -- conta a mensagem de texto como midia — inflou 859 vs 692 (24%%) na
          -- medicao de 29/09. [[reference_contar_midia_wa_no_payload]]
          AND (
               (w.payload->'data'->'message') ?| %s
            OR (w.payload->'data'->'message'->'ephemeralMessage'->'message') ?| %s
            OR (w.payload->'data'->'message'->'viewOnceMessageV2'->'message') ?| %s
            OR (w.payload->'data'->'message'->'templateMessage') IS NOT NULL
          )
          AND NOT EXISTS (
              SELECT 1 FROM wa_attachments a
              WHERE a.message_id = w.message_id
                AND a.extracted_text IS NOT NULL AND a.extracted_text <> ''
          )
        ORDER BY w.received_at
        """,
        (args.desde, TIPOS_MIDIA, TIPOS_MIDIA, TIPOS_MIDIA),
    )
    linhas = cur.fetchall()
    print("universo cego desde %s: %d webhooks" % (args.desde, len(linhas)), flush=True)

    placar = {"ok": 0, "sem_payload": 0, "video_sem_extrator": 0, "kind_indefinido": 0,
              "download_falhou": 0, "sha_divergiu": 0, "sem_texto": 0, "erro": 0,
              "pulado_outro_kind": 0}
    vistos = set()
    feitos = 0

    for r in linhas:
        wid = r["message_id"]
        if wid in vistos:
            continue
        vistos.add(wid)
        if args.limit and feitos >= args.limit:
            break

        payload = r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"])
        node = find_media((payload.get("data") or {}).get("message") or {})
        if not node:
            placar["sem_payload"] += 1
            continue
        tipo, m = node
        if tipo == "videoMessage":
            placar["video_sem_extrator"] += 1
            continue

        kind = detecta_kind(tipo, m)
        if not kind:
            placar["kind_indefinido"] += 1
            continue
        if args.kind and kind != args.kind:
            placar["pulado_outro_kind"] += 1
            continue

        info, _, ext_fixa = INFO_CRIPTO[tipo]
        phone = (r["remote_jid"] or "").split("@")[0]
        mime = (m.get("mimetype") or "").split(";")[0].strip()
        nome = m.get("fileName") or ""
        ext = ext_fixa or os.path.splitext(nome)[1] or (".pdf" if kind == "pdf" else ".bin")
        feitos += 1

        try:
            plain, sha_ok = baixa_e_decifra(m, info)
        except Exception as e:
            placar["download_falhou"] += 1
            print("[%d] %s %-9s DOWNLOAD/DECIFRA FALHOU: %s: %s"
                  % (feitos, wid[:18], kind, type(e).__name__, str(e)[:60]), flush=True)
            if not args.dry_run:
                grava(conn, wid, phone, kind, mime, None, None, None,
                      "cdn: %s: %s" % (type(e).__name__, str(e)[:200]))
            continue

        if sha_ok is False:
            placar["sha_divergiu"] += 1
            print("[%d] %s %-9s SHA-256 DIVERGIU — descartado (%d bytes)"
                  % (feitos, wid[:18], kind, len(plain)), flush=True)
            if not args.dry_run:
                grava(conn, wid, phone, kind, mime, len(plain), None, None,
                      "cdn: sha256 divergiu, arquivo descartado")
            continue

        selo = "sha ok" if sha_ok else "sem sha no payload"
        if args.dry_run:
            placar["ok"] += 1
            print("[%d] %s %-9s %7d bytes | %s | %s"
                  % (feitos, wid[:18], kind, len(plain), selo, nome[:40]), flush=True)
            continue

        tmp = os.path.join(tempfile.gettempdir(), "wa_bf_" + wid[:24] + ext)
        open(tmp, "wb").write(plain)
        try:
            if kind == "image":
                texto = extrai_imagem(tmp, mime or "image/jpeg", m.get("caption") or "", wid)
                modelo = "%s (backfill cdn)" % MODELO_VISAO
            elif kind == "audio":
                texto = extrai_audio(tmp)
                modelo = "whisper-large-v3 (backfill cdn)"
            elif kind == "pdf":
                texto = extrai_pdf(tmp)
                modelo = "pdftotext (backfill cdn)"
            else:
                texto = extrai_pdf(tmp) if ext == ".pdf" else ""
                modelo = "pdftotext (backfill cdn)"
        except Exception as e:
            placar["erro"] += 1
            print("[%d] %s %-9s EXTRACAO FALHOU: %s: %s"
                  % (feitos, wid[:18], kind, type(e).__name__, str(e)[:60]), flush=True)
            grava(conn, wid, phone, kind, mime, len(plain), None, None,
                  "extracao: %s: %s" % (type(e).__name__, str(e)[:200]))
            os.remove(tmp)
            continue
        finally:
            if os.path.exists(tmp) and kind != "documento":
                os.remove(tmp)

        if not texto:
            placar["sem_texto"] += 1
            # PDF escaneado e imagem sem texto caem aqui. Gravar o motivo, nunca
            # deixar sem linha: anexo sem rastro e o buraco que este script conserta.
            grava(conn, wid, phone, kind, mime, len(plain), None, None,
                  "extracao vazia (provavel escaneado/sem texto)")
            print("[%d] %s %-9s SEM TEXTO (%d bytes, %s)"
                  % (feitos, wid[:18], kind, len(plain), selo), flush=True)
            continue

        grava(conn, wid, phone, kind, mime, len(plain), texto, modelo, None)
        placar["ok"] += 1
        print("[%d] %s %-9s OK %d chars | %s | %s"
              % (feitos, wid[:18], kind, len(texto), selo,
                 texto.replace("\n", " ")[:90]), flush=True)

    print("\n== PLACAR (%s) ==" % ("dry-run" if args.dry_run else "gravando"), flush=True)
    for k, v in placar.items():
        if v:
            print("  %-22s %d" % (k, v))
    print("  %-22s %d" % ("processados", feitos))
    return 0


if __name__ == "__main__":
    sys.exit(main())
