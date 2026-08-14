-- 075 — o nome do grupo passa a vir também do cache, não só do vínculo com projeto
--
-- O DEFEITO, medido em 14/08/26 no prod. `copilot.group_messages` buscava
-- `group_name` só em `project_whatsapp_groups` — que é a tabela dos grupos
-- VINCULADOS A PROJETO, e tem 12 linhas. Como há 53 grupos com mensagem,
-- 43.247 das 52.898 mensagens (82%) saíam da view com `group_name = NULL`.
--
-- POR QUE ISSO QUEBRAVA A BUSCA INTEIRA, e em silêncio. O MCP
-- `search_group_messages` filtra `group_name ILIKE %termo%` (mcp/db.py:379).
-- Em SQL, `NULL ILIKE '<qualquer coisa>'` não é FALSE, é NULL — a linha é
-- descartada pelo WHERE sem erro, sem aviso, sem zero explicado. Buscar o grupo
-- da ASSESPRO devolvia lista vazia, que se lê como "não falaram disso" quando o
-- real era "esse grupo não tem nome nesta view". É a família de
-- [[feedback_filtro_vocabulario_errado_falha_calado]]: o filtro não erra alto,
-- erra calado, e quem consome não distingue ausência de invisibilidade.
--
-- O CONSERTO. `social_groups_cache` já tem o nome de 53 dos 53 grupos (117
-- linhas, sem `group_jid` duplicado — conferido). Ele vira o fallback via
-- COALESCE. Medido antes/depois: 'assespro' 0 → 512 mensagens · 'conselho'
-- 818 → 820 · 'vallen' 264 → 264 (esse já funcionava, é grupo com projeto).
-- Nenhum valor existente muda: o vínculo com projeto continua ganhando quando
-- existe, e é ele que carrega o nome curado.
--
-- O `project_id` continua vindo SÓ do vínculo, e isso é de propósito: grupo de
-- convívio não tem projeto, e inventar um seria pior que o NULL honesto.
--
-- DE QUEBRA, o ORDER BY no LATERAL. 'Governança APCE'
-- (120363408627197480@g.us) tem DUAS linhas ativas em project_whatsapp_groups,
-- apontando pros projetos 34 e 33. Com `LIMIT 1` sem ordenação o Postgres pode
-- devolver qualquer uma das duas entre execuções, então a mesma mensagem
-- oscilava de projeto. O ORDER BY não decide qual está certa — decidir isso é
-- do Renato — mas troca "oscila" por "sempre a mesma", que é a diferença entre
-- um dado errado e um dado irreproduzível.

CREATE OR REPLACE VIEW copilot.group_messages AS
SELECT gm.id,
       gm.group_jid,
       COALESCE(pwg.group_name, sgc.group_name) AS group_name,
       pwg.project_id,
       gm.sender_name,
       gm.contact_id,
       gm.content,
       gm.message_type,
       gm.from_me,
       gm."timestamp"
  FROM group_messages gm
  LEFT JOIN LATERAL (
       SELECT p.group_name, p.project_id
         FROM project_whatsapp_groups p
        WHERE p.group_jid = gm.group_jid
          AND p.ativo
        ORDER BY p.id
        LIMIT 1
  ) pwg ON true
  LEFT JOIN LATERAL (
       SELECT s.group_name
         FROM social_groups_cache s
        WHERE s.group_jid = gm.group_jid
        LIMIT 1
  ) sgc ON true;
