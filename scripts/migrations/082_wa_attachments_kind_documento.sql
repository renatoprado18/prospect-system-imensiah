-- 082 — `kind='documento'` (08/09/26).
--
-- O CHECK antigo permitia só ('pdf','audio','image'). Quando o worker passou a
-- extrair xlsx/docx/pptx, o texto SAÍA certo (107.998 caracteres do relatório
-- financeiro, 25.979 do documento de processos) e o INSERT morria na
-- constraint — dentro de um `try/except` que só faz `logger.warning`. O
-- endpoint respondia `{"ok": true, "chars": 107998}` e o banco continuava
-- vazio: falha silenciosa dentro do conserto de uma falha silenciosa.
--
-- Recriar o CHECK em vez de removê-lo: ele é o que impede kind inventado por
-- typo ('imagem', 'áudio') de virar linha que nenhum leitor encontra. O
-- conserto é ampliar a lista, não abrir mão da guarda.

BEGIN;

ALTER TABLE wa_attachments DROP CONSTRAINT IF EXISTS wa_attachments_kind_check;

ALTER TABLE wa_attachments
    ADD CONSTRAINT wa_attachments_kind_check
    CHECK (kind = ANY (ARRAY['pdf'::text, 'audio'::text, 'image'::text, 'documento'::text]));

COMMIT;
