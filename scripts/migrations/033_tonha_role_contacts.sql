-- 033 — tonha_role_contacts: role -> contact_id mapeamento persistente
--
-- Caso 16/06/26: Renato corrigiu 3x os IDs de Andressa/Joao/Priscila pq
-- estavam hardcoded em codigo. Andressa #313 mergeada em #25737 no mesmo
-- dia quebrou execute endpoint. ARCHITECTURE_REBUILD.md tinha os IDs mas
-- estavam stale (Andressa #313, Joao #2869) — doc nao acompanha dedupes.
--
-- Solucao: tabela que Renato controla. Execute endpoint le daqui, nao
-- hardcoda. Quando contact merge/dedupe, Renato atualiza UMA linha.
--
-- Tonha tb pode ler isso pra delegate inteligente.

CREATE TABLE IF NOT EXISTS tonha_role_contacts (
    role TEXT PRIMARY KEY,
    contact_id INTEGER NOT NULL,
    nota TEXT,
    atualizado_em TIMESTAMPTZ DEFAULT NOW(),
    atualizado_por TEXT
);

INSERT INTO tonha_role_contacts (role, contact_id, nota, atualizado_por) VALUES
    ('andressa',           25737, 'Assistente virtual — operacional, contabil, fornecedores admin (Agilize etc).', 'migration_033'),
    ('joao_piccino',       21313, 'Advogado — contratos, processos, juridico.', 'migration_033'),
    ('priscila_contadora', 4734,  'Contadora — balancetes, demonstracoes, fiscal.', 'migration_033')
ON CONFLICT (role) DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_tonha_role_contacts_contact ON tonha_role_contacts (contact_id);
