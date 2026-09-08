"""Autentique — assinatura de documento deixa de ser canal cego (08/09/26).

O INTEL so soube que o Baeta assinou o acordo de nao-circunvencao porque o
Renato contou. O documento estava la (`a0b9ec99…`, criado 08/09 16h49, 2/2
assinados) e nenhum caminho do sistema o alcancava.

DOIS FORMATOS DE WEBHOOK, e eles nao se parecem:

  NOVO  — JSON, envelope `{id, object:"webhook", event:{id, type, data:{object}}}`,
          tipos `document.*` / `signature.*`, registro no Developer Panel e
          **HMAC-SHA256** no header `X-Autentique-Signature` sobre o corpo CRU.
  ANTIGO— `x-www-form-urlencoded` (NAO json), array `partes[]` com
          `visualizado`/`assinado`/`rejeitado`, e NENHUMA assinatura. O cadastro
          de endpoints novos nesse formato foi descontinuado, mas endpoints ja
          registrados seguem funcionando.

Este modulo aceita os dois. Se so tratasse JSON, um `request.json()` sobre um
corpo form-urlencoded falharia — e falharia CALADO, que e o modo que fez o
audio e o `news_pendente` custarem semanas.

⚠️ RECUSA NAO CHEGA no formato antigo ("eventos de rejeitado nao enviam
webhooks"). Entao ausencia de evento nunca prova que ninguem recusou — so que
ninguem assinou. Qualquer leitura que trate silencio como "seguiu bem" esta
errada por construcao.
"""
import hashlib
import hmac
import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs

import httpx

from database import get_db
from services.tz import now_utc

log = logging.getLogger(__name__)

API_URL = "https://api.autentique.com.br/v2/graphql"

_QUERY_DOCS = """
query($limit: Int!, $page: Int!) {
  documents(limit: $limit, page: $page) {
    total
    data {
      id
      name
      created_at
      signatures {
        name
        email
        action { name }
        viewed   { created_at }
        signed   { created_at }
        rejected { created_at }
      }
    }
  }
}
"""


def _api_key() -> Optional[str]:
    # .strip() nao e paranoia: env var com espaco no fim ja derrubou integracao
    # aqui antes ([[feedback_env_var_whitespace]]).
    v = (os.getenv("AUTENTIQUE_API_KEY") or "").strip()
    return v or None


def _webhook_secret() -> Optional[str]:
    v = (os.getenv("AUTENTIQUE_WEBHOOK_SECRET") or "").strip()
    return v or None


# ─────────────────────────────── verificacao ────────────────────────────────

def verificar_assinatura(raw_body: bytes, headers: Dict[str, str]) -> bool:
    """HMAC-SHA256 do corpo CRU contra `X-Autentique-Signature`.

    Corpo CRU e nao re-serializado: qualquer json.dumps muda espacos e ordem de
    chaves e o digest deixa de bater — o erro classico deste tipo de guarda.

    Sem segredo configurado devolve False (nao "True por otimismo"). O chamador
    decide o que fazer com evento nao verificado; aqui nao se inventa confianca.
    """
    segredo = _webhook_secret()
    if not segredo or not raw_body:
        return False
    recebida = ""
    for k, v in (headers or {}).items():
        if k.lower() == "x-autentique-signature":
            recebida = (v or "").strip()
            break
    if not recebida:
        return False
    if "=" in recebida and recebida.split("=", 1)[0].lower() in ("sha256", "hmac-sha256"):
        recebida = recebida.split("=", 1)[1].strip()
    esperada = hmac.new(segredo.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperada.lower(), recebida.lower())


def autorizar_webhook(
    raw_body: bytes,
    headers: Dict[str, str],
    token_query: Optional[str] = None,
) -> Tuple[bool, bool]:
    """Porteiro da rota. Devolve (autorizado, assinatura_conferida).

    DOIS caminhos porque os dois formatos do Autentique diferem no que oferecem:
      - formato NOVO: HMAC-SHA256 em `X-Autentique-Signature` -> autorizado E
        conferido;
      - formato ANTIGO: nao assina NADA. Sobra o segredo compartilhado na URL
        (`?token=`), que o painel deixa embutir -> autorizado, nao conferido.

    FAIL-CLOSED: sem `AUTENTIQUE_WEBHOOK_SECRET` configurado, nada passa. A
    rota GRAVA e AVISA, entao deixa-la aberta seria dar a qualquer um a
    capacidade de inventar "fulano assinou" no sistema. Preferir 401 enquanto o
    segredo nao existe torna o registro do endpoint um passo consciente, em vez
    de uma porta que ninguem lembra que ficou aberta.
    """
    segredo = _webhook_secret()
    if not segredo:
        return False, False
    if verificar_assinatura(raw_body, headers):
        return True, True
    candidato = (token_query or "").strip()
    if not candidato:
        for k, v in (headers or {}).items():
            if k.lower() in ("x-webhook-token", "x-autentique-token"):
                candidato = (v or "").strip()
                break
    if candidato and hmac.compare_digest(candidato, segredo):
        return True, False
    return False, False


# ──────────────────────────────── parsing ───────────────────────────────────

def _hash_payload(raw: bytes) -> str:
    return hashlib.sha256(raw or b"").hexdigest()[:48]


def parse_webhook(raw_body: bytes, content_type: str = "") -> Optional[Dict[str, Any]]:
    """Normaliza os DOIS formatos num dict unico. None se nao der pra entender.

    Decide pelo CONTEUDO e nao so pelo Content-Type: header errado e comum em
    webhook, e cair no parser errado por causa dele seria falhar calado.
    """
    if not raw_body:
        return None

    texto = raw_body.decode("utf-8", errors="replace").strip()

    # 1. JSON (formato novo). Tentado primeiro por ser o unico com id de evento.
    if texto.startswith("{"):
        try:
            payload = json.loads(texto)
        except Exception:
            payload = None
        if isinstance(payload, dict):
            return _parse_json(payload, raw_body)

    # 2. form-urlencoded (formato antigo/deprecado).
    if "=" in texto:
        try:
            campos = parse_qs(texto, keep_blank_values=True)
        except Exception:
            campos = {}
        if campos:
            return _parse_form(campos, raw_body)

    return None


def _parse_json(payload: Dict[str, Any], raw: bytes) -> Dict[str, Any]:
    evento = payload.get("event") or {}
    dados = (evento.get("data") or {}).get("object") or {}
    tipo = evento.get("type") or payload.get("type")

    # O `object` pode ser o documento OU a assinatura, conforme o tipo. Quando e
    # assinatura, o documento vem aninhado.
    doc = dados.get("document") if isinstance(dados.get("document"), dict) else dados

    return {
        "formato": "json",
        "evento_chave": str(evento.get("id") or payload.get("id") or _hash_payload(raw)),
        "tipo": tipo,
        "documento_id": (doc or {}).get("id"),
        "documento_nome": (doc or {}).get("name"),
        "signatario": dados.get("email") or (dados.get("user") or {}).get("email"),
        "ocorrido_em": evento.get("created_at") or payload.get("created_at"),
        "payload": payload,
    }


def _um(campos: Dict[str, List[str]], *nomes: str) -> Optional[str]:
    for n in nomes:
        v = campos.get(n)
        if v and v[0]:
            return v[0]
    return None


def _parse_form(campos: Dict[str, List[str]], raw: bytes) -> Dict[str, Any]:
    """Formato antigo. Chaves chegam achatadas: `partes[0][email]`, `documento[id]`.

    Sem id de evento — a idempotencia sai do hash do corpo. Duas assinaturas
    diferentes geram corpos diferentes, entao o hash separa; a MESMA reentrega
    tem o mesmo corpo e e barrada pela UNIQUE, que e o que se quer.
    """
    doc_id = _um(campos, "documento[id]", "document[id]", "id")
    doc_nome = _um(campos, "documento[nome]", "documento[name]", "document[name]", "nome", "name")

    # Ultimo signatario com `assinado` preenchido = quem acabou de agir.
    signatario, ocorrido = None, None
    for chave, valores in campos.items():
        if "[assinado]" in chave and "[data]" in chave and valores and valores[0]:
            prefixo = chave.split("[assinado]")[0]
            email = _um(campos, f"{prefixo}[email]")
            if email:
                signatario, ocorrido = email, valores[0]

    return {
        "formato": "form",
        "evento_chave": _hash_payload(raw),
        "tipo": "signature.accepted" if signatario else "document.updated",
        "documento_id": doc_id,
        "documento_nome": doc_nome,
        "signatario": signatario,
        "ocorrido_em": ocorrido,
        "payload": {k: (v[0] if len(v) == 1 else v) for k, v in campos.items()},
    }


# ──────────────────────────────── gravacao ──────────────────────────────────

def registrar_evento(evento: Dict[str, Any], verificado: bool) -> Dict[str, Any]:
    """Grava o evento (idempotente por `evento_chave`). Diz se era novidade."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO autentique_eventos
                   (evento_chave, tipo, documento_id, documento_nome, signatario,
                    ocorrido_em, formato, verificado, payload)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (evento_chave) DO NOTHING
               RETURNING id""",
            (
                evento["evento_chave"], evento.get("tipo"), evento.get("documento_id"),
                evento.get("documento_nome"), evento.get("signatario"),
                _ts(evento.get("ocorrido_em")), evento.get("formato"),
                verificado, json.dumps(evento.get("payload") or {}, default=str),
            ),
        )
        row = cur.fetchone()
        conn.commit()
    return {"novo": bool(row), "id": (dict(row)["id"] if row else None)}


def _ts(valor: Any) -> Optional[str]:
    """Normaliza timestamp textual. Devolve None em vez de estourar."""
    if not valor:
        return None
    s = str(valor).strip().replace("T", " ").replace("Z", "")
    return s[:26] or None


# ───────────────────────────── estado (GraphQL) ─────────────────────────────

async def buscar_documentos(limit: int = 60) -> List[Dict[str, Any]]:
    """Lista os documentos da conta. Levanta se a API recusar — quem chama trata."""
    chave = _api_key()
    if not chave:
        raise RuntimeError("AUTENTIQUE_API_KEY ausente")

    async with httpx.AsyncClient(timeout=40) as client:
        resp = await client.post(
            API_URL,
            headers={"Authorization": f"Bearer {chave}", "Content-Type": "application/json"},
            json={"query": _QUERY_DOCS, "variables": {"limit": limit, "page": 1}},
        )
    resp.raise_for_status()
    corpo = resp.json()
    if corpo.get("errors"):
        raise RuntimeError(f"Autentique GraphQL: {str(corpo['errors'])[:300]}")
    return ((corpo.get("data") or {}).get("documents") or {}).get("data") or []


def _resumo_signatarios(doc: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], int, Optional[str]]:
    sigs = []
    assinados = 0
    ultimo: Optional[str] = None
    for s in doc.get("signatures") or []:
        assinado_em = (s.get("signed") or {}).get("created_at")
        if assinado_em:
            assinados += 1
            if ultimo is None or assinado_em > ultimo:
                ultimo = assinado_em
        sigs.append({
            "nome": s.get("name"),
            "email": s.get("email"),
            "acao": (s.get("action") or {}).get("name"),
            "assinado_em": assinado_em,
            "visto_em": (s.get("viewed") or {}).get("created_at"),
            "recusado_em": (s.get("rejected") or {}).get("created_at"),
        })
    return sigs, assinados, ultimo


def upsert_documento(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Grava o ESTADO do documento. Devolve o que mudou em relacao ao que havia."""
    sigs, assinados, ultimo = _resumo_signatarios(doc)
    total = len(sigs)
    finalizado = ultimo if (total and assinados == total) else None

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT total_assinados, finalizado_em FROM autentique_documentos WHERE id = %s",
                    (doc["id"],))
        antes = cur.fetchone()
        antes_assinados = dict(antes)["total_assinados"] if antes else None

        cur.execute(
            """INSERT INTO autentique_documentos
                   (id, nome, criado_em_doc, total_signatarios, total_assinados,
                    finalizado_em, signatarios, ultimo_evento_em, atualizado_em)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, (now() AT TIME ZONE 'UTC'))
               ON CONFLICT (id) DO UPDATE SET
                   nome = EXCLUDED.nome,
                   total_signatarios = EXCLUDED.total_signatarios,
                   total_assinados = EXCLUDED.total_assinados,
                   finalizado_em = EXCLUDED.finalizado_em,
                   signatarios = EXCLUDED.signatarios,
                   ultimo_evento_em = EXCLUDED.ultimo_evento_em,
                   atualizado_em = (now() AT TIME ZONE 'UTC')""",
            (doc["id"], doc.get("name"), _ts(doc.get("created_at")), total, assinados,
             _ts(finalizado), json.dumps(sigs, default=str), _ts(ultimo)),
        )
        conn.commit()

    return {
        "id": doc["id"],
        "nome": doc.get("name"),
        "total": total,
        "assinados": assinados,
        "novo": antes is None,
        "avancou": antes is not None and antes_assinados is not None and assinados > antes_assinados,
        "concluiu": bool(finalizado) and (antes is None or (dict(antes).get("finalizado_em") is None)),
    }


async def sincronizar(limit: int = 60) -> Dict[str, Any]:
    """Reconcilia o estado com a API. NAO e o caminho principal — o webhook e.

    Existe porque webhook que falha, falha calado: se a entrega se perder (ou o
    endpoint nao chegar a ser registrado), sem isto o INTEL volta a nao saber de
    assinatura nenhuma e ninguem descobre. Chamada sob demanda, nao em cron.
    """
    docs = await buscar_documentos(limit=limit)
    resultado = {"documentos": len(docs), "novos": 0, "avancaram": 0, "concluiram": 0}
    mudancas: List[Dict[str, Any]] = []
    for d in docs:
        r = upsert_documento(d)
        resultado["novos"] += int(r["novo"])
        resultado["avancaram"] += int(r["avancou"])
        resultado["concluiram"] += int(r["concluiu"])
        if r["novo"] or r["avancou"] or r["concluiu"]:
            mudancas.append(r)
    resultado["mudancas"] = mudancas
    return resultado


# ────────────────────────────── notificacao ─────────────────────────────────

async def avisar(evento: Dict[str, Any], estado: Optional[Dict[str, Any]], verificado: bool) -> None:
    """Avisa o Renato. NAO fecha task: assinatura chega por um canal que, no
    formato antigo, nao vem assinado — evento nao verificado pode ser forjado, e
    fechar coisa a partir dele seria dar a estranho a caneta do sistema."""
    from services.notification_router import notify

    nome = evento.get("documento_nome") or (estado or {}).get("nome") or "documento"
    quem = evento.get("signatario") or "alguém"
    linhas = [f"*{nome}*", f"assinatura de {quem}"]
    if estado and estado.get("total"):
        linhas.append(f"progresso: {estado['assinados']}/{estado['total']}")
        if estado.get("concluiu"):
            linhas.append("✅ *todos assinaram*")
    if not verificado:
        linhas.append("_(evento sem assinatura HMAC conferida)_")

    await notify(
        "autentique",
        f"✍️ Autentique — {nome[:60]}",
        "\n".join(linhas),
        7,
        msg_type="autentique_assinatura",
        dedup=f"autentique:{evento.get('evento_chave')}",
    )


async def processar_webhook(
    raw_body: bytes,
    headers: Dict[str, str],
    verificado: bool = False,
) -> Dict[str, Any]:
    """Ponta a ponta do webhook, DEPOIS de autorizado pela rota.

    NUNCA levanta: webhook que devolve 500 vira reentrega e, em alguns
    provedores, endpoint desativado. A recusa de quem nao passa no porteiro e
    da rota (401), nao daqui."""
    try:
        evento = parse_webhook(raw_body, (headers or {}).get("content-type", ""))
        if not evento:
            log.warning("autentique: payload nao reconhecido (%d bytes)", len(raw_body or b""))
            return {"ok": False, "motivo": "payload_nao_reconhecido"}

        gravado = registrar_evento(evento, verificado)
        if not gravado["novo"]:
            return {"ok": True, "duplicado": True, "evento": evento["evento_chave"]}

        estado = None
        try:
            docs = await buscar_documentos()
            alvo = next((d for d in docs if d.get("id") == evento.get("documento_id")), None)
            if alvo:
                estado = upsert_documento(alvo)
        except Exception:
            # A API pode estar fora; o evento JA esta gravado e o aviso sai
            # mesmo sem o estado. Perder o aviso por causa do enriquecimento
            # seria trocar o canal cego por outro.
            log.exception("autentique: falha buscando estado do documento")

        try:
            await avisar(evento, estado, verificado)
        except Exception:
            log.exception("autentique: falha avisando o Renato")

        return {"ok": True, "evento": evento["evento_chave"], "tipo": evento.get("tipo"),
                "verificado": verificado, "estado": estado}
    except Exception as e:
        log.exception("autentique: webhook falhou")
        return {"ok": False, "erro": f"{type(e).__name__}: {e}"[:200]}
