-- 021_frente_keywords_vallen_curtas.sql
-- Calibracao email triage 10/06/26.
--
-- Bugs detectados no shadow rejected:
--   - "Solicitacao de compartilhamento: Vallen - Pauta Conselho"
--   - "Vallen - Dossie", "Vallen - Devolutiva"
--   - "RACI" standalone (sem "Vallen")
-- Nao batiam frente 4 pq keyword era "Vallen Clinic" (so match exato).
--
-- Adiciona substrings curtas que cobrem casos legitimos da operacao
-- Almeida Prado consultoria (frente 4).

INSERT INTO frente_keywords (frente, keyword) VALUES
  (4, 'Vallen'),                -- substring suficiente (cobre Pauta, Dossie, Devolutiva)
  (4, 'RACI'),                  -- standalone (ja existe RACI Vallen mas tbm precisa solo)
  (4, 'Dossie Conselho'),
  (4, 'Pauta Conselho'),
  (4, 'Devolutiva')
ON CONFLICT (frente, keyword) DO NOTHING;
