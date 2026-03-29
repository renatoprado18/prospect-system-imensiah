"""
PostgreSQL Database Module for Vercel Postgres
Com connection pooling para melhor performance
"""
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import pool
from contextlib import contextmanager
import threading

# Vercel Postgres connection string
DATABASE_URL = os.getenv("POSTGRES_URL", os.getenv("DATABASE_URL", ""))

# Connection pool (singleton)
_connection_pool = None
_pool_lock = threading.Lock()

def _get_conn_string():
    """Get formatted connection string"""
    if not DATABASE_URL:
        raise Exception("POSTGRES_URL environment variable not set")
    # Vercel uses postgres:// but psycopg2 needs postgresql://
    return DATABASE_URL.replace("postgres://", "postgresql://")

def get_pool():
    """Get or create connection pool (thread-safe singleton)"""
    global _connection_pool
    if _connection_pool is None:
        with _pool_lock:
            if _connection_pool is None:
                conn_string = _get_conn_string()
                _connection_pool = pool.ThreadedConnectionPool(
                    minconn=2,
                    maxconn=10,
                    dsn=conn_string,
                    cursor_factory=RealDictCursor
                )
    return _connection_pool

def get_connection():
    """Get a PostgreSQL connection from pool"""
    return get_pool().getconn()

def return_connection(conn):
    """Return connection to pool"""
    try:
        get_pool().putconn(conn)
    except:
        pass

@contextmanager
def get_db():
    """Context manager for database connections with pooling"""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        return_connection(conn)

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

        # Adicionar colunas de Enriquecimento Avancado
        cursor.execute('''
            ALTER TABLE contacts
            ADD COLUMN IF NOT EXISTS relationship_context TEXT,
            ADD COLUMN IF NOT EXISTS company_website TEXT,
            ADD COLUMN IF NOT EXISTS enrichment_sources JSONB DEFAULT '[]',
            ADD COLUMN IF NOT EXISTS last_web_enrichment TIMESTAMP,
            ADD COLUMN IF NOT EXISTS manual_notes TEXT
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
                contexto TEXT DEFAULT 'professional'
            )
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

        conn.commit()
        print("Database initialized successfully")
        return True
