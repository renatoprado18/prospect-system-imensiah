"""
Social Groups Service - Mapeia grupos de WhatsApp como circulos sociais

Usa cache no banco para evitar chamadas lentas à Evolution API.
Sync em background (cron) atualiza o cache diariamente.
"""
import os
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import httpx

from database import get_db
from services.contact_identity import find_contact_by_phone

logger = logging.getLogger(__name__)


async def get_group_info(group_jid: str) -> Optional[Dict]:
    """Busca info do grupo via Evolution API."""
    base_url = os.getenv('EVOLUTION_API_URL', '')
    api_key = os.getenv('EVOLUTION_API_KEY', '')
    instance = os.getenv('EVOLUTION_INSTANCE', 'default')

    if not base_url:
        return None

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f'{base_url}/group/findGroupInfos/{instance}?groupJid={group_jid}',
                headers={'apikey': api_key}
            )
            if resp.status_code == 200:
                return resp.json()
    except Exception as e:
        logger.warning(f"Erro ao buscar info do grupo {group_jid}: {e}")
    return None


def _cross_phone_with_contacts(phones: List[str]) -> List[Dict]:
    """Cruza lista de telefones com contatos INTEL."""
    if not phones:
        return []

    known = []
    with get_db() as conn:
        cursor = conn.cursor()
        for phone in phones:
            # So digitos dos dois lados — o participante vem cru do WhatsApp e
            # o numero na base pode estar formatado pelo Google. Com o LIKE
            # anterior, participante conhecido com numero formatado nao
            # aparecia como conhecido.
            contact = find_contact_by_phone(
                cursor, phone,
                columns="id, nome, empresa, cargo, circulo, health_score, foto_url",
            )
            if contact:
                known.append(contact)
    return known


async def _fetch_labels_map(client, base_url, api_key, instance) -> dict:
    """Busca labels do WA Business e mapeia id -> nome."""
    try:
        resp = await client.get(
            f'{base_url}/label/findLabels/{instance}',
            headers={'apikey': api_key}
        )
        if resp.status_code == 200:
            return {l['id']: l['name'] for l in resp.json()}
    except Exception as e:
        logger.warning(f"Erro ao buscar labels: {e}")
    return {}


async def _fetch_chat_labels(client, base_url, api_key, instance, jid) -> list:
    """Busca labels de um chat especifico via findChatByRemoteJid."""
    try:
        resp = await client.get(
            f'{base_url}/chat/findChatByRemoteJid/{instance}?remoteJid={jid}',
            headers={'apikey': api_key},
            timeout=5.0
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get('labels') or []
    except Exception:
        pass
    return []


async def sync_all_groups_cache(
    limit: Optional[int] = None,
    offset: int = 0,
) -> Dict:
    """Sincroniza cache de grupos.

    Pagination: quando limit eh fornecido, processa apenas group_jids[offset:offset+limit]
    e retorna `next_offset` (cyclic) + `more_pages`. Sem limit, processa todos.
    """
    base_url = os.getenv('EVOLUTION_API_URL', '')
    api_key = os.getenv('EVOLUTION_API_KEY', '')
    instance = os.getenv('EVOLUTION_INSTANCE', 'default')

    if not base_url:
        return {"error": "Evolution API nao configurada"}

    results = {"synced": 0, "errors": 0, "total_groups": 0, "next_offset": 0, "more_pages": False}

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            # 1. Buscar labels map (id -> nome)
            labels_map = await _fetch_labels_map(client, base_url, api_key, instance)

            # 2. Buscar todos os chats
            resp = await client.post(
                f'{base_url}/chat/findChats/{instance}',
                headers={'apikey': api_key, 'Content-Type': 'application/json'},
                json={}
            )
            chats = resp.json()
            all_group_jids = []
            named = {}

            for c in chats:
                jid = c.get('remoteJid', '')
                name = c.get('pushName', '')
                if '@g.us' in jid:
                    all_group_jids.append(jid)
                    if name:
                        named[jid] = name

            total = len(all_group_jids)
            results["total_groups"] = total

            # Aplicar paginacao
            if limit is not None and limit > 0:
                group_jids = all_group_jids[offset:offset + limit]
                next_off = offset + limit
                if next_off >= total:
                    results["next_offset"] = 0
                    results["more_pages"] = False
                else:
                    results["next_offset"] = next_off
                    results["more_pages"] = True
            else:
                group_jids = all_group_jids

            # 3. Buscar metadados e labels de cada grupo
            import asyncio as aio

            async def fetch_group(jid):
                try:
                    r = await client.get(
                        f'{base_url}/group/findGroupInfos/{instance}?groupJid={jid}',
                        headers={'apikey': api_key},
                        timeout=10.0
                    )
                    if r.status_code == 200:
                        info = r.json()
                        name = info.get('subject', named.get(jid, jid))
                        participants = info.get('participants', [])
                        phones = []
                        for p in participants:
                            phone_jid = p.get('phoneNumber', p.get('id', ''))
                            phone = phone_jid.replace('@s.whatsapp.net', '').replace('@lid', '')
                            if phone and len(phone) > 8:
                                phones.append(phone)

                        # Buscar labels do chat
                        label_ids = await _fetch_chat_labels(client, base_url, api_key, instance, jid)
                        labels = [{'id': lid, 'name': labels_map.get(lid, lid)} for lid in label_ids]

                        return {
                            'jid': jid,
                            'name': name,
                            'total': info.get('size', len(participants)),
                            'phones': phones,
                            'labels': labels
                        }
                except Exception:
                    pass
                if jid in named:
                    return {'jid': jid, 'name': named[jid], 'total': 0, 'phones': [], 'labels': []}
                return None

            # Processar em batches de 10
            all_groups = []
            for i in range(0, len(group_jids), 10):
                batch = group_jids[i:i+10]
                batch_results = await aio.gather(*[fetch_group(jid) for jid in batch])
                all_groups.extend([r for r in batch_results if r])

        # 4. Cruzar telefones com contatos e salvar cache.
        # Why to_thread: get_db()+_cross_phone_with_contacts+UPDATE em loop de N grupos
        # eh sync e bloqueava o event loop dentro do async. wait_for nao conseguia
        # cancelar — daily-sync timeoutava o step. Wrap em thread libera o loop.
        def _persist():
            synced = 0
            errors = 0
            with get_db() as conn:
                cursor = conn.cursor()
                for g in all_groups:
                    try:
                        known = _cross_phone_with_contacts(g['phones'])
                        known_ids = [c['id'] for c in known]
                        healths = [c['health_score'] for c in known if c.get('health_score') is not None]
                        health_medio = round(sum(healths) / len(healths)) if healths else None

                        cursor.execute("""
                            INSERT INTO social_groups_cache (group_jid, group_name, total_participants,
                                participants_phones, known_contact_ids, known_count, health_medio, labels, last_synced_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                            ON CONFLICT (group_jid) DO UPDATE SET
                                group_name = EXCLUDED.group_name,
                                total_participants = EXCLUDED.total_participants,
                                participants_phones = EXCLUDED.participants_phones,
                                known_contact_ids = EXCLUDED.known_contact_ids,
                                known_count = EXCLUDED.known_count,
                                health_medio = EXCLUDED.health_medio,
                                labels = EXCLUDED.labels,
                                last_synced_at = NOW()
                        """, (
                            g['jid'], g['name'], g['total'],
                            json.dumps(g['phones']), json.dumps(known_ids),
                            len(known), health_medio, json.dumps(g['labels'])
                        ))
                        synced += 1
                    except Exception as e:
                        errors += 1
                        logger.warning(f"Erro ao cachear grupo {g.get('name')}: {e}")
                conn.commit()
            return synced, errors

        synced, errors = await aio.to_thread(_persist)
        results["synced"] = synced
        results["errors"] = errors

        # Label-driven digest: grupo etiquetado "Digest" no WhatsApp entra no digest (auto-sync).
        # Kill-switch: DIGEST_LABEL_AUTOSYNC=on. Só LIGA (nunca desliga) por seguranca.
        # (usa o `os` importado no topo do módulo; import local aqui shadowava `os`
        #  como variável local → UnboundLocalError nas linhas 102-104. Removido 15/07.)
        if os.getenv("DIGEST_LABEL_AUTOSYNC", "off").strip().lower() == "on":
            try:
                results["digest_autosync"] = await aio.to_thread(_apply_digest_label_autosync)
            except Exception as e:
                logger.warning(f"digest label autosync falhou: {e}")

    except Exception as e:
        logger.error(f"Erro no sync de grupos: {e}")
        results["error"] = str(e)

    return results


def _apply_digest_label_autosync() -> int:
    """Liga sync_enabled para grupos com o label 'Digest' no WhatsApp. Retorna quantos ligou.
    Add-only: nunca DESLIGA (remocao fica manual pra nao cair grupo por glitch de label)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE social_groups_cache
            SET sync_enabled = TRUE
            WHERE sync_enabled = FALSE
              AND labels IS NOT NULL
              AND jsonb_typeof(labels) = 'array'
              AND EXISTS (
                  SELECT 1 FROM jsonb_array_elements(labels) l
                  WHERE l->>'name' ILIKE 'Digest'
              )
        """)
        n = cursor.rowcount
        conn.commit()
        return n


def list_cached_groups() -> List[Dict]:
    """Lista grupos do cache (rapido, sem chamada à Evolution API)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT group_jid, group_name, total_participants, known_count, health_medio,
                   labels, sync_enabled, last_synced_at
            FROM social_groups_cache
            WHERE group_name IS NOT NULL
            ORDER BY sync_enabled DESC, group_name
        """)
        rows = []
        for r in cursor.fetchall():
            row = dict(r)
            if isinstance(row.get('labels'), str):
                row['labels'] = json.loads(row['labels'])
            rows.append(row)
        return rows


def get_cached_group_detail(group_jid: str) -> Optional[Dict]:
    """Detalhe de um grupo do cache com contatos conhecidos."""
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM social_groups_cache WHERE group_jid = %s", (group_jid,))
        group = cursor.fetchone()
        if not group:
            return None
        group = dict(group)

        # Buscar dados dos contatos conhecidos
        known_ids = group.get('known_contact_ids', [])
        if isinstance(known_ids, str):
            known_ids = json.loads(known_ids)

        members = []
        if known_ids:
            cursor.execute("""
                SELECT id, nome, empresa, cargo, circulo, health_score, foto_url, telefones, emails
                FROM contacts WHERE id = ANY(%s)
                ORDER BY nome
            """, (known_ids,))
            for row in cursor.fetchall():
                c = dict(row)
                telefones = c.get('telefones', [])
                if isinstance(telefones, str):
                    telefones = json.loads(telefones) if telefones else []
                phone = ''
                for t in telefones:
                    if t.get('number'):
                        phone = t['number']
                        break
                c['phone'] = phone
                members.append(c)

        needs_attention = [m for m in members if (m.get('health_score') or 100) < 50]

        return {
            'group_jid': group['group_jid'],
            'name': group['group_name'],
            'total_participants': group['total_participants'],
            'known_members': len(members),
            'unknown_members': group['total_participants'] - len(members),
            'health_medio': group.get('health_medio'),
            'needs_attention': len(needs_attention),
            'members': sorted(members, key=lambda m: m.get('nome', '')),
            'attention_list': sorted(needs_attention, key=lambda m: m.get('health_score', 0)),
            'last_synced': group.get('last_synced_at')
        }


def toggle_group_sync(group_jid: str, enabled: bool) -> bool:
    """Ativa/desativa sincronizacao de um grupo."""
    with get_db() as conn:
        cursor = conn.cursor()

        # Upsert: create if not exists, update if exists
        cursor.execute("""
            INSERT INTO social_groups_cache (group_jid, group_name, sync_enabled)
            VALUES (%s, %s, %s)
            ON CONFLICT (group_jid) DO UPDATE SET sync_enabled = %s
        """, (group_jid, group_jid, enabled, enabled))
        conn.commit()
        return True


def get_sync_enabled_groups() -> List[Dict]:
    """Retorna grupos marcados para sincronizacao."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT group_jid, group_name FROM social_groups_cache
            WHERE sync_enabled = TRUE AND group_name IS NOT NULL
            ORDER BY group_name
        """)
        return [dict(r) for r in cursor.fetchall()]


async def list_all_social_groups() -> List[Dict]:
    """Lista grupos — do cache completo se disponivel, senao da API + sync flags."""
    cached = list_cached_groups()

    # Cache is "complete" if it has >10 groups with real names (not JIDs)
    real_cache = [g for g in cached if g['group_name'] and '@' not in g['group_name']]

    if len(real_cache) > 10:
        return [{'jid': g['group_jid'], 'name': g['group_name'],
                 'known': g['known_count'], 'total': g['total_participants'],
                 'health': g.get('health_medio'),
                 'labels': g.get('labels', []),
                 'sync_enabled': g.get('sync_enabled', False)} for g in cached
                if g['group_name'] and '@' not in g['group_name']]

    # Fallback: API + merge sync flags from cache
    api_groups = await _fetch_groups_from_api()
    sync_map = {g['group_jid']: g.get('sync_enabled', False) for g in cached}
    for g in api_groups:
        g['sync_enabled'] = sync_map.get(g['jid'], False)
    return api_groups


async def refresh_groups_basic(full: bool = False) -> Dict:
    """Refresh RAPIDO do inventario de grupos: so jid+nome, SEM enrichment.

    Separa "aparecer" (barato) de "enriquecer" (caro). Um grupo recem-criado aparece na
    pagina em segundos, em vez de esperar o cursor ciclico do cron `run-social-groups`
    (lotes de SOCIAL_GROUPS_BATCH) chegar nele — participantes/health/labels ficam pro cron.

    Duas fontes (trade-off velocidade × cobertura):
    - `full=False` (padrao, ~2-4s): `findChats` — so grupos COM atividade (>=1 msg). Cobre o
      caso comum: grupo novo + saudacao de abertura ja aparece na hora.
    - `full=True` (~2min): `fetchAllGroups` da Evolution — superset que inclui grupos MUDOS
      (sem 1a msg). Endpoint pesado (varre todos os grupos); so vale quando um grupo
      recem-criado ainda sem nenhuma mensagem nao apareceu no refresh rapido. Fallback pro
      findChats se falhar/vier vazio.

    UPSERT so do registro basico. ON CONFLICT atualiza APENAS o nome (preserva
    participantes/health/labels/sync_enabled ja cacheados). As fontes podem devolver grupo
    com `name=None` (sem nome e nao resolvido); esse NAO e gravado — inserir o jid como
    nome recria o placeholder `group_name = group_jid` do `toggle_group_sync`, que esconde o
    grupo da lista. Mas ele sai CONTADO em `sem_nome`, com os jids, que e a diferenca que
    importa: antes ele desaparecia e `refreshed` dizia que estava tudo certo.

    ⚠️ `refreshed` NAO E COBERTURA (10/11). Medido contra a Evolution: `findChats` devolve
    136 grupos, 60 com `pushName: null`, e a resposta dizia `refreshed: 75`. Agora a resposta
    traz `vistos` (o denominador), `resolvidos_por_info` e `sem_nome` — sem os tres, "75"
    se le como sucesso e e o numero errado ([[feedback_mecanismo_que_nao_mede_o_denominador]]).
    """
    if full:
        groups = await _fetch_all_groups_evolution()  # pesado (~2min), inclui mudos
        source = "fetchAllGroups"
        if not groups:
            groups = await _fetch_groups_from_api()  # fallback: findChats
            source = "findChats(fallback)"
    else:
        groups = await _fetch_groups_from_api()  # findChats: rapido, ja filtra @g.us + pushName
        source = "findChats"
    if not groups:
        return {"refreshed": 0, "vistos": 0,
                "note": "nenhum grupo retornado pela API", "source": source}

    sem_nome = [g['jid'] for g in groups if not g.get('name')]
    resolvidos = sum(1 for g in groups if g.get('resolvido_por_info'))

    def _persist():
        upserted = 0
        with get_db() as conn:
            cursor = conn.cursor()
            for g in groups:
                jid, name = g.get('jid'), g.get('name')
                if not jid or not name:
                    continue
                cursor.execute("""
                    INSERT INTO social_groups_cache (group_jid, group_name)
                    VALUES (%s, %s)
                    ON CONFLICT (group_jid) DO UPDATE SET group_name = EXCLUDED.group_name
                """, (jid, name))
                upserted += 1
            conn.commit()
        return upserted

    import asyncio as aio
    upserted = await aio.to_thread(_persist)
    out = {
        "refreshed": upserted,
        # O DENOMINADOR. `refreshed` sozinho nao diz cobertura: eram 75 de 136.
        "vistos": len(groups),
        "resolvidos_por_info": resolvidos,
        "sem_nome": len(sem_nome),
        "source": source,
        "total_from_api": len(groups),
    }
    if sem_nome:
        # Os jids VAO na resposta. Contagem sem a lista nao permite descobrir se
        # e grupo novo (acionavel) ou JID morto de grupo que o numero deixou —
        # em 10/11 os 6 eram todos 404 no findGroupInfos, mas isso se descobre
        # olhando, nao supondo.
        out["sem_nome_jids"] = sem_nome[:50]
        out["nota_sem_nome"] = (
            f"{len(sem_nome)} de {len(groups)} grupos nao tem nome e nao foram "
            "resolvidos pelo findGroupInfos — NAO entraram no cache. 404 aqui "
            "costuma ser grupo que o numero deixou ou JID de comunidade."
        )
    return out


# --- Refresh completo em BACKGROUND (Railway always-on) ------------------------
# `full=True` bate no fetchAllGroups (~2min) e travava a request HTTP. No Railway o
# processo eh always-on, entao `asyncio.create_task` sobrevive pos-response (ao
# contrario do Vercel serverless, onde a task era cortada). O endpoint dispara isto
# e retorna 202 na hora; o guard evita disparos concorrentes duplicados.
_full_refresh_running = False


def is_full_refresh_running() -> bool:
    """True se um refresh completo (fetchAllGroups) esta em andamento."""
    return _full_refresh_running


def try_acquire_full_refresh() -> bool:
    """Check-and-set atomico (event loop single-thread) do guard anti-duplicata.
    Retorna True se conseguiu a vaga; False se ja tem um refresh completo rodando.
    Quem adquire DEVE garantir que refresh_groups_full_background() rode (ele libera)."""
    global _full_refresh_running
    if _full_refresh_running:
        return False
    _full_refresh_running = True
    return True


async def refresh_groups_full_background() -> Dict:
    """Roda refresh_groups_basic(full=True) em background e libera o guard no fim.
    Pressupoe que try_acquire_full_refresh() ja adquiriu a vaga. Chamado via
    asyncio.create_task (fire-and-forget) — nunca propaga excecao pro caller."""
    global _full_refresh_running
    try:
        result = await refresh_groups_basic(full=True)
        logger.info(f"refresh completo de grupos concluido: {result}")
        return result
    except Exception as e:
        logger.error(f"refresh completo de grupos falhou: {e}")
        return {"error": str(e)}
    finally:
        _full_refresh_running = False


import unicodedata as _ud
import re as _re

_PROJ_STOP = {"para", "com", "the", "and", "dos", "das", "por", "pro", "sao", "grupo"}


def _match_tokens(s: str) -> list:
    """Tokens normalizados pra match de nome: sem acento, sem emoji, lower, >=3 chars, sem conectores."""
    if not s:
        return []
    ascii_s = _ud.normalize("NFKD", s).encode("ascii", "ignore").decode()  # tira acento E emoji
    return [t for t in _re.findall(r"[a-z0-9]{3,}", ascii_s.lower()) if t not in _PROJ_STOP]


def suggest_groups_for_project(project_name: str, exclude_jids=None, limit: int = 5, min_score: int = 4) -> List[Dict]:
    """Sugere grupos WA pra vincular a um projeto, por match do nome.

    Explora o padrao ratificado `<emoji> <Projeto> — <Contraparte>` (nome do projeto lidera
    o do grupo), mas tolera nomes de projeto antigos fora do padrao. Casa TODOS os tokens do
    nome do projeto (unaccent, sem emoji) contra o nome de cada grupo, pesando cada token
    casado pelo comprimento (nomes proprios longos = mais distintivos) e dobrando o peso dos
    tokens do "core" do projeto (1º segmento antes do travessao — o nome-lider). `min_score`
    corta ruido (token curto isolado). Exclui grupos ja vinculados e nomes-placeholder.
    Read-only — so sugere, nao vincula. Ver [[reference_naming_grupos_projeto]].
    """
    exclude = set(exclude_jids or [])
    all_toks = _match_tokens(project_name)
    if not all_toks:
        return []
    core = project_name.split("—")[0] if "—" in project_name else project_name
    core_toks = set(_match_tokens(core))
    # peso por token: comprimento, dobrado se está no nome-líder do projeto
    weights = {t: (len(t) * (2 if t in core_toks else 1)) for t in all_toks}

    out = []
    for g in list_cached_groups():
        jid = g["group_jid"]
        name = g.get("group_name") or ""
        if jid in exclude or "@" in name or not name:
            continue
        gtoks = set(_match_tokens(name))
        matched = [t for t in weights if t in gtoks]
        if not matched:
            continue
        score = sum(weights[t] for t in matched)
        if score < min_score:
            continue
        out.append({
            "jid": jid, "name": name, "score": score,
            "matched": sorted(matched, key=len, reverse=True),
            "sync_enabled": g.get("sync_enabled", False),
        })
    out.sort(key=lambda r: -r["score"])
    return out[:limit]


# ===== GRUPO SEM NOME NAO SE DESCARTA EM SILENCIO (10/11/26) =====
#
# O DEFEITO. Os dois caminhos de inventario filtravam por nome
# (`and c.get('pushName')` no findChats, `and name` no fetchAllGroups) e o que nao
# tinha nome SUMIA — sem log, sem contagem, sem erro. MEDIDO em 10/11 contra a
# Evolution ao vivo: o `findChats` devolve **136 grupos, 60 deles com
# `pushName: null` (44%)**, e o refresh respondia `refreshed: 75`, que se le como
# "deu tudo certo". Foi assim que a "Fazenda Jaboticabeiras" nao existia no cache
# no dia do dossie.
#
# ⚠️ O QUE A MEDICAO MOSTROU, e que muda o tamanho do conserto: dos 60 sem nome,
# **54 ja estavam no cache com nome real** (de quando tinham `pushName`, ou via
# fetchAllGroups) e os outros **6 dao 404 no `findGroupInfos`** — nao sao
# resolviveis (numero saiu do grupo, JID de comunidade). Ou seja: hoje isto
# recupera ZERO grupo. O que conserta e o MECANISMO (grupo novo sem nome passa a
# aparecer) e, principalmente, a MEDICAO — `refreshed: 75` era o numero errado e
# nao havia como notar.
#
# Por isso a regra aqui e: resolver o que da, e o que NAO da sai CONTADO na
# resposta, nunca engolido ([[feedback_guarda_abstencao_vira_fabrica]]).
_INFO_CONC = 8          # findGroupInfos em paralelo
_INFO_CAP = 200         # teto declarado: acima disto, o excedente vai na resposta


async def _resolver_nomes(jids: List[str]) -> Dict[str, Optional[str]]:
    """`{jid: subject}` via findGroupInfos, em paralelo. Valor None = nao resolveu.

    Nunca levanta: grupo que falha entra como None e quem chama o CONTA. O
    semaforo existe porque sao ~60 chamadas de ~1s — sequenciais poriam um minuto
    no relogio de um refresh que promete 2-4s.
    """
    import asyncio as aio
    if not jids:
        return {}
    sem = aio.Semaphore(_INFO_CONC)

    async def _um(jid: str):
        async with sem:
            try:
                info = await get_group_info(jid)
            except Exception as e:
                logger.warning("findGroupInfos falhou em %s: %s", jid, e)
                return jid, None
            subject = (info or {}).get('subject') if isinstance(info, dict) else None
            return jid, (subject or None)

    pares = await aio.gather(*[_um(j) for j in jids[:_INFO_CAP]])
    out = dict(pares)
    for j in jids[_INFO_CAP:]:      # teto NUNCA silencioso
        out[j] = None
    return out


async def _fetch_groups_from_api() -> List[Dict]:
    """Busca grupos da Evolution API via findChats (rapido, ~2s).

    Devolve TODOS os `@g.us` vistos. `name=None` significa "sem nome e nao
    resolvido" — quem chama precisa distinguir isso de "nao existe", que era
    justamente o que o filtro antigo apagava.
    """
    base_url = os.getenv('EVOLUTION_API_URL', '')
    api_key = os.getenv('EVOLUTION_API_KEY', '')
    instance = os.getenv('EVOLUTION_INSTANCE', 'default')

    if not base_url:
        return []

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f'{base_url}/chat/findChats/{instance}',
                headers={'apikey': api_key, 'Content-Type': 'application/json'},
                json={}
            )
            chats = resp.json()
    except Exception as e:
        logger.error(f"Erro ao listar grupos: {e}")
        return []

    import asyncio as aio
    vistos = [c for c in chats
              if isinstance(c, dict) and '@g.us' in (c.get('remoteJid') or '')]

    # ORDEM DE RESOLUCAO: pushName -> cache -> findGroupInfos. O cache no meio e o
    # que mantem a promessa de 2-4s do refresh rapido.
    #
    # A primeira versao disto chamava findGroupInfos para TODOS os 60 sem
    # pushName e levava **13,4s** — 3 a 6 vezes o prometido. Medido: dos 60, 26
    # resolvem e **34 dao 404** (grupo que o numero deixou, JID de comunidade), e
    # esses 34 seriam marteladas desperdicadas a CADA refresh, para sempre. Com o
    # cache no meio, a chamada ao vivo sobra so para o que ninguem conhece ainda —
    # que e exatamente o grupo novo, o caso que esta frente existe pra consertar.
    #
    # ⚠️ TRADE-OFF DECLARADO: grupo RENOMEADO cujo `pushName` vem null mantem o
    # nome velho do cache, porque a chamada ao vivo nao acontece para ele. O nome
    # novo entra pelo `full=True` (fetchAllGroups, que devolve `subject`) ou pelo
    # cron de enriquecimento. Trocar isso por 13s em todo refresh nao vale.
    com_nome_no_cache = await aio.to_thread(_jids_nomeados_no_cache)
    faltam = [c['remoteJid'] for c in vistos
              if not c.get('pushName') and c['remoteJid'] not in com_nome_no_cache]
    resolvidos = await _resolver_nomes(faltam)
    if faltam:
        logger.info("findChats: %d de %d grupos sem pushName e fora do cache; "
                    "findGroupInfos resolveu %d", len(faltam), len(vistos),
                    sum(1 for v in resolvidos.values() if v))

    groups = []
    for c in vistos:
        jid = c['remoteJid']
        nome = c.get('pushName') or resolvidos.get(jid) or com_nome_no_cache.get(jid)
        groups.append({'jid': jid, 'name': nome or None,
                       'resolvido_por_info': bool(not c.get('pushName')
                                                  and resolvidos.get(jid)),
                       'total': 0, 'labels': [], 'sync_enabled': False})
    return sorted(groups, key=lambda g: (g['name'] is None, g['name'] or g['jid']))


def _jids_nomeados_no_cache() -> Dict[str, str]:
    """`{jid: nome}` dos grupos que o cache JA conhece por um nome de verdade.

    Exclui `group_name = group_jid`, o placeholder que o `toggle_group_sync`
    deixa: tratar o jid como nome aqui faria o grupo parecer resolvido e ele
    continuaria escondido da lista, que e o defeito que o placeholder causa.
    """
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""SELECT group_jid, group_name FROM social_groups_cache
                            WHERE group_name IS NOT NULL AND btrim(group_name) <> ''
                              AND group_name <> group_jid""")
            return {r['group_jid']: r['group_name'] for r in cur.fetchall()}
    except Exception as e:
        # Sem cache a resolucao fica mais lenta, nao errada: todos os sem-nome
        # caem no findGroupInfos. Degradar para o caminho correto-e-lento e o
        # certo; degradar para "descarta" era o defeito.
        logger.warning("cache de nomes de grupo indisponivel (%s) — "
                       "findGroupInfos vai ser chamado para todos os sem-nome", e)
        return {}


async def _fetch_all_groups_evolution() -> List[Dict]:
    """Busca TODOS os grupos que o numero participa via Evolution fetchAllGroups.

    Superset do `findChats`: inclui grupos MUDOS (sem 1a mensagem), que o Baileys
    so registra como chat depois da 1a msg. Assim um grupo recem-criado aparece na
    pagina ANTES da 1a mensagem — elimina o contorno "criar grupo -> 1a msg -> refresh".

    Endpoint mais pesado (varre todos os grupos, pode levar dezenas de segundos),
    por isso timeout generoso. Retorna [{'jid': <id>, 'name': <subject>}] filtrando @g.us.
    """
    base_url = os.getenv('EVOLUTION_API_URL', '').strip()
    api_key = os.getenv('EVOLUTION_API_KEY', '').strip()
    instance = os.getenv('EVOLUTION_INSTANCE', 'default').strip()

    if not base_url:
        return []

    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            resp = await client.get(
                f'{base_url}/group/fetchAllGroups/{instance}?getParticipants=false',
                headers={'apikey': api_key}
            )
            if resp.status_code != 200:
                logger.warning(f"fetchAllGroups status {resp.status_code}")
                return []
            data = resp.json()
            # Evolution devolve lista; tolera envelope {groups|data: [...]}
            if isinstance(data, dict):
                data = data.get('groups') or data.get('data') or []
            if not isinstance(data, list):
                return []
            # MESMO defeito do findChats, por outra porta: `and name` descartava
            # calado. Aqui nem findGroupInfos faz sentido — o fetchAllGroups JA e
            # a fonte do `subject`; se ele veio vazio, o nome nao existe. Entao o
            # grupo entra com `name=None` e e CONTADO pelo chamador.
            groups = []
            for g in data:
                if not isinstance(g, dict):
                    continue
                jid = g.get('id', '')
                name = (g.get('subject') or '').strip()
                if '@g.us' in jid:
                    groups.append({'jid': jid, 'name': name or None})
            return sorted(groups, key=lambda x: (x['name'] is None,
                                                 x['name'] or x['jid']))
    except Exception as e:
        logger.warning(f"Erro ao buscar grupos via fetchAllGroups: {e}")
        return []


async def get_group_with_members(group_jid: str) -> Optional[Dict]:
    """Detalhe do grupo — do cache se disponivel."""
    cached = get_cached_group_detail(group_jid)
    if cached:
        return cached

    # Fallback: buscar ao vivo (lento)
    info = await get_group_info(group_jid)
    if not info:
        return None

    participants = info.get('participants', [])
    phones = []
    for p in participants:
        phone_jid = p.get('phoneNumber', p.get('id', ''))
        phone = phone_jid.replace('@s.whatsapp.net', '').replace('@lid', '')
        if phone and len(phone) > 8:
            phones.append(phone)

    known = _cross_phone_with_contacts(phones)
    healths = [m['health_score'] for m in known if m.get('health_score') is not None]

    return {
        'group_jid': group_jid,
        'name': info.get('subject', group_jid),
        'total_participants': info.get('size', len(participants)),
        'known_members': len(known),
        'unknown_members': len(participants) - len(known),
        'health_medio': round(sum(healths) / len(healths)) if healths else None,
        'needs_attention': len([m for m in known if (m.get('health_score') or 100) < 50]),
        'members': sorted(known, key=lambda m: m.get('nome', '')),
        'attention_list': sorted([m for m in known if (m.get('health_score') or 100) < 50],
                                  key=lambda m: m.get('health_score', 0)),
    }


def get_contact_crossover() -> List[Dict]:
    """
    Cruza participantes entre grupos.
    Retorna contatos que participam de 2+ grupos, ordenados por quantidade.
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Get all groups with known_contact_ids
        cursor.execute("""
            SELECT group_jid, group_name, known_contact_ids
            FROM social_groups_cache
            WHERE group_name IS NOT NULL AND known_count > 0
        """)
        groups = [dict(r) for r in cursor.fetchall()]

    # Build contact -> groups map
    contact_groups = {}  # contact_id -> [group_names]
    for g in groups:
        ids = g.get('known_contact_ids', [])
        if isinstance(ids, str):
            ids = json.loads(ids)
        for cid in ids:
            if cid not in contact_groups:
                contact_groups[cid] = []
            contact_groups[cid].append(g['group_name'])

    # Filter contacts in 2+ groups
    multi = {cid: gnames for cid, gnames in contact_groups.items() if len(gnames) >= 2}
    if not multi:
        return []

    # Get contact details
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, nome, empresa, cargo, circulo, health_score, foto_url
            FROM contacts WHERE id = ANY(%s)
        """, (list(multi.keys()),))
        contacts = {r['id']: dict(r) for r in cursor.fetchall()}

    results = []
    for cid, gnames in multi.items():
        if cid in contacts:
            c = contacts[cid]
            c['groups'] = sorted(gnames)
            c['group_count'] = len(gnames)
            results.append(c)

    return sorted(results, key=lambda x: -x['group_count'])


def suggest_introductions(limit: int = 10) -> List[Dict]:
    """
    Sugere introducoes entre contatos que compartilham grupos.
    Busca pares de contatos em grupos em comum que NAO tem interacao direta.
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Get all groups with known_contact_ids
        cursor.execute("""
            SELECT group_jid, group_name, known_contact_ids
            FROM social_groups_cache
            WHERE group_name IS NOT NULL AND known_count >= 2
        """)
        groups = [dict(r) for r in cursor.fetchall()]

    # Build pairs: (contact_a, contact_b) -> [shared_groups]
    pairs = {}
    for g in groups:
        ids = g.get('known_contact_ids', [])
        if isinstance(ids, str):
            ids = json.loads(ids)
        ids = sorted(set(ids))
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                pair = (ids[i], ids[j])
                if pair not in pairs:
                    pairs[pair] = []
                pairs[pair].append(g['group_name'])

    # Filter pairs with 2+ shared groups (stronger connection signal)
    strong_pairs = {p: gs for p, gs in pairs.items() if len(gs) >= 2}
    if not strong_pairs:
        # Fall back to any shared group
        strong_pairs = dict(sorted(pairs.items(), key=lambda x: -len(x[1]))[:limit])

    if not strong_pairs:
        return []

    # Get contact details
    all_ids = set()
    for a, b in strong_pairs.keys():
        all_ids.add(a)
        all_ids.add(b)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, nome, empresa, cargo, circulo, health_score, foto_url
            FROM contacts WHERE id = ANY(%s)
        """, (list(all_ids),))
        contacts = {r['id']: dict(r) for r in cursor.fetchall()}

    results = []
    for (a_id, b_id), shared_groups in sorted(strong_pairs.items(), key=lambda x: -len(x[1])):
        if a_id not in contacts or b_id not in contacts:
            continue
        ca = contacts[a_id]
        cb = contacts[b_id]
        results.append({
            'contact_a': ca,
            'contact_b': cb,
            'shared_groups': shared_groups,
            'shared_count': len(shared_groups),
            'reason': _generate_intro_reason(ca, cb, shared_groups)
        })
        if len(results) >= limit:
            break

    return results


def _generate_intro_reason(a: Dict, b: Dict, groups: List[str]) -> str:
    """Gera razao para sugestao de introducao."""
    groups_str = ' e '.join(groups[:3])
    parts = []
    if a.get('empresa') and b.get('empresa') and a['empresa'] != b['empresa']:
        parts.append(f"trabalham em empresas diferentes ({a['empresa']} e {b['empresa']})")
    if a.get('cargo') and b.get('cargo'):
        parts.append(f"atuam como {a['cargo']} e {b['cargo']}")
    base = f"Ambos participam de {groups_str}"
    if parts:
        return f"{base}. {parts[0].capitalize()}."
    return f"{base}."
