-- 015_publish_failures_counter.sql
-- Contador de falhas consecutivas no auto_publisher.
-- Apos 3 falhas com body vazio, post vira status='dismissed' com
-- dismissed_motivo='auto: body vazio (N falhas)' — evita loop infinito
-- no cron que enche /admin/cron-health de erros.

ALTER TABLE editorial_posts
    ADD COLUMN IF NOT EXISTS publish_failures INT DEFAULT 0;

ALTER TABLE hot_takes
    ADD COLUMN IF NOT EXISTS publish_failures INT DEFAULT 0;

COMMENT ON COLUMN editorial_posts.publish_failures IS
    'Falhas consecutivas no auto_publisher. >=3 -> dismissed com motivo auto. Reseta em scheduled novo OR publish success.';
COMMENT ON COLUMN hot_takes.publish_failures IS
    'Idem editorial_posts.publish_failures';
