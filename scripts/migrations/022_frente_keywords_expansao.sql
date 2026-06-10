-- 022_frente_keywords_expansao.sql
-- Calibracao email triage 10/06/26.
--
-- Expansao de keywords por frente baseada em 40 rejecoes user em shadow:
--   - IBGC, Chapter Zero, Poli Angels => frente 2 governanca
--   - Holding patrimonial / planejamento => frente 4 consultoria
--   - Colegio Santa Cruz => frente 3 familia
--   - Claude/Anthropic => frente 1 (lancamentos AI Renato usa)

-- Frente 1 (imensIAH) - tecnologia/AI
INSERT INTO frente_keywords (frente, keyword) VALUES
  (1, 'Claude'),                -- Anthropic Claude lancamentos
  (1, 'Anthropic')
ON CONFLICT (frente, keyword) DO NOTHING;

-- Frente 2 (ConselhoOS + governanca) - eventos board
INSERT INTO frente_keywords (frente, keyword) VALUES
  (2, 'IBGC'),                  -- Instituto Brasileiro Governanca Corporativa
  (2, 'ICGC'),                  -- variacao
  (2, 'Chapter Zero'),
  (2, 'Chapter Zero Alliance'),
  (2, 'Poli Angels'),           -- grupo investidores - decisao deal flow
  (2, 'Round 47'),              -- naming convention Poli Angels rounds
  (2, 'Lifelong Learner IBGC')
ON CONFLICT (frente, keyword) DO NOTHING;

-- Frente 3 (familia/vida)
INSERT INTO frente_keywords (frente, keyword) VALUES
  (3, 'Colegio Santa Cruz')     -- escola dos filhos
ON CONFLICT (frente, keyword) DO NOTHING;
-- Nota: 'escola dos filhos' ja existe em migration 018.

-- Frente 4 (Almeida Prado consultoria fiscal/patrimonial)
INSERT INTO frente_keywords (frente, keyword) VALUES
  (4, 'holding patrimonial'),
  (4, 'planejamento patrimonial'),
  (4, 'planejamento tributario'),
  (4, 'Cleverson Marinho')      -- advogado patrimonial
ON CONFLICT (frente, keyword) DO NOTHING;

-- Frente 5 (capital relacional Despertar)
-- 'Itausa' ja existe em migration 018. Aqui so confirma idempotencia.
INSERT INTO frente_keywords (frente, keyword) VALUES
  (5, 'Despertar')
ON CONFLICT (frente, keyword) DO NOTHING;
