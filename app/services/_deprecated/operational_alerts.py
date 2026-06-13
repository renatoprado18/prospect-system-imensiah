"""
Operational Alerts — detector de sinais criticos em mensagens recebidas.

Camada de inteligencia proativa do INTEL (Chief of Staff). Roda em cima do
classificador de mensagens (cron classify-messages) e emite action_proposals
quando detecta sinais que demandam acao do dono (Renato), mesmo quando a
mensagem nao parece "obvia" pro Renato leigo no statcard de atencao.

Triggers (ordem de prioridade):

  P1 (implementado): operational_risk — funcionaria-chave de empresa
       monitorada vai ter cirurgia/internacao/afastamento/atestado/luto.
       Output: action_proposal "Propor call com [contato] sobre [pessoa+risco]".

  P2 (TODO): active_recruitment — curriculo/CV/indicacao recebida +
       cross-reference com RACI item ativo de vaga (Concierge, Gerente Comercial).
       Output: "Avaliar [candidata] no contexto da vaga X".

  P3 (TODO): KPI discrepancy em material novo vs atas anteriores.
       Output: "Bloco de pauta proposto".

  P4 (TODO): stuck RACI item — vencido + 0 update em 30+ dias.
       Output: "Item X em risco estrutural".

Contrato de saida: dict pronto pra ActionProposalsService.create_proposal()
(dedup por contato+tipo em 24h ja roda la dentro).

Caso real que motivou (10/06/2026): Dra. Thalita (Vallen Clinic, contato 5715)
mandou: "Veridiana me avisou agora que ira fazer uma cirurgia segunda (ela
disse que nao conseguiu escolher a data e acabaram de avisar)". Veridiana e
recepcionista chave da Vallen — sai segunda sem plano de cobertura. INTEL
nao sinalizou. Esse modulo cobre.
"""
import logging
import re
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# Empresas monitoradas + funcionarias-chave cuja ausencia gera risco operacional.
# Hardcode pra MVP; depois vira config (system_memories ou tabela operational_watch).
# Match e case-insensitive e exige nome proprio (capitalizado) presente
# como token isolado pra evitar match em substring acidental.
KEY_PERSONNEL: Dict[str, List[str]] = {
    "Vallen Clinic": [
        "Veridiana",
        "Katia",  # variantes acentuadas resolvidas no normalizer
        "Natalia",
        "Lara",
        "Thalita",
    ],
}

# Palavras-gatilho de risco operacional. So matcham como palavra completa
# (\b) e case-insensitive. Sem acento no padrao — input e normalizado antes.
_RISK_KEYWORDS = [
    "cirurgia",
    "internacao",
    "internada",
    "internado",
    "afastamento",
    "afastada",
    "afastado",
    "atestado",
    "licenca medica",
    "luto",
    "falecimento",
    "demissao",
    "demitida",
    "demitido",
    "pediu demissao",
]

_RISK_REGEX = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in _RISK_KEYWORDS) + r")\b",
    re.IGNORECASE,
)

# Distancia maxima (chars) entre o nome da pessoa e a palavra de risco
# pra reduzir falso positivo (banter sobre "cirurgia plastica" fora de
# contexto nao deve casar com "Lara" mencionada 200 chars antes).
_PROXIMITY_WINDOW = 80


def _strip_accents(s: str) -> str:
    """Remove acentos pra comparar via regex sem se preocupar com encoding."""
    import unicodedata
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _find_in_window(text: str, person_name: str) -> Optional[Dict]:
    """
    Procura ocorrencia de nome de pessoa + palavra-gatilho de risco numa
    janela de _PROXIMITY_WINDOW chars. Retorna match info ou None.
    """
    text_norm = _strip_accents(text)
    name_norm = _strip_accents(person_name)

    # Match do nome como token isolado (case-insensitive, \b nas bordas)
    name_re = re.compile(r"\b" + re.escape(name_norm) + r"\b", re.IGNORECASE)
    name_match = name_re.search(text_norm)
    if not name_match:
        return None

    # Janela em torno do nome — antes E depois
    start = max(0, name_match.start() - _PROXIMITY_WINDOW)
    end = min(len(text_norm), name_match.end() + _PROXIMITY_WINDOW)
    window = text_norm[start:end]

    risk_match = _RISK_REGEX.search(window)
    if not risk_match:
        return None

    return {
        "person": person_name,
        "risk_keyword": risk_match.group(1).lower(),
        "window": window,
    }


def detect_operational_risk(
    message_content: str,
    contact_id: int,
    contact_company: Optional[str] = None,
    contact_name: Optional[str] = None,
) -> Optional[Dict]:
    """
    Trigger P1: detecta menção a risco operacional sobre funcionária-chave
    em empresa monitorada.

    Args:
        message_content: texto da mensagem recebida (incoming)
        contact_id: id do contato remetente
        contact_company: empresa do contato remetente (se aparece em KEY_PERSONNEL,
                         consideramos que ele esta falando sobre a propria empresa)
        contact_name: nome do remetente (pra montar a proposta)

    Returns:
        dict pronto pra ActionProposalsService.create_proposal() ou None.
    """
    if not message_content or not contact_id:
        return None

    text = message_content.strip()
    if len(text) < 20:
        # Mensagens muito curtas raramente carregam contexto suficiente
        # ("ok obrigado", "cirurgia?" sem nome). Pula.
        return None

    # Quais empresas monitorar? Se a empresa do remetente esta em KEY_PERSONNEL,
    # ele provavelmente esta falando sobre alguem da propria equipe.
    # Caso contrario, ainda checamos todas as empresas — alguem pode contar
    # a Renato sobre Veridiana via outro canal.
    candidate_companies = list(KEY_PERSONNEL.keys())
    if contact_company and contact_company in KEY_PERSONNEL:
        # Prioriza a propria empresa do remetente — match mais provavel
        candidate_companies = [contact_company] + [
            c for c in candidate_companies if c != contact_company
        ]

    # Pelo menos uma palavra de risco precisa aparecer no texto inteiro
    if not _RISK_REGEX.search(_strip_accents(text)):
        return None

    for company in candidate_companies:
        for person in KEY_PERSONNEL[company]:
            match = _find_in_window(text, person)
            if not match:
                continue

            # Match encontrado — monta a proposta.
            risk_kw = match["risk_keyword"]
            sender_label = contact_name or f"contato #{contact_id}"

            title = f"Alerta operacional {company}: {person} ({risk_kw})"
            description = (
                f"{sender_label} mencionou que {person.title()} esta com "
                f'"{risk_kw}". Propor call rapida com {sender_label} pra '
                f"alinhar plano de cobertura e impacto operacional em {company}."
            )

            return {
                "action_type": "operational_risk",
                "contact_id": contact_id,
                "urgency": "high",
                "confidence": 0.85,
                "trigger_text": text[:300],
                "ai_reasoning": (
                    f"Detector operational_alerts.detect_operational_risk: "
                    f"nome '{person}' + gatilho '{risk_kw}' "
                    f"numa janela de {_PROXIMITY_WINDOW} chars."
                ),
                "title": title,
                "description": description,
                "action_params": {
                    "company": company,
                    "person": person,
                    "risk_keyword": risk_kw,
                    "detector": "operational_alerts.v1",
                },
                "options": [
                    {
                        "id": "schedule_call",
                        "label": f"Agendar call com {sender_label}",
                        "action": "create_task",
                    },
                    {
                        "id": "respond_now",
                        "label": "Responder agora",
                        "action": "open_conversation",
                    },
                    {
                        "id": "ignore",
                        "label": "Ignorar",
                        "action": "dismiss",
                    },
                ],
            }

    return None


def process_message(
    message_id: int,
    contact_id: int,
    message_content: str,
    contact_company: Optional[str] = None,
    contact_name: Optional[str] = None,
) -> Optional[Dict]:
    """
    Wrapper que roda todos os detectors P1..P4 sequencialmente e emite a primeira
    proposta encontrada (P1 tem prioridade absoluta). Wire pra ser chamado pelo
    cron classify-messages depois da classificacao binaria.

    Returns:
        Proposta criada (dict do ActionProposalsService) ou None.
    """
    from services.action_proposals import get_action_proposals

    # P1: operational risk (implementado)
    proposal_data = detect_operational_risk(
        message_content=message_content,
        contact_id=contact_id,
        contact_company=contact_company,
        contact_name=contact_name,
    )

    # TODO P2/P3/P4: ver docs/COS_DILIGENCIA_NEXT.md

    if not proposal_data:
        return None

    proposal_data["message_id"] = message_id
    service = get_action_proposals()
    proposal = service.create_proposal(proposal_data)
    if proposal:
        logger.info(
            f"operational_alerts: created proposal #{proposal.get('id')} "
            f"(type={proposal_data['action_type']}, contact={contact_id}, msg={message_id})"
        )
    return proposal
