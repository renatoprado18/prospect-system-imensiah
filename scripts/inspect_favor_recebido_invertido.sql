-- ============================================================================
-- Inspeção: Rodas favor_recebido potencialmente invertidas
-- ============================================================================
-- Lista rodas do tipo `favor_recebido` cuja mensagem fonte parece ser um
-- AGRADECIMENTO do contato a Renato — o que indica o oposto: Renato foi
-- quem fez o favor, não recebeu.
--
-- Critérios de suspeita (qualquer um dispara):
--   1. Mensagem fonte é INCOMING e contém padrão de agradecimento
--      ("obrigad[ao]", "valeu", "grat[ao]", "thanks", "obg", "gracias")
--      sem subject explícito mencionando o contato como agente.
--
--   2. Conteúdo da roda menciona "indicação/apresentação/ajuda" sem deixar
--      claro o sujeito (sem "me", "Renato", "para mim").
--
-- USO:
--   Apenas LEITURA. Nenhuma alteração é feita.
--   Após revisão manual, marque as rodas erradas como `expirado`:
--
--     UPDATE contact_rodas
--     SET status = 'expirado', atualizado_em = NOW()
--     WHERE id IN (<lista de IDs revisada>);
--
-- ============================================================================

WITH suspeitos AS (
    SELECT
        r.id              AS roda_id,
        r.contact_id,
        c.nome            AS contato,
        r.tipo,
        r.conteudo        AS roda_conteudo,
        r.status,
        r.criado_em,
        r.ai_confidence,
        m.id              AS message_id,
        m.direcao         AS msg_direcao,
        LEFT(m.conteudo, 300) AS msg_conteudo,
        m.enviado_em,

        -- Heurística 1: agradecimento incoming
        (m.direcao = 'incoming'
         AND m.conteudo ~* '\m(obrigad[ao]|valeu|grat[ao]|thanks?|obg|gracias)\M'
        ) AS h_msg_agradecimento_incoming,

        -- Heurística 2: conteúdo da roda sem subject explícito
        (r.conteudo ~* '(indica|apresenta|ajuda|conex)'
         AND r.conteudo !~* '\m(me|mim|renato)\M'
        ) AS h_roda_sem_subject

    FROM contact_rodas r
    JOIN contacts c ON c.id = r.contact_id
    LEFT JOIN messages m ON m.id = r.message_id
    WHERE r.tipo = 'favor_recebido'
      AND r.status = 'pendente'
)
SELECT
    roda_id,
    contact_id,
    contato,
    roda_conteudo,
    msg_direcao,
    msg_conteudo,
    enviado_em,
    ai_confidence,
    CASE
        WHEN h_msg_agradecimento_incoming AND h_roda_sem_subject THEN 'ALTA - agradecimento incoming + roda sem subject'
        WHEN h_msg_agradecimento_incoming                        THEN 'MEDIA - agradecimento incoming'
        WHEN h_roda_sem_subject                                  THEN 'BAIXA - roda sem subject'
        ELSE 'revisar manualmente'
    END AS suspeita,
    -- mensagem fonte ausente também é suspeita
    (message_id IS NULL) AS sem_mensagem_fonte
FROM suspeitos
WHERE h_msg_agradecimento_incoming
   OR h_roda_sem_subject
   OR message_id IS NULL
ORDER BY
    h_msg_agradecimento_incoming DESC,
    h_roda_sem_subject DESC,
    criado_em DESC;

-- ============================================================================
-- Resumo agregado: quantos suspeitos por categoria
-- ============================================================================
-- Rode separadamente se quiser só os números:
--
-- SELECT
--     COUNT(*) FILTER (WHERE r.tipo = 'favor_recebido' AND r.status = 'pendente') AS total_pendentes,
--     COUNT(*) FILTER (
--         WHERE r.tipo = 'favor_recebido'
--           AND r.status = 'pendente'
--           AND m.direcao = 'incoming'
--           AND m.conteudo ~* '\m(obrigad[ao]|valeu|grat[ao]|thanks?|obg|gracias)\M'
--     ) AS suspeita_alta,
--     COUNT(*) FILTER (
--         WHERE r.tipo = 'favor_recebido'
--           AND r.status = 'pendente'
--           AND r.message_id IS NULL
--     ) AS sem_mensagem_fonte
-- FROM contact_rodas r
-- LEFT JOIN messages m ON m.id = r.message_id;
