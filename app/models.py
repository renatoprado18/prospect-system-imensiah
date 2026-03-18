"""
Modelos de dados do sistema de prospects ImensIAH
"""
from datetime import datetime
from typing import Optional, List, Dict
from pydantic import BaseModel, Field
from enum import Enum
import sqlite3
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

# Database Setup
def init_db(db_path: str = "data/prospects.db"):
    """Inicializa o banco de dados"""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Tabela de usuários
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            role TEXT DEFAULT 'operador',
            senha_hash TEXT,
            tutorial_concluido BOOLEAN DEFAULT FALSE,
            data_criacao TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ultimo_acesso TIMESTAMP
        )
    ''')

    # Inserir usuários padrão
    cursor.execute('''
        INSERT OR IGNORE INTO users (nome, email, role, tutorial_concluido)
        VALUES ('Renato', 'renato@almeida-prado.com', 'admin', TRUE)
    ''')
    cursor.execute('''
        INSERT OR IGNORE INTO users (nome, email, role, tutorial_concluido)
        VALUES ('Andressa Santos', 'andressa@almeida-prado.com', 'operador', FALSE)
    ''')

    # Tabela de prospects
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS prospects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            empresa TEXT,
            cargo TEXT,
            email TEXT,
            telefone TEXT,
            website TEXT,
            linkedin TEXT,
            score INTEGER DEFAULT 0,
            tier TEXT DEFAULT 'E',
            score_breakdown TEXT DEFAULT '{}',
            reasons TEXT DEFAULT '[]',
            status TEXT DEFAULT 'pendente_aprovacao',
            aprovado_por_renato BOOLEAN DEFAULT FALSE,
            data_aprovacao TIMESTAMP,
            notas_renato TEXT,
            prioridade_renato INTEGER DEFAULT 0,
            data_criacao TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            data_ultimo_contato TIMESTAMP,
            data_reuniao TIMESTAMP,
            meeting_outcome TEXT,
            fathom_meeting_id TEXT,
            meeting_notes TEXT,
            objecoes TEXT DEFAULT '[]',
            interesse_features TEXT DEFAULT '[]',
            converted BOOLEAN DEFAULT FALSE,
            deal_value REAL,
            conversion_notes TEXT,
            dados_enriquecidos TEXT DEFAULT '{}'
        )
    ''')

    # Tabela de reuniões
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS meetings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prospect_id INTEGER NOT NULL,
            google_event_id TEXT,
            fathom_meeting_id TEXT,
            data_hora TIMESTAMP,
            duracao_minutos INTEGER DEFAULT 30,
            tipo TEXT DEFAULT 'discovery',
            realizada BOOLEAN DEFAULT FALSE,
            outcome TEXT,
            summary TEXT,
            key_topics TEXT DEFAULT '[]',
            action_items TEXT DEFAULT '[]',
            sentiment TEXT,
            objecoes_identificadas TEXT DEFAULT '[]',
            pontos_interesse TEXT DEFAULT '[]',
            proximos_passos TEXT,
            FOREIGN KEY (prospect_id) REFERENCES prospects(id)
        )
    ''')

    # Tabela de análise de ICP
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS icp_analysis (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data_analise TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            analysis_data TEXT NOT NULL
        )
    ''')

    # Tabela de argumentos de venda
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sales_arguments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            argumento TEXT NOT NULL,
            categoria TEXT,
            efetividade_score REAL DEFAULT 0,
            vezes_usado INTEGER DEFAULT 0,
            vezes_converteu INTEGER DEFAULT 0,
            objecao_relacionada TEXT,
            data_criacao TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Tabela de log de atividades
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prospect_id INTEGER,
            usuario TEXT,
            acao TEXT NOT NULL,
            detalhes TEXT,
            data_hora TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (prospect_id) REFERENCES prospects(id)
        )
    ''')

    conn.commit()
    conn.close()

    return True
