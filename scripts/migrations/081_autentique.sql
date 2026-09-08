-- 081 — assinatura de documento deixa de ser canal cego (08/09/26).
--
-- Hoje o INTEL so soube que o Baeta assinou o acordo de nao-circunvencao
-- porque o Renato contou. O documento existe na conta do Autentique
-- (a0b9ec99…, criado 08/09 16h49, 2/2 assinados) e nada no sistema sabia.
--
-- DUAS TABELAS, de proposito:
--   `autentique_documentos` = ESTADO atual (quantos assinaram, terminou?).
--   `autentique_eventos`    = LOG do que chegou, com o payload cru.
-- O log guarda o cru porque webhook e a unica fonte do que aconteceu NAQUELE
-- instante: se o parse estiver errado, o estado da pra reconstruir; sem o cru,
-- o evento se perde e nao volta.
--
-- IDEMPOTENCIA por `evento_chave`: o formato novo traz `event.id`; o antigo
-- (form-urlencoded, sem id) usa um hash do payload. Webhook reentrega em caso
-- de timeout — sem a UNIQUE, uma reentrega viraria segundo aviso ao Renato do
-- mesmo fato.
--
-- ⚠️ `verificado` NAO e enfeite: o formato novo assina com HMAC-SHA256
-- (`X-Autentique-Signature`), o antigo nao assina nada. Guardar se a
-- assinatura foi conferida permite que qualquer automacao futura exija
-- evidencia verificada antes de MEXER em algo — evento nao verificado serve
-- pra avisar, nunca pra fechar task sozinho.

BEGIN;

CREATE TABLE IF NOT EXISTS autentique_documentos (
    id                 TEXT PRIMARY KEY,          -- id do documento no Autentique
    nome               TEXT,
    criado_em_doc      TIMESTAMP,                 -- created_at do Autentique (UTC)
    total_signatarios  INT     NOT NULL DEFAULT 0,
    total_assinados    INT     NOT NULL DEFAULT 0,
    finalizado_em      TIMESTAMP,                 -- quando o ultimo assinou
    signatarios        JSONB,                     -- [{nome,email,acao,assinado_em,visto_em,recusado_em}]
    ultimo_evento_em   TIMESTAMP,
    criado_em          TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'UTC'),
    atualizado_em      TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'UTC')
);

CREATE TABLE IF NOT EXISTS autentique_eventos (
    id             SERIAL PRIMARY KEY,
    evento_chave   TEXT NOT NULL UNIQUE,          -- event.id (novo) ou hash (antigo)
    tipo           TEXT,                          -- signature.accepted, document.finished, ...
    documento_id   TEXT,
    documento_nome TEXT,
    signatario     TEXT,                          -- email de quem agiu, quando da pra saber
    ocorrido_em    TIMESTAMP,
    formato        TEXT NOT NULL,                 -- 'json' | 'form'
    verificado     BOOLEAN NOT NULL DEFAULT FALSE,
    payload        JSONB,
    criado_em      TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'UTC')
);

CREATE INDEX IF NOT EXISTS idx_autentique_eventos_doc
    ON autentique_eventos (documento_id, criado_em DESC);
CREATE INDEX IF NOT EXISTS idx_autentique_eventos_recentes
    ON autentique_eventos (criado_em DESC);

COMMIT;
