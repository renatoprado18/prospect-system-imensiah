-- Seed retroativo de platform_costs (Fase 1 do Cost Tracker).
-- Idempotente via ON CONFLICT (provider, period_start).
--
-- Cobertura: nov/2025 a abr/2026 (6 meses fechados) + mai/2026 parcial.
-- Valores Railway sao precisos (memory project_cost_tracker.md). Demais
-- precisam ser refinados manualmente via dashboards (POST endpoint).

-- ===== Railway (numeros precisos do incidente LibreChat) =====
INSERT INTO platform_costs (provider, period_start, period_end, amount_usd, notes) VALUES
    ('railway', '2026-03-01', '2026-03-31', 5.00,  'Hobby base, antes do crescimento silencioso'),
    ('railway', '2026-04-01', '2026-04-30', 13.17, 'Crescimento silencioso (LibreChat zumbi acumulando storage)'),
    ('railway', '2026-05-01', '2026-05-31', 17.66, 'Pico antes do cleanup em 07/05; pos-cleanup deve cair pra ~$5-10')
ON CONFLICT (provider, period_start) DO NOTHING;

-- ===== Free tiers (zeros confirmados) =====
INSERT INTO platform_costs (provider, period_start, period_end, amount_usd, notes) VALUES
    ('vercel', '2025-11-01', '2025-11-30', 0.00, 'Hobby plan free tier'),
    ('vercel', '2025-12-01', '2025-12-31', 0.00, 'Hobby plan free tier'),
    ('vercel', '2026-01-01', '2026-01-31', 0.00, 'Hobby plan free tier'),
    ('vercel', '2026-02-01', '2026-02-28', 0.00, 'Hobby plan free tier'),
    ('vercel', '2026-03-01', '2026-03-31', 0.00, 'Hobby plan free tier'),
    ('vercel', '2026-04-01', '2026-04-30', 0.00, 'Hobby plan free tier'),

    ('google', '2025-11-01', '2025-11-30', 0.00, 'Free tier permanente (Calendar/Gmail/Contacts/Drive/Tasks)'),
    ('google', '2025-12-01', '2025-12-31', 0.00, 'Free tier permanente'),
    ('google', '2026-01-01', '2026-01-31', 0.00, 'Free tier permanente'),
    ('google', '2026-02-01', '2026-02-28', 0.00, 'Free tier permanente'),
    ('google', '2026-03-01', '2026-03-31', 0.00, 'Free tier permanente'),
    ('google', '2026-04-01', '2026-04-30', 0.00, 'Free tier permanente'),

    ('github', '2025-11-01', '2025-11-30', 0.00, 'Free private repos (~6h/2000min Actions)'),
    ('github', '2025-12-01', '2025-12-31', 0.00, 'Free private repos'),
    ('github', '2026-01-01', '2026-01-31', 0.00, 'Free private repos'),
    ('github', '2026-02-01', '2026-02-28', 0.00, 'Free private repos'),
    ('github', '2026-03-01', '2026-03-31', 0.00, 'Free private repos'),
    ('github', '2026-04-01', '2026-04-30', 0.00, 'Free private repos')
ON CONFLICT (provider, period_start) DO NOTHING;

-- TODO manual via POST /api/admin/platform-costs:
--   - neon (storage + compute hours, 6 meses)
--   - anthropic (Claude API tokens, 6 meses)
--   - linkdapi (creditos consumidos x $1=120, 6 meses)
--   - railway nov/dez/jan/fev (anteriores ao incidente)
