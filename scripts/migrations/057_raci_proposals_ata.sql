-- 057: proposta de RACI vinda de ATA (não só de mensagem de grupo)
--
-- PEDIDO (Renato, 29/07/2026): "cada ata nova varre os abertos anteriores em
-- vez de empilhar" — a 3ª peça do RACI. A regra de negócio que ele cravou é a
-- **(a)**: item aberto que a ata nova NÃO menciona **fica aberto**. Nada fecha
-- por silêncio, porque silêncio numa ata não é entrega.
--
-- O que a investigação achou antes de escrever isto: NÃO EXISTE pipeline vivo
-- ata -> RACI. A função que criaria os itens (`saveRaciFromAta`, no repo
-- ConselhoOS) só é chamada por um script de teste; a geração real de ata foi
-- delegada ao worker Railway, que apenas grava o markdown em `reunioes.ata_md`.
-- Os 75 itens que existem hoje nasceram de inserção em lote (SQL/bot). Ou seja,
-- não havia onde "plugar" a reconciliação — ela vem junto com o pipeline.
--
-- `raci_group_proposals` já é a superfície human-gated de revisão (aplica /
-- descarta / reabre, tela + "aplica o RACI-N" pelo WhatsApp). Reusar é melhor
-- que criar uma segunda fila: seria a mesma decisão em dois lugares, e a
-- segunda diverge. Faltavam 3 colunas, todas aditivas e nullable — nenhuma
-- linha existente muda de significado.

-- De onde a proposta veio. 'grupo' = mensagem de WhatsApp (todo o passivo
-- atual, por isso o DEFAULT); 'ata' = reconciliação pós-reunião. Sem isto a
-- tela de revisão não consegue dizer se a evidência é uma frase solta de um
-- conselheiro ou o registro formal da reunião — e o peso de cada uma é
-- diferente na hora de aprovar.
ALTER TABLE raci_group_proposals
    ADD COLUMN IF NOT EXISTS origem TEXT NOT NULL DEFAULT 'grupo';

-- Reunião (uuid do ConselhoOS) que originou a proposta. É também a chave de
-- idempotência: reconciliar a mesma ata duas vezes não pode duplicar a fila
-- de revisão (a ata pode ser regenerada — o endpoint de geração é um POST que
-- o Renato dispara de novo quando não gosta do resultado).
ALTER TABLE raci_group_proposals
    ADD COLUMN IF NOT EXISTS reuniao_id TEXT;

-- Payload do item NOVO, quando action='create_item'. As colunas existentes
-- (`item_id`, `new_status`, `new_prazo`, `notes`) descrevem uma ATUALIZAÇÃO de
-- item que já existe; um item novo precisa de area + os 4 papéis RACI, que não
-- têm coluna aqui. JSONB em vez de 6 colunas novas: elas só fariam sentido
-- para uma das duas naturezas de proposta, e ficariam NULL em ~100% das linhas.
ALTER TABLE raci_group_proposals
    ADD COLUMN IF NOT EXISTS item_payload JSONB;

-- A fila de revisão é sempre consultada por "pendentes"; quando a origem é
-- ata, também por reunião (idempotência + tela da reunião).
CREATE INDEX IF NOT EXISTS idx_raci_proposals_reuniao
    ON raci_group_proposals (reuniao_id) WHERE reuniao_id IS NOT NULL;

-- `item_id` é NOT NULL na prática hoje (toda proposta de grupo aponta pra um
-- item existente), mas a coluna já é nullable no schema — proposta de criação
-- entra com item_id NULL e o payload preenchido. Nada a alterar; anotado aqui
-- porque o código passa a depender disso (`_store_ata_proposal`).
