"""
PostgreSQL Database Module for Vercel Postgres
With connection pooling for local development performance
"""
import os
import json
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import pool
from contextlib import contextmanager

# Local PostgreSQL for development (much faster)
LOCAL_DB_URL = "postgresql://rap@localhost:5432/intel"

# Vercel Postgres connection string (production)
DATABASE_URL = os.getenv("POSTGRES_URL", os.getenv("DATABASE_URL", ""))

# Connection pool for local development (reuse connections)
_connection_pool = None


def _get_conn_string():
    """Get formatted connection string - prefers local DB for development"""
    # Check if local PostgreSQL is available
    if os.getenv("USE_LOCAL_DB") == "1" or not os.getenv("VERCEL"):
        try:
            # Test local connection
            test_conn = psycopg2.connect(LOCAL_DB_URL)
            test_conn.close()
            return LOCAL_DB_URL
        except:
            pass  # Fall back to remote

    if not DATABASE_URL:
        raise Exception("POSTGRES_URL environment variable not set")
    # Vercel uses postgres:// but psycopg2 needs postgresql://
    return DATABASE_URL.replace("postgres://", "postgresql://")


def _get_pool():
    """Get or create connection pool"""
    global _connection_pool
    if _connection_pool is None:
        _connection_pool = pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=10,
            dsn=_get_conn_string()
        )
    return _connection_pool


def _create_connection():
    """Get connection from pool (or create new for serverless)"""
    # Use pooling for local development (if not on Vercel)
    if not os.getenv('VERCEL'):
        try:
            conn = _get_pool().getconn()
            conn.cursor_factory = RealDictCursor
            return conn
        except Exception as e:
            print(f"[DB] Pool failed, falling back to direct: {e}")
    # Fallback: direct connection (serverless/Vercel)
    return psycopg2.connect(_get_conn_string(), cursor_factory=RealDictCursor)


def _return_to_pool(conn):
    """Return connection to pool if using pooling"""
    if not os.getenv('VERCEL') and _connection_pool:
        try:
            _connection_pool.putconn(conn)
            return
        except:
            pass
    # Fallback: close connection
    try:
        conn.close()
    except:
        pass


class DBConnection:
    """
    Database connection wrapper that works both:
    - As context manager: with get_db() as conn:
    - As direct connection: conn = get_db(); ... ; conn.close()
    """
    def __init__(self):
        self._conn = None

    def _get_conn(self):
        if self._conn is None:
            self._conn = _create_connection()
        return self._conn

    def cursor(self):
        return self._get_conn().cursor()

    def commit(self):
        if self._conn:
            self._conn.commit()

    def rollback(self):
        if self._conn:
            self._conn.rollback()

    def close(self):
        if self._conn:
            _return_to_pool(self._conn)
            self._conn = None

    def __enter__(self):
        return self._get_conn()

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.rollback()
        else:
            self.commit()
        self.close()
        return False


def get_db():
    """
    Get database connection - works both ways:

    # Pattern 1: Context manager (recommended)
    with get_db() as conn:
        cursor = conn.cursor()
        ...

    # Pattern 2: Direct (legacy)
    conn = get_db()
    cursor = conn.cursor()
    ...
    conn.close()
    """
    return DBConnection()


# Legacy compatibility - some modules import these directly
def get_connection():
    """Legacy: Get a raw database connection. Caller must close it."""
    return _create_connection()


def return_connection(conn):
    """Legacy: Close a connection (no pooling, just closes)"""
    if conn:
        try:
            conn.close()
        except:
            pass

def init_db():
    """Initialize PostgreSQL database tables"""
    with get_db() as conn:
        cursor = conn.cursor()

        # Users table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                nome TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                role TEXT DEFAULT 'operador',
                senha_hash TEXT,
                tutorial_concluido BOOLEAN DEFAULT FALSE,
                data_criacao TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ultimo_acesso TIMESTAMP
            )
        ''')

        # Insert default users
        cursor.execute('''
            INSERT INTO users (nome, email, role, tutorial_concluido)
            VALUES ('Renato', 'renato@almeida-prado.com', 'admin', FALSE)
            ON CONFLICT (email) DO NOTHING
        ''')
        cursor.execute('''
            INSERT INTO users (nome, email, role, tutorial_concluido)
            VALUES ('Andressa Santos', 'andressa@almeida-prado.com', 'operador', FALSE)
            ON CONFLICT (email) DO NOTHING
        ''')

        # Prospects table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS prospects (
                id SERIAL PRIMARY KEY,
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

        # Create index on email (not unique - allow duplicates)
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_prospects_email
            ON prospects(email) WHERE email IS NOT NULL AND email != ''
        ''')

        # Meetings table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS meetings (
                id SERIAL PRIMARY KEY,
                prospect_id INTEGER NOT NULL REFERENCES prospects(id),
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
                proximos_passos TEXT
            )
        ''')

        # ICP analysis table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS icp_analysis (
                id SERIAL PRIMARY KEY,
                data_analise TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                analysis_data TEXT NOT NULL
            )
        ''')

        # Sales arguments table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sales_arguments (
                id SERIAL PRIMARY KEY,
                argumento TEXT NOT NULL,
                categoria TEXT,
                efetividade_score REAL DEFAULT 0,
                vezes_usado INTEGER DEFAULT 0,
                vezes_converteu INTEGER DEFAULT 0,
                objecao_relacionada TEXT,
                data_criacao TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Activity log table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS activity_log (
                id SERIAL PRIMARY KEY,
                prospect_id INTEGER REFERENCES prospects(id),
                usuario TEXT,
                acao TEXT NOT NULL,
                detalhes TEXT,
                data_hora TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Interactions table (timeline de interações com prospects)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS interactions (
                id SERIAL PRIMARY KEY,
                prospect_id INTEGER REFERENCES prospects(id) ON DELETE CASCADE,
                tipo TEXT NOT NULL,
                titulo TEXT,
                descricao TEXT,
                data_interacao TIMESTAMP,
                fathom_link TEXT,
                fathom_summary TEXT,
                tags TEXT DEFAULT '[]',
                sentimento TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Create index on prospect_id for faster timeline queries
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_interactions_prospect
            ON interactions(prospect_id)
        ''')

        # ============== RAP - Assistente Pessoal Tables ==============

        # Google accounts for multi-account OAuth
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS google_accounts (
                id SERIAL PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                tipo TEXT NOT NULL,
                access_token TEXT,
                refresh_token TEXT,
                token_expiry TIMESTAMP,
                scopes JSONB,
                conectado BOOLEAN DEFAULT TRUE,
                ultima_sync TIMESTAMP,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Unified contacts table (all 12,498 contacts)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS contacts (
                id SERIAL PRIMARY KEY,
                nome TEXT NOT NULL,
                apelido TEXT,
                empresa TEXT,
                cargo TEXT,
                emails JSONB DEFAULT '[]',
                telefones JSONB DEFAULT '[]',
                linkedin TEXT,
                foto_url TEXT,
                linkedin_headline TEXT,
                linkedin_posts JSONB DEFAULT '[]',
                empresa_dados JSONB DEFAULT '{}',
                ultimo_enriquecimento TIMESTAMP,
                enriquecimento_status TEXT,
                contexto TEXT DEFAULT 'professional',
                categorias JSONB DEFAULT '[]',
                tags JSONB DEFAULT '[]',
                aniversario DATE,
                datas_importantes JSONB DEFAULT '[]',
                google_contact_id TEXT UNIQUE,
                origem TEXT,
                resumo_ai TEXT,
                insights_ai JSONB DEFAULT '{}',
                ultimo_contato TIMESTAMP,
                total_interacoes INTEGER DEFAULT 0,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_contacts_nome
            ON contacts(nome)
        ''')

        # Adicionar colunas de scoring à tabela contacts (se não existirem)
        cursor.execute('''
            ALTER TABLE contacts
            ADD COLUMN IF NOT EXISTS score INTEGER DEFAULT 0,
            ADD COLUMN IF NOT EXISTS tier TEXT DEFAULT 'E',
            ADD COLUMN IF NOT EXISTS score_breakdown TEXT DEFAULT '{}',
            ADD COLUMN IF NOT EXISTS score_reasons TEXT DEFAULT '[]'
        ''')

        # Adicionar colunas de Circulos à tabela contacts (se não existirem)
        cursor.execute('''
            ALTER TABLE contacts
            ADD COLUMN IF NOT EXISTS circulo INTEGER DEFAULT 5,
            ADD COLUMN IF NOT EXISTS circulo_manual BOOLEAN DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS frequencia_ideal_dias INTEGER,
            ADD COLUMN IF NOT EXISTS ultimo_calculo_circulo TIMESTAMP,
            ADD COLUMN IF NOT EXISTS health_score INTEGER DEFAULT 50
        ''')

        # Sistema dual de circulos (Pessoal + Profissional)
        cursor.execute('''
            ALTER TABLE contacts
            ADD COLUMN IF NOT EXISTS circulo_pessoal INTEGER,
            ADD COLUMN IF NOT EXISTS circulo_profissional INTEGER,
            ADD COLUMN IF NOT EXISTS circulo_pessoal_manual BOOLEAN DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS circulo_profissional_manual BOOLEAN DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS health_pessoal INTEGER,
            ADD COLUMN IF NOT EXISTS health_profissional INTEGER
        ''')

        # Enderecos e Relacionamentos - Gestão completa de contatos
        cursor.execute('''
            ALTER TABLE contacts
            ADD COLUMN IF NOT EXISTS enderecos JSONB DEFAULT '[]',
            ADD COLUMN IF NOT EXISTS relacionamentos JSONB DEFAULT '[]'
        ''')
        # enderecos: [{"tipo": "residencial", "logradouro": "...", "cidade": "...", "estado": "...", "cep": "...", "pais": "Brasil"}]
        # relacionamentos: [{"tipo": "conjuge", "nome": "João", "contact_id": 123}]

        # Adicionar colunas de Enriquecimento Avancado
        cursor.execute('''
            ALTER TABLE contacts
            ADD COLUMN IF NOT EXISTS relationship_context TEXT,
            ADD COLUMN IF NOT EXISTS company_website TEXT,
            ADD COLUMN IF NOT EXISTS enrichment_sources JSONB DEFAULT '[]',
            ADD COLUMN IF NOT EXISTS last_web_enrichment TIMESTAMP,
            ADD COLUMN IF NOT EXISTS manual_notes TEXT
        ''')

        # Coluna para controlar tentativa de busca de avatar
        cursor.execute('''
            ALTER TABLE contacts
            ADD COLUMN IF NOT EXISTS avatar_checked_at TIMESTAMP
        ''')

        # Indices para Circulos
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_contacts_circulo
            ON contacts(circulo)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_contacts_health
            ON contacts(health_score)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_contacts_emails
            ON contacts USING GIN(emails)
        ''')

        # Link between contacts and prospects
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS contact_prospect_link (
                id SERIAL PRIMARY KEY,
                contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
                prospect_id INTEGER REFERENCES prospects(id) ON DELETE CASCADE,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(contact_id, prospect_id)
            )
        ''')

        # Contact interactions (timeline de interações manuais)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS contact_interactions (
                id SERIAL PRIMARY KEY,
                contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
                tipo TEXT NOT NULL,
                titulo TEXT,
                descricao TEXT,
                data_interacao TIMESTAMP,
                tags JSONB DEFAULT '[]',
                sentimento TEXT,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_contact_interactions_contact
            ON contact_interactions(contact_id)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_contact_interactions_data
            ON contact_interactions(data_interacao DESC)
        ''')

        # Conversations (unified email + whatsapp threads)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS conversations (
                id SERIAL PRIMARY KEY,
                contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
                canal TEXT NOT NULL,
                external_id TEXT,
                assunto TEXT,
                ultimo_mensagem TIMESTAMP,
                total_mensagens INTEGER DEFAULT 0,
                status TEXT DEFAULT 'open',
                resumo_ai TEXT,
                sentimento TEXT,
                requer_resposta BOOLEAN DEFAULT FALSE,
                resposta_sugerida TEXT,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_conversations_contact
            ON conversations(contact_id)
        ''')

        # Individual messages
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
                contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
                external_id TEXT,
                direcao TEXT NOT NULL,
                conteudo TEXT,
                conteudo_html TEXT,
                anexos JSONB DEFAULT '[]',
                metadata JSONB DEFAULT '{}',
                resumo_ai TEXT,
                acoes_extraidas JSONB DEFAULT '[]',
                enviado_em TIMESTAMP,
                recebido_em TIMESTAMP,
                lido_em TIMESTAMP,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_messages_conversation
            ON messages(conversation_id)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_messages_contact
            ON messages(contact_id)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_messages_enviado
            ON messages(enviado_em DESC NULLS LAST)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_messages_external_id
            ON messages(external_id) WHERE external_id IS NOT NULL
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_contacts_ultimo_contato
            ON contacts(ultimo_contato DESC NULLS LAST)
        ''')

        # Contact memories (historical interactions)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS contact_memories (
                id SERIAL PRIMARY KEY,
                contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
                tipo TEXT NOT NULL,
                subtipo TEXT,
                source_table TEXT,
                source_id INTEGER,
                titulo TEXT,
                resumo TEXT NOT NULL,
                conteudo_completo TEXT,
                importancia INTEGER DEFAULT 5,
                e_marco BOOLEAN DEFAULT FALSE,
                fatos_importantes JSONB DEFAULT '[]',
                topicos JSONB DEFAULT '[]',
                compromissos JSONB DEFAULT '[]',
                data_ocorrencia TIMESTAMP NOT NULL,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_memories_contact
            ON contact_memories(contact_id)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_memories_data
            ON contact_memories(data_ocorrencia)
        ''')

        # Contact facts (AI-extracted)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS contact_facts (
                id SERIAL PRIMARY KEY,
                contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
                categoria TEXT NOT NULL,
                fato TEXT NOT NULL,
                fonte TEXT,
                source_memory_id INTEGER REFERENCES contact_memories(id),
                confianca FLOAT DEFAULT 0.8,
                verificado BOOLEAN DEFAULT FALSE,
                valido_desde DATE,
                valido_ate DATE,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_facts_contact
            ON contact_facts(contact_id)
        ''')

        # Contact Briefings - AI-generated briefings persistidos
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS contact_briefings (
                id SERIAL PRIMARY KEY,
                contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
                content TEXT NOT NULL,
                summary TEXT,
                opportunities JSONB DEFAULT '[]',
                next_steps JSONB DEFAULT '[]',
                talking_points JSONB DEFAULT '[]',
                generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                feedback TEXT,
                actions_taken JSONB DEFAULT '[]',
                is_current BOOLEAN DEFAULT TRUE,
                health_at_generation INTEGER,
                circulo_at_generation INTEGER
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_briefings_contact
            ON contact_briefings(contact_id)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_briefings_current
            ON contact_briefings(contact_id, is_current) WHERE is_current = TRUE
        ''')

        # Tasks
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                id SERIAL PRIMARY KEY,
                titulo TEXT NOT NULL,
                descricao TEXT,
                origem TEXT,
                source_table TEXT,
                source_id INTEGER,
                contact_id INTEGER REFERENCES contacts(id),
                prospect_id INTEGER REFERENCES prospects(id),
                project_id INTEGER REFERENCES projects(id),
                data_criacao TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                data_vencimento TIMESTAMP,
                data_conclusao TIMESTAMP,
                status TEXT DEFAULT 'pending',
                prioridade INTEGER DEFAULT 5,
                ai_generated BOOLEAN DEFAULT FALSE,
                confianca_ai FLOAT,
                recorrente BOOLEAN DEFAULT FALSE,
                recurrence_rule TEXT,
                tags JSONB DEFAULT '[]',
                contexto TEXT DEFAULT 'professional',
                google_task_id TEXT,
                google_tasklist_id TEXT DEFAULT '@default',
                last_synced_at TIMESTAMP,
                sync_status TEXT DEFAULT 'local_only',
                etag TEXT
            )
        ''')

        # Add Google sync columns if they don't exist (migration)
        for col, col_def in [
            ('google_task_id', 'TEXT'),
            ('google_tasklist_id', "TEXT DEFAULT '@default'"),
            ('last_synced_at', 'TIMESTAMP'),
            ('sync_status', "TEXT DEFAULT 'local_only'"),
            ('etag', 'TEXT'),
            ('project_id', 'INTEGER REFERENCES projects(id)'),
            ('conselhoos_raci_id', 'TEXT')
        ]:
            try:
                cursor.execute(f'ALTER TABLE tasks ADD COLUMN IF NOT EXISTS {col} {col_def}')
            except:
                pass

        # Index for ConselhoOS RACI sync lookups
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_tasks_conselhoos_raci
            ON tasks(conselhoos_raci_id) WHERE conselhoos_raci_id IS NOT NULL
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_tasks_status
            ON tasks(status)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_tasks_vencimento
            ON tasks(data_vencimento)
        ''')

        # Reminders
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS reminders (
                id SERIAL PRIMARY KEY,
                contact_id INTEGER REFERENCES contacts(id),
                tipo TEXT NOT NULL,
                titulo TEXT NOT NULL,
                descricao TEXT,
                data_lembrete TIMESTAMP NOT NULL,
                antecedencia_dias INTEGER DEFAULT 0,
                recorrente BOOLEAN DEFAULT FALSE,
                recurrence_rule TEXT,
                status TEXT DEFAULT 'pending',
                notificado_em TIMESTAMP,
                ai_generated BOOLEAN DEFAULT FALSE,
                razao_ai TEXT,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_reminders_data
            ON reminders(data_lembrete)
        ''')

        # ConselhoOS - Companies
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS conselhoos_companies (
                id SERIAL PRIMARY KEY,
                nome TEXT NOT NULL,
                razao_social TEXT,
                cnpj TEXT,
                setor TEXT,
                descricao TEXT,
                website TEXT,
                logo_url TEXT,
                tipo_conselho TEXT,
                papel_renato TEXT,
                data_inicio DATE,
                data_fim DATE,
                frequencia_reunioes TEXT,
                proximo_encontro DATE,
                ativo BOOLEAN DEFAULT TRUE,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # ConselhoOS - Board Members
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS conselhoos_board_members (
                id SERIAL PRIMARY KEY,
                company_id INTEGER REFERENCES conselhoos_companies(id) ON DELETE CASCADE,
                contact_id INTEGER REFERENCES contacts(id),
                nome TEXT NOT NULL,
                cargo TEXT,
                papel TEXT,
                data_inicio DATE,
                data_fim DATE,
                ativo BOOLEAN DEFAULT TRUE,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # ConselhoOS - Meetings
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS conselhoos_meetings (
                id SERIAL PRIMARY KEY,
                company_id INTEGER REFERENCES conselhoos_companies(id) ON DELETE CASCADE,
                calendar_event_id INTEGER,
                tipo TEXT,
                numero INTEGER,
                data TIMESTAMP NOT NULL,
                local TEXT,
                pauta JSONB DEFAULT '[]',
                ata_url TEXT,
                documentos JSONB DEFAULT '[]',
                presentes JSONB DEFAULT '[]',
                deliberacoes JSONB DEFAULT '[]',
                pendencias JSONB DEFAULT '[]',
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # ============== AI ADVANCED Tables ==============

        # AI Suggestions - Sugestoes geradas pela IA
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ai_suggestions (
                id SERIAL PRIMARY KEY,
                contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
                tipo TEXT NOT NULL,
                titulo TEXT NOT NULL,
                descricao TEXT,
                razao TEXT,
                dados JSONB DEFAULT '{}',
                prioridade INTEGER DEFAULT 5,
                status TEXT DEFAULT 'pending',
                aceita_em TIMESTAMP,
                descartada_em TIMESTAMP,
                motivo_descarte TEXT,
                executada_em TIMESTAMP,
                resultado TEXT,
                validade TIMESTAMP,
                confianca FLOAT DEFAULT 0.8,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_ai_suggestions_contact
            ON ai_suggestions(contact_id)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_ai_suggestions_status
            ON ai_suggestions(status)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_ai_suggestions_tipo
            ON ai_suggestions(tipo)
        ''')

        # AI Automations - Regras de automacao
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ai_automations (
                id SERIAL PRIMARY KEY,
                nome TEXT NOT NULL,
                descricao TEXT,
                trigger_type TEXT NOT NULL,
                trigger_config JSONB DEFAULT '{}',
                action_type TEXT NOT NULL,
                action_config JSONB DEFAULT '{}',
                ativo BOOLEAN DEFAULT TRUE,
                ultima_execucao TIMESTAMP,
                total_execucoes INTEGER DEFAULT 0,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Health Predictions - Previsoes de saude do relacionamento
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS health_predictions (
                id SERIAL PRIMARY KEY,
                contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
                health_atual INTEGER,
                health_previsto INTEGER,
                tendencia TEXT,
                dias_previsao INTEGER DEFAULT 30,
                fatores JSONB DEFAULT '[]',
                recomendacoes JSONB DEFAULT '[]',
                data_previsao TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                acerto BOOLEAN
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_health_predictions_contact
            ON health_predictions(contact_id)
        ''')

        # Message Templates - Templates de mensagens
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS message_templates (
                id SERIAL PRIMARY KEY,
                nome TEXT NOT NULL,
                categoria TEXT NOT NULL,
                canal TEXT,
                assunto TEXT,
                corpo TEXT NOT NULL,
                variaveis JSONB DEFAULT '[]',
                tags JSONB DEFAULT '[]',
                uso_count INTEGER DEFAULT 0,
                ultima_uso TIMESTAMP,
                ativo BOOLEAN DEFAULT TRUE,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_message_templates_categoria
            ON message_templates(categoria)
        ''')

        # AI Digests - Resumos periodicos
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ai_digests (
                id SERIAL PRIMARY KEY,
                tipo TEXT NOT NULL,
                periodo_inicio TIMESTAMP NOT NULL,
                periodo_fim TIMESTAMP NOT NULL,
                titulo TEXT,
                resumo TEXT,
                highlights JSONB DEFAULT '[]',
                metricas JSONB DEFAULT '{}',
                sugestoes JSONB DEFAULT '[]',
                contatos_destaque JSONB DEFAULT '[]',
                enviado BOOLEAN DEFAULT FALSE,
                enviado_em TIMESTAMP,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_ai_digests_tipo
            ON ai_digests(tipo)
        ''')

        # =========================================================================
        # EMAIL TRIAGE TABLES
        # =========================================================================

        # Email Triage - Triagem de emails para atenção
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS email_triage (
                id SERIAL PRIMARY KEY,
                message_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
                conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
                contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,

                -- Classificação
                needs_attention BOOLEAN DEFAULT TRUE,
                priority INTEGER DEFAULT 5,
                classification TEXT,

                -- Razões da classificação
                classification_reasons JSONB DEFAULT '[]',

                -- Tags sugeridas pela IA
                suggested_tags JSONB DEFAULT '[]',

                -- Ações sugeridas
                suggested_actions JSONB DEFAULT '[]',

                -- Status do workflow
                status TEXT DEFAULT 'pending',
                approved_tags JSONB,
                approved_at TIMESTAMP,
                dismissed_at TIMESTAMP,
                actioned_at TIMESTAMP,
                action_taken TEXT,

                -- Metadados
                account_type TEXT,
                ai_confidence FLOAT DEFAULT 0.8,
                expires_at TIMESTAMP,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_email_triage_status
            ON email_triage(status)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_email_triage_priority
            ON email_triage(priority DESC)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_email_triage_contact
            ON email_triage(contact_id)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_email_triage_message
            ON email_triage(message_id)
        ''')

        # Email Triage Rules - Regras de classificação
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS email_triage_rules (
                id SERIAL PRIMARY KEY,
                nome TEXT NOT NULL,
                descricao TEXT,

                -- Condições
                conditions JSONB NOT NULL,

                -- Ações automáticas
                auto_classify TEXT,
                auto_tags JSONB DEFAULT '[]',
                auto_priority INTEGER,
                requires_approval BOOLEAN DEFAULT TRUE,

                -- Status
                ativo BOOLEAN DEFAULT TRUE,
                ordem INTEGER DEFAULT 100,

                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_email_triage_rules_ativo
            ON email_triage_rules(ativo)
        ''')

        # =========================================================================
        # CALENDAR EVENTS TABLES
        # =========================================================================

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS calendar_events (
                id SERIAL PRIMARY KEY,
                google_event_id TEXT UNIQUE,
                summary TEXT NOT NULL,
                description TEXT,
                location TEXT,
                start_datetime TIMESTAMP NOT NULL,
                end_datetime TIMESTAMP NOT NULL,
                all_day BOOLEAN DEFAULT FALSE,
                timezone TEXT DEFAULT 'America/Sao_Paulo',
                recurring_event_id TEXT,
                recurrence_rule TEXT,
                contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
                prospect_id INTEGER REFERENCES prospects(id) ON DELETE SET NULL,
                ai_suggestion_id INTEGER,
                conference_url TEXT,
                conference_type TEXT,
                attendees JSONB DEFAULT '[]',
                status TEXT DEFAULT 'confirmed',
                etag TEXT,
                source TEXT DEFAULT 'google',
                last_synced_at TIMESTAMP,
                local_only BOOLEAN DEFAULT FALSE,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_calendar_events_google_id
            ON calendar_events(google_event_id)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_calendar_events_contact
            ON calendar_events(contact_id)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_calendar_events_start
            ON calendar_events(start_datetime)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_calendar_events_source
            ON calendar_events(source)
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS calendar_sync_state (
                id SERIAL PRIMARY KEY,
                google_account_email TEXT UNIQUE NOT NULL,
                calendar_id TEXT DEFAULT 'primary',
                sync_token TEXT,
                last_full_sync TIMESTAMP,
                last_incremental_sync TIMESTAMP,
                events_synced INTEGER DEFAULT 0,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # ConselhoOS Links - vincula contatos INTEL com empresas do ConselhoOS
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS conselhoos_links (
                id SERIAL PRIMARY KEY,
                contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
                conselhoos_empresa_id UUID,
                conselhoos_empresa_nome VARCHAR(255),
                role VARCHAR(100),
                notes TEXT,
                synced_at TIMESTAMP,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_conselhoos_links_contact
            ON conselhoos_links(contact_id)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_conselhoos_links_empresa
            ON conselhoos_links(conselhoos_empresa_id)
        ''')

        # =========================================================================
        # PROJECTS TABLES - Sistema de Projetos Pessoais/Profissionais
        # =========================================================================

        # Main projects table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS projects (
                id SERIAL PRIMARY KEY,
                nome TEXT NOT NULL,
                descricao TEXT,
                tipo TEXT NOT NULL DEFAULT 'negocio',
                status TEXT DEFAULT 'ativo',
                prioridade INTEGER DEFAULT 5,
                data_inicio DATE,
                data_previsao DATE,
                data_conclusao DATE,
                cor TEXT DEFAULT '#6366f1',
                icone TEXT DEFAULT 'folder',
                owner_contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
                empresa_relacionada TEXT,
                valor_estimado DECIMAL(15,2),
                notas TEXT,
                tags JSONB DEFAULT '[]',
                metadata JSONB DEFAULT '{}',
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_projects_tipo
            ON projects(tipo)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_projects_status
            ON projects(status)
        ''')

        # Project members - pessoas envolvidas no projeto
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS project_members (
                id SERIAL PRIMARY KEY,
                project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
                contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
                papel TEXT,
                responsabilidades TEXT,
                adicionado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(project_id, contact_id)
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_project_members_project
            ON project_members(project_id)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_project_members_contact
            ON project_members(contact_id)
        ''')

        # Project milestones - marcos importantes
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS project_milestones (
                id SERIAL PRIMARY KEY,
                project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
                titulo TEXT NOT NULL,
                descricao TEXT,
                data_prevista DATE,
                data_conclusao DATE,
                status TEXT DEFAULT 'pendente',
                ordem INTEGER DEFAULT 0,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_project_milestones_project
            ON project_milestones(project_id)
        ''')

        cursor.execute('''
            ALTER TABLE project_milestones
            ADD COLUMN IF NOT EXISTS email_thread_id TEXT,
            ADD COLUMN IF NOT EXISTS email_message_id TEXT,
            ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}'
        ''')

        # Project messages - emails/whatsapp relacionados
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS project_messages (
                id SERIAL PRIMARY KEY,
                project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
                message_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
                conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
                vinculo_tipo TEXT DEFAULT 'auto',
                relevancia INTEGER DEFAULT 5,
                notas TEXT,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(project_id, message_id)
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_project_messages_project
            ON project_messages(project_id)
        ''')

        # Project events - reunioes/eventos do calendario
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS project_events (
                id SERIAL PRIMARY KEY,
                project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
                calendar_event_id INTEGER REFERENCES calendar_events(id) ON DELETE CASCADE,
                google_event_id TEXT,
                vinculo_tipo TEXT DEFAULT 'auto',
                notas TEXT,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_project_events_project
            ON project_events(project_id)
        ''')

        # Project WhatsApp groups - grupos vinculados a projetos
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS project_whatsapp_groups (
                id SERIAL PRIMARY KEY,
                project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
                group_jid TEXT NOT NULL,
                group_name TEXT,
                ativo BOOLEAN DEFAULT TRUE,
                last_synced_at TIMESTAMP,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(project_id, group_jid)
            )
        ''')

        # Social groups cache
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS social_groups_cache (
                id SERIAL PRIMARY KEY,
                group_jid TEXT UNIQUE NOT NULL,
                group_name TEXT NOT NULL,
                total_participants INTEGER DEFAULT 0,
                participants_phones JSONB DEFAULT '[]'::jsonb,
                known_contact_ids JSONB DEFAULT '[]'::jsonb,
                known_count INTEGER DEFAULT 0,
                health_medio INTEGER,
                labels JSONB DEFAULT '[]'::jsonb,
                sync_enabled BOOLEAN DEFAULT FALSE,
                last_synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Project notes - timeline de atualizacoes/notas
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS project_notes (
                id SERIAL PRIMARY KEY,
                project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
                tipo TEXT DEFAULT 'nota',
                titulo TEXT,
                conteudo TEXT NOT NULL,
                autor TEXT,
                anexos JSONB DEFAULT '[]',
                metadata JSONB DEFAULT '{}',
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_project_notes_project
            ON project_notes(project_id)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_project_notes_tipo
            ON project_notes(tipo)
        ''')

        # Link tasks to projects
        cursor.execute('''
            ALTER TABLE tasks
            ADD COLUMN IF NOT EXISTS project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_tasks_project
            ON tasks(project_id)
        ''')

        # ============== LINKEDIN ENRICHMENT Tables ==============

        # Expand contacts table with more LinkedIn fields
        cursor.execute('''
            ALTER TABLE contacts
            ADD COLUMN IF NOT EXISTS linkedin_location TEXT,
            ADD COLUMN IF NOT EXISTS linkedin_about TEXT,
            ADD COLUMN IF NOT EXISTS linkedin_experience JSONB DEFAULT '[]',
            ADD COLUMN IF NOT EXISTS linkedin_education JSONB DEFAULT '[]',
            ADD COLUMN IF NOT EXISTS linkedin_skills JSONB DEFAULT '[]',
            ADD COLUMN IF NOT EXISTS linkedin_connections INTEGER,
            ADD COLUMN IF NOT EXISTS linkedin_open_to_work BOOLEAN DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS linkedin_last_activity TEXT,
            ADD COLUMN IF NOT EXISTS linkedin_enriched_at TIMESTAMP,
            ADD COLUMN IF NOT EXISTS linkedin_previous_company TEXT,
            ADD COLUMN IF NOT EXISTS linkedin_previous_title TEXT,
            ADD COLUMN IF NOT EXISTS linkedin_job_changed_at TIMESTAMP
        ''')

        # LinkedIn enrichment history - tracks changes over time
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS linkedin_enrichment_history (
                id SERIAL PRIMARY KEY,
                contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
                empresa_anterior TEXT,
                cargo_anterior TEXT,
                empresa_nova TEXT,
                cargo_nova TEXT,
                headline_anterior TEXT,
                headline_nova TEXT,
                tipo_mudanca TEXT,
                detectado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                notificado BOOLEAN DEFAULT FALSE,
                dados_completos JSONB DEFAULT '{}'
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_linkedin_history_contact
            ON linkedin_enrichment_history(contact_id)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_linkedin_history_tipo
            ON linkedin_enrichment_history(tipo_mudanca)
        ''')

        # =========================================================================
        # ACTION PROPOSALS - Sistema de Propostas de Ação do INTEL Proativo
        # =========================================================================

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS action_proposals (
                id SERIAL PRIMARY KEY,
                contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
                message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
                conversation_id INTEGER REFERENCES conversations(id) ON DELETE SET NULL,

                -- Tipo e detalhes da ação proposta
                action_type TEXT NOT NULL,
                action_params JSONB DEFAULT '{}',

                -- Contexto e razão
                trigger_text TEXT,
                ai_reasoning TEXT,
                confidence FLOAT DEFAULT 0.5,
                urgency TEXT DEFAULT 'medium',

                -- Status: pending, accepted, rejected, executed, expired
                status TEXT DEFAULT 'pending',

                -- UI display
                title TEXT NOT NULL,
                description TEXT,
                options JSONB DEFAULT '[]',

                -- Timestamps
                expires_at TIMESTAMP,
                responded_at TIMESTAMP,
                executed_at TIMESTAMP,
                execution_result JSONB,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_action_proposals_status
            ON action_proposals(status)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_action_proposals_contact
            ON action_proposals(contact_id)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_action_proposals_pending
            ON action_proposals(status, criado_em DESC) WHERE status = 'pending'
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_action_proposals_urgency
            ON action_proposals(urgency, criado_em DESC) WHERE status = 'pending'
        ''')

        # Push Subscriptions - Browser push notification subscriptions
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id SERIAL PRIMARY KEY,
                endpoint TEXT UNIQUE NOT NULL,
                keys JSONB NOT NULL,
                user_id TEXT,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_push_subscriptions_user
            ON push_subscriptions(user_id)
        ''')

        # Analyzer Feedback - Learning from user responses to proposals
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS analyzer_feedback (
                id SERIAL PRIMARY KEY,
                proposal_id INTEGER REFERENCES action_proposals(id) ON DELETE CASCADE,
                intent_type TEXT NOT NULL,
                action_type TEXT NOT NULL,
                confidence FLOAT,
                urgency TEXT,

                -- User response
                user_action TEXT NOT NULL,  -- 'accepted', 'rejected', 'dismissed', 'expired'
                option_chosen TEXT,

                -- Context for learning
                contact_id INTEGER,
                message_preview TEXT,

                -- Timestamps
                responded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_analyzer_feedback_intent
            ON analyzer_feedback(intent_type)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_analyzer_feedback_action
            ON analyzer_feedback(user_action)
        ''')

        # Analyzer Settings - User preferences for sensitivity
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS analyzer_settings (
                id SERIAL PRIMARY KEY,
                setting_key TEXT UNIQUE NOT NULL,
                setting_value JSONB NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Insert default settings if not exist
        default_intents = [
            'reschedule_meeting', 'cancel_meeting', 'confirm_meeting', 'urgent_request',
            'question', 'payment_mention', 'deadline_mention', 'important_info',
            'introduction_request', 'opportunity_signal', 'complaint', 'meeting_request', 'follow_up_needed'
        ]
        cursor.execute('''
            INSERT INTO analyzer_settings (setting_key, setting_value)
            VALUES
                ('min_confidence', '0.7'),
                ('enabled_intents', %s),
                ('urgency_threshold', '"medium"')
            ON CONFLICT (setting_key) DO NOTHING
        ''', (json.dumps(default_intents),))

        # Background Jobs - Track long-running background tasks
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS background_jobs (
                id SERIAL PRIMARY KEY,
                job_type TEXT NOT NULL,
                status TEXT DEFAULT 'running',
                total_items INTEGER DEFAULT 0,
                processed_items INTEGER DEFAULT 0,
                success_count INTEGER DEFAULT 0,
                failed_count INTEGER DEFAULT 0,
                skipped_count INTEGER DEFAULT 0,
                result JSONB,
                error TEXT,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            )
        ''')

        # Timeline Summaries - Cache de resumos IA para grupos de mensagens
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS timeline_summaries (
                id SERIAL PRIMARY KEY,
                contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
                cache_hash VARCHAR(16) NOT NULL,
                summary TEXT,
                message_count INTEGER,
                channel VARCHAR(50),
                msg_date DATE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(contact_id, cache_hash)
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_timeline_summaries_contact
            ON timeline_summaries(contact_id)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_timeline_summaries_hash
            ON timeline_summaries(contact_id, cache_hash)
        ''')

        # Rodas de Relacionamento - contexto extraido de mensagens
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS contact_rodas (
                id SERIAL PRIMARY KEY,
                contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
                message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,

                -- Tipo: promessa, favor_recebido, favor_feito, topico, proximo_passo
                tipo TEXT NOT NULL,
                conteudo TEXT NOT NULL,
                tags TEXT[] DEFAULT '{}',

                -- Status: pendente, cumprido, expirado
                status TEXT DEFAULT 'pendente',
                prazo DATE,

                -- IA metadata
                ai_confidence FLOAT DEFAULT 0.5,

                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_rodas_contact
            ON contact_rodas(contact_id)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_rodas_status
            ON contact_rodas(status) WHERE status = 'pendente'
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_rodas_tipo
            ON contact_rodas(tipo)
        ''')

        # Manual contact tracking - "Já contatei" button
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS contact_today_manual (
                id SERIAL PRIMARY KEY,
                contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
                data DATE NOT NULL DEFAULT CURRENT_DATE,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(contact_id, data)
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_contact_today_manual_date
            ON contact_today_manual(data)
        ''')

        # Editorial Calendar - posts para redes sociais
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS editorial_posts (
                id SERIAL PRIMARY KEY,
                project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,

                -- Conteúdo original (do site)
                article_slug TEXT,
                article_title TEXT NOT NULL,
                article_url TEXT,
                article_description TEXT,

                -- Adaptação para redes
                canal TEXT NOT NULL DEFAULT 'linkedin',
                tipo TEXT DEFAULT 'repost',
                titulo_adaptado TEXT,
                conteudo_adaptado TEXT,
                hashtags JSONB DEFAULT '[]',
                imagem_url TEXT,

                -- Agendamento
                status TEXT DEFAULT 'draft',
                data_publicacao TIMESTAMP,
                data_publicado TIMESTAMP,

                -- Integração com Tasks e Calendar
                task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
                calendar_event_id INTEGER REFERENCES calendar_events(id) ON DELETE SET NULL,

                -- Métricas pós-publicação
                metricas JSONB DEFAULT '{}',
                url_publicado TEXT,

                -- Metadata
                prioridade INTEGER DEFAULT 5,
                notas TEXT,
                tags JSONB DEFAULT '[]',
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # AI categorization fields for editorial posts
        cursor.execute('''
            ALTER TABLE editorial_posts
            ADD COLUMN IF NOT EXISTS ai_categoria TEXT,
            ADD COLUMN IF NOT EXISTS ai_subcategoria TEXT,
            ADD COLUMN IF NOT EXISTS ai_publico_alvo JSONB DEFAULT '[]',
            ADD COLUMN IF NOT EXISTS ai_tipo_conteudo TEXT,
            ADD COLUMN IF NOT EXISTS ai_complexidade TEXT,
            ADD COLUMN IF NOT EXISTS ai_evergreen BOOLEAN DEFAULT TRUE,
            ADD COLUMN IF NOT EXISTS ai_keywords JSONB DEFAULT '[]',
            ADD COLUMN IF NOT EXISTS ai_gancho_linkedin TEXT,
            ADD COLUMN IF NOT EXISTS ai_tempo_leitura INTEGER,
            ADD COLUMN IF NOT EXISTS ai_score_relevancia INTEGER,
            ADD COLUMN IF NOT EXISTS ai_analise_completa JSONB DEFAULT '{}',
            ADD COLUMN IF NOT EXISTS ai_analisado_em TIMESTAMP
        ''')

        # LinkedIn metrics columns for easier reporting
        cursor.execute('''
            ALTER TABLE editorial_posts
            ADD COLUMN IF NOT EXISTS linkedin_post_url TEXT,
            ADD COLUMN IF NOT EXISTS linkedin_impressoes INTEGER DEFAULT 0,
            ADD COLUMN IF NOT EXISTS linkedin_reacoes INTEGER DEFAULT 0,
            ADD COLUMN IF NOT EXISTS linkedin_comentarios INTEGER DEFAULT 0,
            ADD COLUMN IF NOT EXISTS linkedin_compartilhamentos INTEGER DEFAULT 0,
            ADD COLUMN IF NOT EXISTS linkedin_cliques INTEGER DEFAULT 0,
            ADD COLUMN IF NOT EXISTS linkedin_metricas_em TIMESTAMP,
            ADD COLUMN IF NOT EXISTS hot_take_id INTEGER
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_editorial_posts_status
            ON editorial_posts(status)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_editorial_posts_canal
            ON editorial_posts(canal)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_editorial_posts_data_publicacao
            ON editorial_posts(data_publicacao)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_editorial_posts_project
            ON editorial_posts(project_id)
        ''')

        # Performance indexes for Artigos page
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_editorial_posts_article_url
            ON editorial_posts(article_url) WHERE article_url IS NOT NULL
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_editorial_posts_ai_categoria
            ON editorial_posts(ai_categoria) WHERE ai_categoria IS NOT NULL
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_editorial_posts_ai_score
            ON editorial_posts(ai_score_relevancia DESC NULLS LAST)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_editorial_posts_ai_evergreen
            ON editorial_posts(ai_evergreen) WHERE ai_evergreen = TRUE
        ''')

        # Composite index for Artigos filtering
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_editorial_posts_artigos
            ON editorial_posts(article_url, ai_score_relevancia DESC NULLS LAST, criado_em DESC)
            WHERE article_url IS NOT NULL
        ''')

        # ============== Hot Takes Performance Indexes ==============
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_hot_takes_status
            ON hot_takes(status)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_hot_takes_created_at
            ON hot_takes(created_at DESC)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_hot_takes_status_created
            ON hot_takes(status, created_at DESC)
        ''')

        # ============== Google Drive Integration ==============

        # Add google_drive_folder_id to projects
        cursor.execute('''
            ALTER TABLE projects
            ADD COLUMN IF NOT EXISTS google_drive_folder_id TEXT
        ''')

        # Add google_drive_folder_id to contacts
        cursor.execute('''
            ALTER TABLE contacts
            ADD COLUMN IF NOT EXISTS google_drive_folder_id TEXT
        ''')

        # Documents table - stores indexed documents from Google Drive
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS documentos (
                id SERIAL PRIMARY KEY,
                nome TEXT NOT NULL,
                google_drive_id TEXT UNIQUE,
                google_drive_url TEXT,
                mime_type TEXT,
                tamanho_bytes BIGINT,
                pasta_origem_id TEXT,
                tags JSONB DEFAULT '[]',
                descricao TEXT,
                indexado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                indexado_por INTEGER REFERENCES users(id)
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_documentos_google_drive_id
            ON documentos(google_drive_id)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_documentos_pasta
            ON documentos(pasta_origem_id)
        ''')

        # Document links - many-to-many relationships between documents and entities
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS documento_links (
                id SERIAL PRIMARY KEY,
                documento_id INTEGER REFERENCES documentos(id) ON DELETE CASCADE,
                entidade_tipo TEXT NOT NULL,
                entidade_id INTEGER NOT NULL,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(documento_id, entidade_tipo, entidade_id)
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_documento_links_documento
            ON documento_links(documento_id)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_documento_links_entidade
            ON documento_links(entidade_tipo, entidade_id)
        ''')

        # Drive watches - Google Drive push notification channels
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS drive_watches (
                id SERIAL PRIMARY KEY,
                project_id INTEGER UNIQUE REFERENCES projects(id) ON DELETE CASCADE,
                folder_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                resource_id TEXT,
                expiration TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # =========================================================================
        # VEICULOS - Sistema de Controle de Manutenção de Veículos
        # =========================================================================

        # Veiculos - dados do veículo
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS veiculos (
                id SERIAL PRIMARY KEY,
                placa TEXT UNIQUE NOT NULL,
                apelido TEXT,
                marca TEXT NOT NULL,
                modelo TEXT NOT NULL,
                versao TEXT,
                ano_fabricacao INTEGER,
                ano_modelo INTEGER,
                cor TEXT,
                combustivel TEXT,
                renavam TEXT,
                chassi TEXT,
                motor TEXT,
                potencia TEXT,
                km_atual INTEGER DEFAULT 0,
                km_atualizado_em TIMESTAMP,
                foto_url TEXT,
                foto_url_2 TEXT,
                foto_url_3 TEXT,
                proprietario TEXT,
                data_aquisicao DATE,
                valor_aquisicao DECIMAL(15,2),
                observacoes TEXT,
                ativo BOOLEAN DEFAULT TRUE,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_veiculos_placa
            ON veiculos(placa)
        ''')

        # Adicionar coluna para pasta do Google Drive
        cursor.execute('''
            ALTER TABLE veiculos
            ADD COLUMN IF NOT EXISTS google_drive_folder_id TEXT
        ''')

        # Itens do plano de manutenção - o que deve ser feito em cada intervalo
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS veiculo_itens_manutencao (
                id SERIAL PRIMARY KEY,
                veiculo_id INTEGER REFERENCES veiculos(id) ON DELETE CASCADE,
                categoria TEXT NOT NULL,
                item TEXT NOT NULL,
                descricao TEXT,
                intervalo_km INTEGER,
                intervalo_meses INTEGER,
                tipo_acao TEXT DEFAULT 'substituir',
                notas TEXT,
                ativo BOOLEAN DEFAULT TRUE,
                ordem INTEGER DEFAULT 0,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_veiculo_itens_veiculo
            ON veiculo_itens_manutencao(veiculo_id)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_veiculo_itens_categoria
            ON veiculo_itens_manutencao(categoria)
        ''')

        # Adicionar coluna de notas do fabricante
        cursor.execute('''
            ALTER TABLE veiculo_itens_manutencao
            ADD COLUMN IF NOT EXISTS notas_fabricante TEXT
        ''')

        # Histórico de manutenções realizadas
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS veiculo_manutencoes (
                id SERIAL PRIMARY KEY,
                veiculo_id INTEGER REFERENCES veiculos(id) ON DELETE CASCADE,
                item_id INTEGER REFERENCES veiculo_itens_manutencao(id) ON DELETE SET NULL,
                data_manutencao DATE NOT NULL,
                km_manutencao INTEGER NOT NULL,
                tipo_acao TEXT,
                descricao TEXT,
                fornecedor TEXT,
                valor DECIMAL(10,2),
                nota_fiscal_url TEXT,
                relatorio_url TEXT,
                observacoes TEXT,
                ordem_servico_id INTEGER,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_veiculo_manutencoes_veiculo
            ON veiculo_manutencoes(veiculo_id)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_veiculo_manutencoes_data
            ON veiculo_manutencoes(data_manutencao DESC)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_veiculo_manutencoes_item
            ON veiculo_manutencoes(item_id)
        ''')

        # Documentos do veículo (CRLV, seguro, etc)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS veiculo_documentos (
                id SERIAL PRIMARY KEY,
                veiculo_id INTEGER REFERENCES veiculos(id) ON DELETE CASCADE,
                tipo TEXT NOT NULL,
                descricao TEXT,
                arquivo_url TEXT,
                data_emissao DATE,
                data_validade DATE,
                numero TEXT,
                observacoes TEXT,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_veiculo_documentos_veiculo
            ON veiculo_documentos(veiculo_id)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_veiculo_documentos_tipo
            ON veiculo_documentos(tipo)
        ''')

        # Ordens de Serviço - geradas para levar para oficina
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS veiculo_ordens_servico (
                id SERIAL PRIMARY KEY,
                veiculo_id INTEGER REFERENCES veiculos(id) ON DELETE CASCADE,
                numero TEXT UNIQUE,
                data_criacao TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                km_criacao INTEGER NOT NULL,
                status TEXT DEFAULT 'pendente',
                oficina TEXT,
                data_agendamento DATE,
                itens JSONB DEFAULT '[]',
                valor_estimado DECIMAL(10,2),
                valor_final DECIMAL(10,2),
                nota_fiscal_url TEXT,
                relatorio_url TEXT,
                observacoes TEXT,
                data_execucao DATE,
                km_execucao INTEGER,
                atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_veiculo_os_veiculo
            ON veiculo_ordens_servico(veiculo_id)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_veiculo_os_status
            ON veiculo_ordens_servico(status)
        ''')

        # Oficinas (Workshops) table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS oficinas (
                id SERIAL PRIMARY KEY,
                nome TEXT NOT NULL,
                apelido TEXT,
                endereco TEXT,
                cidade TEXT,
                estado TEXT,
                cep TEXT,
                telefone TEXT,
                whatsapp TEXT,
                email TEXT,
                website TEXT,
                contato_nome TEXT,
                contato_id INTEGER,
                especialidades JSONB DEFAULT '[]',
                servicos JSONB DEFAULT '[]',
                notas TEXT,
                google_maps_url TEXT,
                ativo BOOLEAN DEFAULT TRUE,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_oficinas_nome
            ON oficinas(nome)
        ''')

        # ============== News Hub ==============

        # Notícias coletadas de RSS/APIs
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS news_items (
                id SERIAL PRIMARY KEY,
                source TEXT NOT NULL,
                source_url TEXT,
                title TEXT NOT NULL,
                description TEXT,
                link TEXT UNIQUE,
                published_at TIMESTAMP,
                collected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                category TEXT,
                is_trending BOOLEAN DEFAULT FALSE,
                trending_score FLOAT DEFAULT 0,
                relevance_score FLOAT,
                topics JSONB DEFAULT '[]',
                keywords JSONB DEFAULT '[]',
                processed BOOLEAN DEFAULT FALSE,
                ai_summary TEXT
            )
        ''')

        # Adicionar coluna ai_summary se não existir (migração)
        cursor.execute('''
            ALTER TABLE news_items ADD COLUMN IF NOT EXISTS ai_summary TEXT
        ''')

        # Interações do usuário com notícias (para aprendizado)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS news_interactions (
                id SERIAL PRIMARY KEY,
                news_id INTEGER REFERENCES news_items(id) ON DELETE CASCADE,
                user_id INTEGER REFERENCES users(id),
                action TEXT NOT NULL,
                time_spent INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                metadata JSONB DEFAULT '{}'
            )
        ''')

        # Perfil de interesses do usuário (evolui com interações)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_interests (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) UNIQUE,
                topics JSONB DEFAULT '{}',
                sources JSONB DEFAULT '{}',
                keywords_positive JSONB DEFAULT '[]',
                keywords_negative JSONB DEFAULT '[]',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_news_items_collected
            ON news_items(collected_at DESC)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_news_items_trending
            ON news_items(is_trending, trending_score DESC)
            WHERE is_trending = TRUE
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_news_interactions_news
            ON news_interactions(news_id, action)
        ''')

        # Editorial Metrics History - track metrics snapshots over time
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS editorial_metrics_history (
                id SERIAL PRIMARY KEY,
                post_id INTEGER REFERENCES editorial_posts(id) ON DELETE CASCADE,
                impressoes INTEGER DEFAULT 0,
                reacoes INTEGER DEFAULT 0,
                comentarios INTEGER DEFAULT 0,
                compartilhamentos INTEGER DEFAULT 0,
                visitas_perfil INTEGER DEFAULT 0,
                seguidores INTEGER DEFAULT 0,
                salvamentos INTEGER DEFAULT 0,
                coletado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                dias_apos_publicacao INTEGER,
                fonte TEXT DEFAULT 'manual'
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_editorial_metrics_history_post
            ON editorial_metrics_history(post_id, coletado_em DESC)
        ''')

        # Bot conversations (conversation memory for intel-bot)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bot_conversations (
                id SERIAL PRIMARY KEY,
                phone TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                tool_calls JSONB,
                tool_results JSONB,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_bot_conv_phone
            ON bot_conversations(phone, created_at DESC)
        ''')

        # Migration: add labels and sync_enabled to social_groups_cache
        cursor.execute('''
            ALTER TABLE social_groups_cache ADD COLUMN IF NOT EXISTS labels JSONB DEFAULT '[]'::jsonb
        ''')
        cursor.execute('''
            ALTER TABLE social_groups_cache ADD COLUMN IF NOT EXISTS sync_enabled BOOLEAN DEFAULT FALSE
        ''')

        # Group messages - mensagens de grupos WhatsApp sincronizados
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS group_messages (
                id SERIAL PRIMARY KEY,
                group_jid TEXT NOT NULL,
                message_id TEXT UNIQUE,
                sender_phone TEXT,
                sender_name TEXT,
                contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
                content TEXT,
                message_type TEXT DEFAULT 'text',
                timestamp TIMESTAMP NOT NULL,
                from_me BOOLEAN DEFAULT FALSE,
                metadata JSONB DEFAULT '{}',
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_group_messages_jid_ts
            ON group_messages(group_jid, timestamp DESC)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_group_messages_contact
            ON group_messages(contact_id) WHERE contact_id IS NOT NULL
        ''')

        # Track last sync timestamp per group
        cursor.execute('''
            ALTER TABLE social_groups_cache ADD COLUMN IF NOT EXISTS last_message_sync TIMESTAMP
        ''')

        # Project assistant conversation history
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS project_assistant_messages (
                id SERIAL PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_project_assistant_proj
            ON project_assistant_messages(project_id, criado_em DESC)
        ''')

        # System feedback from WhatsApp
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS system_feedback (
                id SERIAL PRIMARY KEY,
                tipo TEXT DEFAULT 'feedback',
                conteudo TEXT NOT NULL,
                screenshot_url TEXT,
                status TEXT DEFAULT 'pending',
                resolved_at TIMESTAMP,
                resolution TEXT,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        conn.commit()
        print("Database initialized successfully")
        return True
