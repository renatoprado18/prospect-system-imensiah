"""
Política de voz do Renato, destilada do corpus REAL (fase 1: WhatsApp).

Why: o gerador de draft (CoS, camada esperta do inbox, tonIAH) escrevia num
registro médio e o Renato reescrevia — 5 rounds num WA de 3 linhas pra Andressa
foi o gatilho. A política NÃO é escrita à mão: sai do que ele já mandou
(~5,7k mensagens outgoing), por PERFIL DE DESTINATÁRIO, com evidência contável.
Espelha [[feedback_passive_labeling_over_manual]] — comportamento real é o
ground-truth, não regra declarada.

Duas camadas, de propósito:
  1. MEDIÇÃO determinística (`measure_style`) — contagens verificáveis
     (saudação, fechamento, grato×obrigado, emoji, tamanho). É o que dá
     autoridade à regra e permite ao Renato conferir/refutar linha por linha.
  2. SÍNTESE por LLM (`distill_policy`) — só transforma as contagens + amostras
     em instrução acionável. O LLM não INVENTA regra: ele recebe as métricas já
     apuradas e as amostras reais.

Fase 1 = WhatsApp. Fase 2 = e-mail (Gmail Sent), com coletor e limpeza próprios:
o registro é outro (mais longo, com vocativo e assinatura) e o texto vem sujo de
citação da thread. O canal NÃO mora em `messages` — mora em `conversations.canal`,
e é por isso que `collect_corpus` faz o JOIN: sem ele os dois corpora se misturam.

Ver [[project_voice_policy]].
"""
from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Um documento por canal: a voz de e-mail é outra e misturá-las produziria uma
# média que não descreve nenhuma das duas. O título do WhatsApp fica inalterado
# pra não quebrar quem já lê a política da fase 1.
MEMORY_TITLE = "reference_renato_voice"
MEMORY_TITLE_EMAIL = "reference_renato_voice_email"

CANAIS = ("whatsapp", "email")


def memory_title_for(canal: str) -> str:
    return MEMORY_TITLE_EMAIL if canal == "email" else MEMORY_TITLE

# Fichas do PRÓPRIO Renato: self-chat é anotação pessoal, não comunicação com
# terceiro — treinar nisso ensina o gerador a falar sozinho. IDs conferidos em
# prod 26/07 (todos com o telefone RENATO_PHONE ou nome idêntico).
SELF_CONTACT_IDS = (25613, 14911, 23419, 25407)

# A instância do bot. Sem excluir, a política aprende a voz da tonIAH — era o
# risco #1 apontado na spec. Medido: 196 msgs, todas pro próprio Renato.
BOT_INSTANCE = "intel-bot-v2"

# Placeholder de mídia que o webhook grava quando não há texto.
_MEDIA_PLACEHOLDER = re.compile(r"^\[(Áudio|Imagem|Vídeo|Documento|Figurinha|Contato)")

# Bloco de export do WhatsApp colado dentro do conteúdo (import .txt do iOS,
# ver [[feedback_wa_import_tz_dupes]]). Vem com fala de TERCEIROS embutida —
# 67 casos medidos, incluindo linhas da Wanelise. Truncamos no primeiro bloco
# em vez de descartar: o texto ANTES dele é do Renato e é bom corpus.
_EXPORT_BLOCK = re.compile(r"\[\d{1,2}/\d{1,2}/\d{2,4},\s*\d{1,2}:\d{2}")

_INVISIBLE = re.compile(r"[‎‏‪-‮]")


def clean_outgoing(text: Optional[str]) -> str:
    """Deixa só o que o Renato efetivamente escreveu naquela mensagem."""
    if not text:
        return ""
    t = _INVISIBLE.sub("", text)
    m = _EXPORT_BLOCK.search(t)
    if m:
        t = t[:m.start()]
    return t.strip()


# ---- Limpeza específica de e-mail (fase 2) ----
#
# Um e-mail enviado carrega, além do texto do Renato: a thread citada abaixo
# (que é fala de TERCEIRO — o mesmo erro de corpus que o bloco de export do
# WhatsApp causava na fase 1), a assinatura, e o corpo inteiro do e-mail alheio
# quando é encaminhamento. Sem cortar isso, a "voz" medida seria a dos outros.

# "Em qui., 16 de jul. de 2026 às 21:58, Fulano <x@y> escreveu:" e variantes
# (pt-BR e en). Tudo daí pra baixo é thread citada.
_EMAIL_QUOTE_HEADER = re.compile(
    r"(?:^|\n)\s*(?:"
    # O cabeçalho de citação quebra em várias linhas quando o nome + e-mail do
    # remetente são longos — o limite precisa caber "Em <data> às <hora>,
    # <Nome Completo> <\n<email@dominio>> escreveu:".
    r"Em\s+.{0,200}?escreveu:"
    r"|On\s+.{0,200}?wrote:"
    r"|-{2,}\s*Mensagem (?:original|encaminhada)\s*-{2,}"
    r"|-{2,}\s*(?:Original|Forwarded) [Mm]essage\s*-{2,}"
    r"|_{10,}"
    r")",
    re.IGNORECASE | re.DOTALL,
)

# Assinatura. O corte é pelo BLOCO estrutural (telefone, LinkedIn, e-mail dele,
# "Saudações/Regards"), não pela despedida: "Abraço,\nRenato" é fechamento e
# PRECISA ficar no corpus — é justamente o que a métrica de despedida conta.
_EMAIL_SIGNATURE = re.compile(
    r"(?:^|\n)[ \t]*(?:"
    r"--\s*$"
    r"|Sauda[çc][õo]es\s*/\s*Regards"
    # Linha que é só o nome dele, em qualquer das grafias que ele assina
    # ("Renato A Prado", "Renato F A Prado", "Renato de Faria e Almeida Prado").
    # A despedida "Abraço,\nRenato" NÃO cai aqui: exige sobrenome.
    r"|Renato(?:\s+(?:[A-ZÀ-Ú][\wÀ-ú]*\.?|de|da|e))*\s+Prado\s*$"
    r"|\+?55\s*\(?\d{2}\)?\s*9?\d{4,5}[-\s]?\d{4}"
    r"|LinkedIn:\s*https?://"
    r"|renato[\w.]*@[\w.-]+\s*$"
    r")",
    re.IGNORECASE | re.MULTILINE,
)

# Encaminhamento gerado pela camada esperta do inbox — texto de máquina + corpo
# de terceiro. Não é voz dele em nenhuma parte; descarta a mensagem inteira.
_EMAIL_AUTO_FORWARD = re.compile(r"^\s*Delegado por Renato via triagem", re.IGNORECASE)

# Linhas de citação (">") que sobrem depois do corte.
_EMAIL_QUOTED_LINE = re.compile(r"^\s*>.*$", re.MULTILINE)


def clean_email(text: Optional[str]) -> str:
    """Só o texto que o Renato escreveu neste e-mail — sem thread, sem assinatura.

    Devolve "" para o que não é voz dele (encaminhamento automático), o que faz
    o chamador descartar a mensagem pelo mesmo gate de `min_len`.
    """
    if not text:
        return ""
    t = _INVISIBLE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    if _EMAIL_AUTO_FORWARD.search(t):
        return ""
    m = _EMAIL_QUOTE_HEADER.search(t)
    if m:
        t = t[:m.start()]
    m = _EMAIL_SIGNATURE.search(t)
    if m:
        t = t[:m.start()]
    t = _EMAIL_QUOTED_LINE.sub("", t)
    # Colapsa as linhas em branco que os cortes deixam pra trás.
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


# ============== Perfis de destinatário ==============
#
# Uma política única deixaria o gerador falar com a companheira como fala com
# um conselho — e o Renato seguiria corrigindo. Os perfis saem da RELAÇÃO
# (tags/cargo/círculo do contato), que é o que o gerador sabe antes de escrever.

PROFILE_ORDER = (
    "companheira", "familia", "assistente", "par_profissional",
    "cliente", "prestador", "circulo_proximo", "outros",
)

_FAMILY_TAGS = ("filho", "filha", "pai", "mae", "mãe", "irma", "irmã", "irmao", "irmão", "familia", "família")
_PEER_TAGS = ("conselheiro", "c-level", "assespro", "conselho")
_PEER_CARGO = ("ceo", "chairman", "board", "partner", "diretor", "conselheiro", "presidente", "founder")
_CLIENT_TAGS = ("cliente", "mentor")
_SERVICE_CARGO = ("advogad", "contador", "contabil", "médic", "medic", "dermatolog", "massagi", "arquitet")


def classify_recipient(contact: Dict) -> str:
    """Perfil de voz para um contato. Ordem importa: o mais específico ganha."""
    tags = " ".join(contact.get("tags") or []) if isinstance(contact.get("tags"), list) else str(contact.get("tags") or "")
    tags = tags.lower()
    cargo = (contact.get("cargo") or "").lower()
    nome = (contact.get("nome") or "").lower()
    circulo = contact.get("circulo") or 9

    if "companheira" in tags or "esposa" in tags:
        return "companheira"
    if any(t in tags for t in _FAMILY_TAGS):
        return "familia"
    # Assistente é papel, não círculo: identifica por cargo, não por nome
    # (ver [[feedback_no_hardcoded_contact_ids]] — nome muda, papel não).
    if "assistente" in cargo or "secretár" in cargo or "secretar" in cargo:
        return "assistente"
    if any(t in tags for t in _PEER_TAGS) or any(k in cargo for k in _PEER_CARGO):
        return "par_profissional"
    if any(t in tags for t in _CLIENT_TAGS):
        return "cliente"
    if any(k in cargo for k in _SERVICE_CARGO):
        return "prestador"
    if circulo <= 2:
        return "circulo_proximo"
    return "outros"


# ============== Medição determinística ==============

_GREETINGS = {
    "bom dia": r"\bbom dia\b",
    "boa tarde": r"\bboa tarde\b",
    "boa noite": r"\bboa noite\b",
    "oi": r"^\s*oi\b",
    "olá": r"^\s*ol[áa]\b",
    "e aí": r"^\s*e a[íi]\b",
}
_CLOSINGS = {
    "abraço": r"\babra[çc]o",
    "grato": r"\bgrato\b",
    "obrigado": r"\bobrigad[oa]\b",
    "valeu": r"\bvaleu\b",
    "até": r"\bat[ée] (mais|logo|breve)\b",
    "beijo": r"\bbeijo",
    "à disposição": r"(à|a) disposi[çc][ãa]o",
}
_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")
_VC_ABREV = re.compile(r"\bvc\b", re.I)
_VOCE = re.compile(r"\bvoc[êe]\b", re.I)
# Palavras que DEVERIAM ter acento e apareceram sem — proxy de descuido.
_MISSING_ACCENT = re.compile(
    r"\b(nao|voce|ja|ate|esta|sao|tambem|amanha|proximo|acao|reuniao|obrigado|apos|so)\b"
)


def measure_style(messages: List[str]) -> Dict:
    """Contagens de estilo sobre um conjunto de mensagens de UM perfil."""
    n = len(messages)
    if not n:
        return {"n": 0}
    acc = {
        "n": n, "chars_total": 0, "multilinha": 0, "com_saudacao": 0,
        "com_fechamento": 0, "pergunta": 0, "exclamacao": 0, "emoji": 0,
        "vc_abreviado": 0, "voce_pleno": 0, "acento_faltando": 0,
    }
    saud, fech = Counter(), Counter()
    for t in messages:
        low = t.lower()
        acc["chars_total"] += len(t)
        if "\n" in t:
            acc["multilinha"] += 1
        hits_s = [k for k, rx in _GREETINGS.items() if re.search(rx, low)]
        if hits_s:
            acc["com_saudacao"] += 1
            saud.update(hits_s)
        hits_f = [k for k, rx in _CLOSINGS.items() if re.search(rx, low)]
        if hits_f:
            acc["com_fechamento"] += 1
            fech.update(hits_f)
        if "?" in t:
            acc["pergunta"] += 1
        if "!" in t:
            acc["exclamacao"] += 1
        if _EMOJI.search(t):
            acc["emoji"] += 1
        if _VC_ABREV.search(t):
            acc["vc_abreviado"] += 1
        if _VOCE.search(t):
            acc["voce_pleno"] += 1
        if _MISSING_ACCENT.search(t):
            acc["acento_faltando"] += 1

    acc["len_medio"] = acc["chars_total"] // n
    acc["saudacoes"] = dict(saud.most_common())
    acc["fechamentos"] = dict(fech.most_common())
    for k in ("com_saudacao", "com_fechamento", "pergunta", "exclamacao",
              "emoji", "vc_abreviado", "acento_faltando", "multilinha"):
        acc[f"pct_{k}"] = round(100.0 * acc[k] / n, 1)
    return acc


def collect_corpus(conn, canal: str = "whatsapp",
                   min_len: Optional[int] = None) -> Dict[str, List[str]]:
    """Corpus outgoing limpo de UM canal, agrupado por perfil de destinatário.

    Filtros (cada um é um gate de qualidade, não capricho):
      - `conversations.canal = %(canal)s` -> não mistura WhatsApp com e-mail.
        O canal NÃO está em `messages`; sem este JOIN os 138 e-mails (1.836
        caracteres em média) entravam no corpus de WhatsApp (136 de média) e
        distorciam a métrica de tamanho e a amostra, que é ordenada por
        comprimento — os e-mails ocupavam justamente a cauda longa.
      - `instance <> BOT_INSTANCE`  -> não aprende a voz da tonIAH
      - contato não é o próprio Renato -> não aprende self-chat
      - sem placeholder de mídia / link puro -> não é texto
      - limpeza por canal -> tira fala de terceiros (bloco de export no WhatsApp,
        thread citada e assinatura no e-mail)
    """
    if canal not in CANAIS:
        raise ValueError(f"canal inválido: {canal!r}. Use um de {CANAIS}.")
    # E-mail tem piso maior: abaixo disso é "ok, obrigado" — confirmação, não voz.
    if min_len is None:
        min_len = 60 if canal == "email" else 15
    limpar = clean_email if canal == "email" else clean_outgoing

    cur = conn.cursor()
    cur.execute(
        """
        SELECT m.conteudo, c.id AS contact_id, c.nome, c.tags, c.cargo, c.circulo
        FROM messages m
        JOIN contacts c ON c.id = m.contact_id
        JOIN conversations cv ON cv.id = m.conversation_id
        WHERE m.direcao = 'outgoing'
          AND cv.canal = %s
          AND COALESCE(m.metadata->>'instance', '') <> %s
          AND m.contact_id <> ALL(%s)
          AND m.conteudo IS NOT NULL
          AND m.conteudo !~ %s
          AND m.conteudo !~* '^https?://\\S+$'
        """,
        (canal, BOT_INSTANCE, list(SELF_CONTACT_IDS), _MEDIA_PLACEHOLDER.pattern),
    )
    por_perfil: Dict[str, List[str]] = defaultdict(list)
    for row in cur.fetchall():
        r = dict(row)
        texto = limpar(r.get("conteudo"))
        if len(texto) < min_len:
            continue
        por_perfil[classify_recipient(r)].append(texto)
    return dict(por_perfil)


def sample_for_profile(messages: List[str], k: int = 25) -> List[str]:
    """Amostra determinística e representativa (curtas, médias e longas).

    Determinística de propósito: mesma entrada -> mesma amostra, senão a
    política muda de texto a cada regeneração sem o corpus ter mudado.
    """
    ordenadas = sorted(messages, key=len)
    if len(ordenadas) <= k:
        return ordenadas
    passo = len(ordenadas) / k
    return [ordenadas[int(i * passo)] for i in range(k)]


# ============== Síntese (LLM) ==============

_CANAL_LABEL = {
    "whatsapp": "mensagens de WhatsApp",
    "email": "e-mails (já sem a thread citada e sem assinatura)",
}

# Gate de corpus por canal. E-mail é mais baixo de propósito: são textos ~13×
# mais longos que os de WhatsApp, então 12 e-mails carregam mais sinal de estilo
# que 30 mensagens de WhatsApp — e o corpus total de e-mail é 138.
MIN_CORPUS = {"whatsapp": 30, "email": 12}

_DISTILL_PROMPT = """Você recebe MÉTRICAS APURADAS e AMOSTRAS REAIS de {canal_label}
que Renato de Faria e Almeida Prado enviou para um tipo específico de destinatário.

Sua tarefa: escrever a regra de voz que um gerador de rascunho deve seguir para
soar como ele COM ESSE destinatário.

REGRAS DA SUA RESPOSTA:
- Use APENAS o que as métricas e as amostras sustentam. Não invente traço que não
  aparece. Se as métricas não mostram algo, não afirme.
- Seja concreto e acionável ("abre sem saudação, vai direto ao pedido"), não
  genérico ("tom profissional e cordial").
- Cite o número quando ele justificar a regra.
- Português do Brasil, com acentuação correta.

PERFIL: {perfil}

MÉTRICAS ({n} mensagens):
- comprimento médio: {len_medio} caracteres
- abre com saudação: {pct_com_saudacao}% (mais usadas: {saudacoes})
- fecha com despedida: {pct_com_fechamento}% (mais usadas: {fechamentos})
- contém pergunta: {pct_pergunta}% | exclamação: {pct_exclamacao}%
- emoji: {pct_emoji}% | "vc" abreviado: {pct_vc_abreviado}% | multilinha: {pct_multilinha}%

AMOSTRAS REAIS (uma por linha, ordenadas da mais curta à mais longa):
{amostras}

Responda em JSON, exatamente estas chaves:
{{"abertura": "...", "fechamento": "...", "tamanho": "...", "tom": "...",
  "faça": ["...", "..."], "evite": ["...", "..."],
  "exemplo_canonico": "uma mensagem curta que você escreveria neste perfil"}}"""


def distill_policy(conn, perfis: Optional[List[str]] = None,
                   canal: str = "whatsapp") -> Dict[str, Dict]:
    """Roda a síntese por perfil, num canal. Devolve {perfil: {metricas, regras}}."""
    import json as _json

    from services.llm import BALANCED, _call_model

    corpus = collect_corpus(conn, canal=canal)
    minimo = MIN_CORPUS.get(canal, 30)
    # E-mail é longo: amostra menor e trecho maior por amostra cabem melhor no
    # mesmo orçamento de prompt do que 25 e-mails truncados em 300 caracteres.
    k_amostra, corte = (10, 900) if canal == "email" else (25, 300)
    out: Dict[str, Dict] = {}
    for perfil, msgs in corpus.items():
        if perfis and perfil not in perfis:
            continue
        if len(msgs) < minimo:
            logger.info("voice_policy[%s]: perfil %s com só %d msgs — pulando",
                        canal, perfil, len(msgs))
            out[perfil] = {"metricas": measure_style(msgs), "regras": None,
                           "nota": f"corpus insuficiente (<{minimo}) — não destilado"}
            continue
        m = measure_style(msgs)
        amostras = sample_for_profile(msgs, k=k_amostra)
        prompt = _DISTILL_PROMPT.format(
            canal_label=_CANAL_LABEL[canal],
            perfil=perfil, n=m["n"], len_medio=m["len_medio"],
            pct_com_saudacao=m["pct_com_saudacao"], saudacoes=m["saudacoes"],
            pct_com_fechamento=m["pct_com_fechamento"], fechamentos=m["fechamentos"],
            pct_pergunta=m["pct_pergunta"], pct_exclamacao=m["pct_exclamacao"],
            pct_emoji=m["pct_emoji"], pct_vc_abreviado=m["pct_vc_abreviado"],
            pct_multilinha=m["pct_multilinha"],
            amostras="\n".join(f"- {a[:corte]}" for a in amostras),
        )
        # E-mail produz regra mais longa (mais traços por amostra: vocativo,
        # estrutura, fechamento). Com 1200 o JSON do perfil `familia` voltou
        # truncado e o parse falhou — a seção degradava pra "síntese não
        # disponível" em silêncio.
        raw = _call_model(BALANCED, prompt,
                          max_tokens=2400 if canal == "email" else 1200,
                          function=f"voice_policy.distill.{canal}")
        regras = None
        if raw:
            try:
                txt = raw[raw.find("{"):raw.rfind("}") + 1]
                regras = _json.loads(txt)
            except Exception as e:
                logger.warning("voice_policy: JSON inválido no perfil %s: %s", perfil, e)
        out[perfil] = {"metricas": m, "regras": regras}
    return out


# ============== Documento revisável ==============

# Perfis fora do documento PERSISTIDO por padrão. Não é limitação técnica: a
# política vive em `system_memories`, que o briefing, a camada e a tonIAH leem —
# e o perfil `companheira` destila apelidos e termos de intimidade do casal. Há
# precedente explícito do Renato de manter frente pessoal fora de board/memória
# (board CoS 22/07). Passar `exclude=()` inclui tudo, se ele pedir.
DEFAULT_EXCLUDE_FROM_MEMORY = ("companheira",)


def render_policy_md(destilado: Dict[str, Dict],
                     exclude: tuple = DEFAULT_EXCLUDE_FROM_MEMORY,
                     canal: str = "whatsapp") -> str:
    """Markdown que o Renato lê e corrige. Métrica ao lado de cada regra — é o
    que separa 'política revisável' de caixa-preta."""
    from services.tz import format_brt, now_utc

    titulo = "WhatsApp" if canal == "whatsapp" else "e-mail"
    nota_canal = (
        "Fase 1 = WhatsApp; a política de e-mail vive em documento próprio."
        if canal == "whatsapp" else
        "Fase 2 = e-mail. Corpus vem do Gmail Sent, já sem a thread citada e "
        "sem assinatura — só o que ele escreveu."
    )
    linhas = [
        f"# Política de voz — Renato ({titulo})",
        "",
        f"Destilada do corpus real em {format_brt(now_utc())}. {nota_canal}",
        "",
        "Cada regra vem com a contagem que a sustenta. Discordou? A regra está "
        "errada ou o corpus mudou — corrija aqui e o gerador passa a seguir.",
        "",
    ]
    total = sum(d["metricas"].get("n", 0) for d in destilado.values())
    unidade = "mensagens" if canal == "whatsapp" else "e-mails"
    linhas.append(f"**Corpus:** {total} {unidade} enviados, "
                  f"{len(destilado)} perfis de destinatário.")
    linhas.append("")

    # Hipótese herdada das regras manuais — confirmada ou refutada pelo corpus.
    grato = sum(d["metricas"].get("fechamentos", {}).get("grato", 0) for d in destilado.values())
    obrigado = sum(d["metricas"].get("fechamentos", {}).get("obrigado", 0) for d in destilado.values())
    veredito = "CONFIRMADA" if grato > obrigado * 3 else "REFUTADA"
    linhas += [
        "## Hipóteses das regras antigas, testadas no corpus",
        "",
        f"- **\"grato\", nunca \"obrigado\"** — {veredito}: "
        f"`grato` {grato}× contra `obrigado` {obrigado}× no corpus todo.",
        "",
    ]

    for perfil in PROFILE_ORDER:
        d = destilado.get(perfil)
        if not d:
            continue
        if perfil in exclude:
            linhas += [f"## Perfil: {perfil}", "",
                       "_Omitido deste documento por privacidade "
                       "(a política é lida pelo briefing/bot). "
                       "Regenerar com `exclude=()` para incluir._", ""]
            continue
        m, r = d["metricas"], d.get("regras")
        linhas.append(f"## Perfil: {perfil}")
        linhas.append("")
        linhas.append(
            f"*{m['n']} mensagens · {m['len_medio']} caracteres em média · "
            f"saudação {m['pct_com_saudacao']}% · despedida {m['pct_com_fechamento']}% · "
            f"emoji {m['pct_emoji']}%*"
        )
        linhas.append("")
        if not r:
            linhas.append(f"_{d.get('nota', 'síntese não disponível')}_")
            linhas.append("")
            continue
        linhas.append(f"- **Abertura:** {r.get('abertura','—')}")
        linhas.append(f"- **Fechamento:** {r.get('fechamento','—')}")
        linhas.append(f"- **Tamanho:** {r.get('tamanho','—')}")
        linhas.append(f"- **Tom:** {r.get('tom','—')}")
        for item in (r.get("faça") or []):
            linhas.append(f"  - ✅ {item}")
        for item in (r.get("evite") or []):
            linhas.append(f"  - ❌ {item}")
        if r.get("exemplo_canonico"):
            linhas.append("")
            linhas.append(f"  > {r['exemplo_canonico']}")
        linhas.append("")
    return "\n".join(linhas)


def persist_policy(conn, markdown: str, canal: str = "whatsapp") -> int:
    """Grava/atualiza a política do canal em `system_memories`.

    Why system_memories: é a memória que a camada, o briefing e o bot JÁ leem em
    prod (o `search_memories` une system_memories + tonia_memories desde
    `aace664`). Guardar num .md do Mac não serviria — prod não vê o disco dele.
    Sem DDL.
    """
    import json as _json

    titulo = memory_title_for(canal)
    tags = _json.dumps(["voz", "draft", canal])
    cur = conn.cursor()
    cur.execute("SELECT id FROM system_memories WHERE titulo = %s", (titulo,))
    row = cur.fetchone()
    if row:
        mid = dict(row)["id"]
        cur.execute(
            """UPDATE system_memories
               SET conteudo = %s, tipo = 'reference', fonte = 'voice_policy.distill',
                   atualizado_em = NOW(), embedding = NULL
             WHERE id = %s""",
            (markdown, mid),
        )
    else:
        cur.execute(
            """INSERT INTO system_memories (titulo, conteudo, tipo, tags, fonte)
               VALUES (%s, %s, 'reference', %s::jsonb, 'voice_policy.distill')
               RETURNING id""",
            (titulo, markdown, tags),
        )
        mid = dict(cur.fetchone())["id"]
    conn.commit()
    return mid


# ============== Consumo pelos geradores ==============

def get_voice_guidance(conn, contact_id: int, canal: str = "whatsapp") -> Optional[str]:
    """Bloco de instrução de voz pro perfil deste contato, NESTE canal.

    É o que CoS / camada esperta / tonIAH injetam ANTES de rascunhar. Devolve
    None se não há política gravada — o chamador segue com o comportamento
    antigo (degradação silenciosa, nunca trava o draft).

    Se o canal pedido ainda não tem política destilada, cai no WhatsApp: uma voz
    aproximada do mesmo autor é melhor que nenhuma, e é o que já rodava antes da
    fase 2. O `canal` default preserva todos os chamadores existentes.
    """
    cur = conn.cursor()
    cur.execute("SELECT nome, tags, cargo, circulo FROM contacts WHERE id = %s", (contact_id,))
    row = cur.fetchone()
    if not row:
        return None
    perfil = classify_recipient(dict(row))

    cur.execute("SELECT conteudo FROM system_memories WHERE titulo = %s",
                (memory_title_for(canal),))
    pol = cur.fetchone()
    if not pol and canal != "whatsapp":
        cur.execute("SELECT conteudo FROM system_memories WHERE titulo = %s", (MEMORY_TITLE,))
        pol = cur.fetchone()
    if not pol:
        return None
    texto = dict(pol)["conteudo"]

    # Recorta a seção do perfil + o bloco de hipóteses (que vale pra todos).
    bloco = []
    guardando = False
    for linha in texto.split("\n"):
        if linha.startswith("## Hipóteses"):
            guardando = True
        elif linha.startswith(f"## Perfil: {perfil}"):
            guardando = True
        elif linha.startswith("## "):
            guardando = False
        if guardando:
            bloco.append(linha)
    return "\n".join(bloco).strip() or None
