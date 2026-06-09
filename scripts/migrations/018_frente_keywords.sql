-- 018_frente_keywords.sql
-- Bloco 2 (E1 zumbi + 2.X filtro assunto de interesse).
--
-- Tabela de keywords por frente CoS (1-5). Usada por:
--   - services/cos_keywords.is_frente_keyword(text) -> Optional[int]
--   - services/notification_router._rule_frente_keyword_match(payload):
--       frente 1/2 -> urgent (zumbi falou de assunto critico)
--       frente 3/4/5 -> normal priority, vai pro morning briefing
--
-- Frente mapping (v5):
--   1 = imensIAH (produto principal)
--   2 = ConselhoOS + governanca corporativa
--   3 = Familia (Emma/Daniela/Orestes + filhos + mudancas)
--   4 = Almeida Prado consultoria (Vallen + RACI)
--   5 = Pro-bono / network (Itausa/Despertar/etc)

CREATE TABLE IF NOT EXISTS frente_keywords (
  id        SERIAL PRIMARY KEY,
  frente    INT  NOT NULL CHECK (frente BETWEEN 1 AND 5),
  keyword   TEXT NOT NULL,
  criado_em TIMESTAMP DEFAULT NOW(),
  UNIQUE (frente, keyword)
);

CREATE INDEX IF NOT EXISTS idx_frente_keywords_frente ON frente_keywords(frente);

INSERT INTO frente_keywords (frente, keyword) VALUES
  (1, 'imensIAH'), (1, 'Assespro'), (1, 'NeoGovernanca'), (1, 'IA aplicada'),
  (1, 'founder PME'), (1, 'planejamento estrategico'), (1, 'agente AI'),
  (1, 'governanca nascente'),

  (2, 'ConselhoOS'), (2, 'Wadhwani'), (2, 'conselho consultivo'),
  (2, 'conselho administracao'), (2, 'governanca corporativa'),
  (2, 'RACI'), (2, 'ata'), (2, 'Venture Partner'),
  (2, 'deal flow'), (2, 'board'), (2, 'conselheiro independente'),

  (3, 'Emma'), (3, 'Emanuele Sakamoto'), (3, 'Renato Dansieri'),
  (3, 'Manuela Dansieri'), (3, 'Daniela'), (3, 'Orestes'),
  (3, 'mudanca SP'), (3, 'Japao'), (3, 'escola dos filhos'),

  (4, 'Vallen Clinic'), (4, 'Almeida Prado consultoria'),
  (4, 'RACI Vallen'), (4, 'devolutiva tecnica'), (4, 'Thalita Mendes'),

  (5, 'Rodolfo Villela'), (5, 'Itausa'), (5, 'Associacao Despertar'),
  (5, 'Cecilia Zanotti')
ON CONFLICT (frente, keyword) DO NOTHING;

COMMENT ON TABLE frente_keywords IS
  'Keywords por frente CoS (1-5). Usada pra filtro de notification_router (frente 1/2=urgent, demais=morning digest) e drift detection.';
