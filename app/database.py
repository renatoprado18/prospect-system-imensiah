"""
PostgreSQL Database Module for Vercel Postgres
"""
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager

# Vercel Postgres connection string
DATABASE_URL = os.getenv("POSTGRES_URL", os.getenv("DATABASE_URL", ""))

def get_connection():
    """Get a PostgreSQL connection"""
    if not DATABASE_URL:
        raise Exception("POSTGRES_URL environment variable not set")

    # Vercel uses postgres:// but psycopg2 needs postgresql://
    conn_string = DATABASE_URL.replace("postgres://", "postgresql://")

    return psycopg2.connect(conn_string, cursor_factory=RealDictCursor)

@contextmanager
def get_db():
    """Context manager for database connections"""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

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

        conn.commit()
        print("Database initialized successfully")
        return True
