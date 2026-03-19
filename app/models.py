"""
Modelos de dados do sistema de prospects ImensIAH
"""
from datetime import datetime
from typing import Optional, List, Dict
from pydantic import BaseModel, Field
from enum import Enum
import json

class ProspectStatus(str, Enum):
    PENDENTE_APROVACAO = "pendente_aprovacao"  # Aguardando Renato aprovar
    NOVO = "novo"  # Aprovado, disponível para Andressa
    CONTATADO = "contatado"
    REUNIAO_AGENDADA = "reuniao_agendada"
    REUNIAO_REALIZADA = "reuniao_realizada"
    NEGOCIANDO = "negociando"
    CONVERTIDO = "convertido"
    PERDIDO = "perdido"
    NURTURING = "nurturing"
    REJEITADO = "rejeitado"  # Renato rejeitou

class UserRole(str, Enum):
    ADMIN = "admin"  # Renato - pode aprovar, ver tudo
    OPERADOR = "operador"  # Andressa - só vê aprovados

class ProspectTier(str, Enum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"
    E = "E"

class MeetingOutcome(str, Enum):
    MUITO_INTERESSADO = "muito_interessado"
    INTERESSADO = "interessado"
    NEUTRO = "neutro"
    POUCO_INTERESSE = "pouco_interesse"
    SEM_INTERESSE = "sem_interesse"
    NAO_COMPARECEU = "nao_compareceu"

class Prospect(BaseModel):
    id: Optional[int] = None
    nome: str
    empresa: Optional[str] = None
    cargo: Optional[str] = None
    email: Optional[str] = None
    telefone: Optional[str] = None
    website: Optional[str] = None
    linkedin: Optional[str] = None

    # Scoring
    score: int = 0
    tier: ProspectTier = ProspectTier.E
    score_breakdown: Dict[str, int] = Field(default_factory=dict)
    reasons: List[str] = Field(default_factory=list)

    # Pipeline
    status: ProspectStatus = ProspectStatus.NOVO

    # Tracking
    data_criacao: datetime = Field(default_factory=datetime.now)
    data_ultimo_contato: Optional[datetime] = None
    data_reuniao: Optional[datetime] = None

    # Feedback
    meeting_outcome: Optional[MeetingOutcome] = None
    fathom_meeting_id: Optional[str] = None
    meeting_notes: Optional[str] = None
    objecoes: List[str] = Field(default_factory=list)
    interesse_features: List[str] = Field(default_factory=list)

    # ICP Learning
    converted: bool = False
    deal_value: Optional[float] = None
    conversion_notes: Optional[str] = None

    # Enrichment
    dados_enriquecidos: Dict = Field(default_factory=dict)

class Meeting(BaseModel):
    id: Optional[int] = None
    prospect_id: int
    google_event_id: Optional[str] = None
    fathom_meeting_id: Optional[str] = None

    data_hora: datetime
    duracao_minutos: int = 30
    tipo: str = "discovery"  # discovery, demo, negociacao, fechamento

    # Resultados
    realizada: bool = False
    outcome: Optional[MeetingOutcome] = None

    # Insights do Fathom
    summary: Optional[str] = None
    key_topics: List[str] = Field(default_factory=list)
    action_items: List[str] = Field(default_factory=list)
    sentiment: Optional[str] = None

    # Learning
    objecoes_identificadas: List[str] = Field(default_factory=list)
    pontos_interesse: List[str] = Field(default_factory=list)
    proximos_passos: Optional[str] = None

class ICPAnalysis(BaseModel):
    """Análise do Perfil Ideal de Cliente baseada em dados reais"""

    # Características mais correlacionadas com conversão
    cargos_top_conversao: List[Dict] = Field(default_factory=list)
    setores_top_conversao: List[Dict] = Field(default_factory=list)
    tamanho_empresa_ideal: Optional[str] = None

    # Padrões identificados
    objecoes_comuns: List[Dict] = Field(default_factory=list)
    features_mais_valorizadas: List[Dict] = Field(default_factory=list)

    # Métricas
    taxa_conversao_por_tier: Dict[str, float] = Field(default_factory=dict)
    ticket_medio_por_segmento: Dict[str, float] = Field(default_factory=dict)

    # Argumentos de venda
    argumentos_que_funcionam: List[str] = Field(default_factory=list)
    argumentos_que_nao_funcionam: List[str] = Field(default_factory=list)

    data_analise: datetime = Field(default_factory=datetime.now)

# Database initialization is now in database.py
def init_db(db_path: str = None):
    """Wrapper for backwards compatibility - now uses PostgreSQL"""
    from database import init_db as pg_init_db
    return pg_init_db()
