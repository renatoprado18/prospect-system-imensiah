-- Adiciona regras estruturadas pras hipoteses H#2 (16h BRT) e H#3 (Ter/Qui/Sex).
-- Sem isso, o modal "Agendar Hot Take" hardcodava Seg-Sex 9h/12h/17h e furava
-- as hipoteses ativas. Convencao weekday() Python: Seg=0, Ter=1, Qua=2, Qui=3,
-- Sex=4, Sab=5, Dom=6 (mesma de get_active_schedule_constraints).
--
-- Idempotente: usa COALESCE pra nao sobrescrever regras existentes.

UPDATE editorial_hypotheses
SET regras = COALESCE(
    regras,
    '[{"action": "restrict_hours", "values": [16, 21], "target_field": "data_publicacao"}]'::jsonb
)
WHERE id = 2 AND status = 'ativa';

UPDATE editorial_hypotheses
SET regras = COALESCE(
    regras,
    '[{"action": "restrict_weekdays", "values": [1, 3, 4], "target_field": "data_publicacao"}]'::jsonb
)
WHERE id = 3 AND status = 'ativa';
