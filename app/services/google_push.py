"""Write-back INTEL → Google Contacts: a ficha criada aqui vai pro celular dele.

POR QUE EXISTE (07/08/2026, pedido aberto desde 03/08). O sync de contatos é de
MÃO ÚNICA — Google → INTEL. Ficha criada no INTEL não sobe, e o celular lê a
agenda do Google: por isso o número aparece sem nome no WhatsApp dele, mesmo
depois de a pessoa já estar cadastrada aqui. Foi o que aconteceu com o Gui Nosé
(#26692) e, em 07/08, com os seis contatos recuperados de vCards compartilhados.

A INFRAESTRUTURA JÁ EXISTIA E MESMO ASSIM NÃO FUNCIONAVA. `create_google_contact`
está pronta há tempo e `propagate_contact_to_google` a chama. Faltavam as duas
peças sem as quais isso vira uma fábrica de duplicatas:

  1. DEDUP CONTRA O GOOGLE. Não havia busca. Empurrar seis fichas sem procurar
     antes cria seis duplicatas lá — e elas voltam pelo sync de entrada,
     multiplicadas. Foi assim que se chegou às 293 duplicatas limpas em 06/08.
  2. GRAVAR O `resourceName` DE VOLTA. `propagate_contact_to_google` cria no
     Google e não registra o id no INTEL. Sem isso a próxima execução não sabe
     que já subiu e cria outra vez — a duplicata nasce do próprio mecanismo, sem
     ninguém errar nada.

Então este módulo faz o ciclo inteiro e é IDEMPOTENTE por construção: procura,
vincula se achou, cria se não achou, e sempre grava o id.

O casamento é por TELEFONE CANÔNICO (só dígitos, últimos 8), nunca por nome:
homônimo é comum e "Ana Maria Piccino" existe quatro vezes no CRM
([[reference_telefone_br_normalizacao]]).
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional

import httpx

from database import get_db

logger = logging.getLogger(__name__)

GOOGLE_PEOPLE_API = "https://people.googleapis.com/v1"


def _digitos(v: str) -> str:
    return re.sub(r"\D", "", v or "")


def _chave_tel(v: str) -> str:
    """Últimos 8 dígitos de número COM DDD (10+ dígitos).

    O piso de 10 é o outro lado do conserto de 07/08: telefone sem DDD
    ("7678-5627") é real e continua na ficha, mas casar por ele junta gente de
    cidades diferentes que por acaso tem o mesmo número local. Dado incompleto
    serve pra ligar pra pessoa, não pra afirmar que duas fichas sao a mesma.
    """
    d = _digitos(v)
    return d[-8:] if len(d) >= 10 else ""


async def indice_telefones(token: str) -> Dict[str, Dict]:
    """{últimos 8 dígitos → pessoa} de TODA a agenda do Google.

    ⚠️ POR QUE NÃO `people:searchContacts` (medido em 07/08, antes de escrever
    qualquer coisa lá): buscar pelo telefone da Daniela — que está na agenda —
    devolveu **0 resultados**; buscar pelo NOME dela devolveu 2. O
    `searchContacts` não casa por telefone do jeito que a dedup precisa, e um
    dedup que sempre responde "não existe" não é dedup: é o gerador de duplicata.

    Baixar a agenda inteira uma vez e indexar localmente custa uma chamada e dá
    a resposta certa. A chave são os últimos 8 dígitos — o que sobrevive a DDI,
    DDD e ao nono dígito ([[reference_telefone_br_normalizacao]]).

    Levanta exceção se a listagem falhar: sem índice NÃO se cria nada, porque aí
    "não achei" significaria só "não olhei".
    """
    import integrations.google_contacts as gc
    pessoas = await gc.fetch_all_contacts(token)
    if pessoas is None:
        raise RuntimeError("fetch_all_contacts devolveu None — sem índice, não se cria nada")
    idx: Dict[str, Dict] = {}
    for p in pessoas:
        for pn in (p.get("phoneNumbers") or []):
            k = _chave_tel(pn.get("value") or "")
            if k:
                idx.setdefault(k, p)
    return idx


async def push_contato(contact_id: int, conta_email: str, idx: Dict[str, Dict],
                       aplicar: bool = False) -> Dict:
    """Sobe UMA ficha pro Google, ou vincula se ela já estiver lá.

    `aplicar=False` (padrão) faz dry-run: diz o que faria sem escrever nada, nem
    no Google nem aqui. Escrita em agenda de terceiro não é coisa pra descobrir
    depois que já aconteceu.
    """
    import integrations.google_contacts as gc

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""SELECT id, nome, telefones, emails, empresa, cargo, contexto,
                              google_contact_id
                         FROM contacts WHERE id = %s""", (contact_id,))
        ficha = cur.fetchone()
    if not ficha:
        return {"contact_id": contact_id, "status": "ficha_inexistente"}
    ficha = dict(ficha)

    if ficha.get("google_contact_id"):
        return {"contact_id": contact_id, "nome": ficha["nome"], "status": "ja_vinculada",
                "google_id": ficha["google_contact_id"]}

    tels = ficha.get("telefones") or []
    numeros = [t.get("number") for t in tels if isinstance(t, dict) and t.get("number")]
    if not numeros:
        return {"contact_id": contact_id, "nome": ficha["nome"], "status": "sem_telefone",
                "nota": "sem telefone não há como deduplicar contra o Google — não sobe"}

    token = await gc.get_valid_token(conta_email)
    if not token:
        return {"contact_id": contact_id, "status": "sem_token", "conta": conta_email}

    # 1) JÁ ESTÁ LÁ? Consulta o índice da agenda inteira (ver `indice_telefones`).
    achado = next((idx[k] for k in (_chave_tel(n) for n in numeros) if k and k in idx), None)

    if achado:
        rname = (achado.get("resourceName") or "").replace("people/", "")
        nome_g = ((achado.get("names") or [{}])[0].get("displayName")) or "(sem nome)"
        if aplicar and rname:
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("""UPDATE contacts SET google_contact_id = %s, atualizado_em = now()
                                WHERE id = %s AND google_contact_id IS NULL""", (rname, contact_id))
                conn.commit()
        return {"contact_id": contact_id, "nome": ficha["nome"], "status": "vinculada",
                "google_id": rname, "nome_no_google": nome_g,
                "nota": "já existia no Google — vinculei em vez de criar"}

    # 2) NÃO ESTÁ: cria e grava o id de volta na MESMA execução.
    if not aplicar:
        return {"contact_id": contact_id, "nome": ficha["nome"], "status": "criaria",
                "telefone": numeros[0]}

    novo = await gc.create_google_contact(token, ficha)
    if not novo:
        return {"contact_id": contact_id, "nome": ficha["nome"], "status": "erro_ao_criar"}
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""UPDATE contacts SET google_contact_id = %s, atualizado_em = now()
                        WHERE id = %s""", (novo, contact_id))
        conn.commit()
    return {"contact_id": contact_id, "nome": ficha["nome"], "status": "criada",
            "google_id": novo}


async def push_varios(ids: List[int], conta_email: str, aplicar: bool = False) -> Dict:
    """Lote. Para no primeiro erro de BUSCA — se o Google não está respondendo,
    seguir criaria contatos sem saber se já existem."""
    import integrations.google_contacts as gc
    token = await gc.get_valid_token(conta_email)
    if not token:
        return {"conta": conta_email, "erro": "sem token"}
    # O índice é montado UMA vez pro lote — e se falhar, nada sobe.
    idx = await indice_telefones(token)
    out = []
    for cid in ids:
        try:
            out.append(await push_contato(cid, conta_email, idx, aplicar=aplicar))
        except Exception as e:
            out.append({"contact_id": cid, "status": "erro", "erro": str(e)[:160]})
            logger.error("google_push: falhou em %s — abortando lote", cid)
            break
    return {"conta": conta_email, "aplicar": aplicar,
            "contatos_no_google": len(idx), "resultados": out}
