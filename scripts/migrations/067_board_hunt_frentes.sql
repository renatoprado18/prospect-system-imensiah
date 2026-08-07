-- 067 — board_hunt_frentes: o funil de originação de conselhos deixa de ser HTML à mão
--
-- POR QUE (pedido da CoS, 07/08/26, task #999763). O `~/cockpit/board_hunt.html`
-- v0 foi escrito à mão e tem o mesmo defeito estrutural que o cockpit de frentes
-- tinha antes de virar gerador: ele retrata o INSTANTE em que alguém o escreveu.
-- O card diz "3 dias sem troca" e continua dizendo isso na semana seguinte. Numa
-- tela cuja informação PRINCIPAL é a passagem do tempo, isso não é desatualização
-- — é mentira com cara de medida.
--
-- E há consumidor esperando: a retro da Máquina de Originação (#50) roda toda
-- sexta 9h30 em cima deste funil.
--
-- O QUE É HUMANO E O QUE DERIVA — a divisão é a razão desta tabela existir:
--
--   HUMANO (mora aqui, sobrevive à regeneração):
--     · fase        — em que ponto do funil a frente está. Ninguém deriva isso.
--     · contato_id  — o canal VIVO da frente. É de quem a temperatura deriva.
--     · originador  — quem abriu a porta. Alimenta o mapa de conectores.
--     · piso_alvo   — remuneração-alvo, quando já houver número.
--     · nota        — o julgamento curto do card.
--
--   DERIVADO (o gerador recalcula a cada 5 min, nunca se grava aqui):
--     · dias sem troca, temperatura, de quem é a bola, próximo passo agendado.
--
-- Gravar o derivado seria refazer o defeito do v0 numa tabela em vez de num HTML.
--
-- POR QUE `contato_id` EXISTE, se o pedido falava em `project_id`. Porque metade
-- das frentes NÃO tem projeto — Gui/FESA, Milpel, Ariolino e Eduardo Marson são
-- conversa viva sem projeto aberto — e `project_members` está povoado em apenas
-- 2 dos 6 projetos envolvidos. Sem um contato âncora declarado não há de onde
-- derivar dia nenhum, e a coluna inteira de temperatura nasceria vazia. O projeto
-- continua ligado quando existe; o âncora é que sustenta a régua.
--
-- ÂNCORA É O CANAL, NÃO A CONTRAPARTE. Em Premix o Renato fala com o Monforte,
-- não com o Guidolin; em Milpel, com o Marson. Enquanto a ponte não apresenta, o
-- toque que importa é o da ponte. Duas frentes podem dividir o mesmo âncora
-- (Milpel e Eduardo Marson dividem o #18707) — e devem: é o mesmo canal.

CREATE TABLE IF NOT EXISTS board_hunt_frentes (
    id                    SERIAL PRIMARY KEY,
    nome                  TEXT NOT NULL,
    subtitulo             TEXT,

    -- ligações (todas opcionais: frente viva sem projeto é o caso comum)
    project_id            INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    contato_id            INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
    originador_contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
    -- quando a porta não foi aberta por ninguém (relação direta, inbound frio):
    originador_rotulo     TEXT,

    -- 1 Prospecção/Rede · 2 Aproximação · 3 Reunião/Descoberta
    -- 4 Avaliação/Proposta · 5 Negociação · 6 Assento fechado
    fase                  SMALLINT NOT NULL CHECK (fase BETWEEN 1 AND 6),

    -- `hold` é ORTOGONAL à fase, não uma sétima fase: a frente parada guarda o
    -- ponto em que parou, e retomá-la é mudar o status, não redescobrir a fase.
    -- `prova` = referência que mostra que a rede paga (Vallen), fora do funil.
    status                TEXT NOT NULL DEFAULT 'ativo'
                          CHECK (status IN ('ativo','hold','prova','fechado','perdido')),

    piso_alvo             NUMERIC(15,2),
    nota                  TEXT,

    criado_em             TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    atualizado_em         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- O nome é a identidade da frente na tela: garante que rodar o seed duas vezes
-- não duplique o card.
CREATE UNIQUE INDEX IF NOT EXISTS board_hunt_frentes_nome_uk ON board_hunt_frentes (nome);
CREATE INDEX IF NOT EXISTS board_hunt_frentes_contato_ix ON board_hunt_frentes (contato_id)
    WHERE contato_id IS NOT NULL;

COMMENT ON TABLE board_hunt_frentes IS
'Funil do Board Hunt 2026 (meta: 2 assentos remunerados ate dez/26). Guarda so o
que e JULGAMENTO — fase, canal ancora, originador, piso, nota. Dias sem troca,
temperatura, bola e proximo passo sao DERIVADOS pelo scripts/board_hunt.py a cada
5 min e nunca gravados aqui: era exatamente isso que o v0 escrito a mao congelava.';

COMMENT ON COLUMN board_hunt_frentes.contato_id IS
'Canal VIVO da frente — de quem o gerador deriva dias/bola/proximo passo. E a
ponte enquanto ela nao apresenta (Monforte em Premix, Marson em Milpel), nao
necessariamente a contraparte final. NULL = frente sem canal declarado: a tela
mostra "sem ancora" em vez de fingir uma temperatura.';

-- ---------------------------------------------------------------- seed --
-- As 10 frentes do v0 (07/08), com os âncoras conferidos contra `messages`: os
-- "dias" que a CoS escreveu à mão batem com o último toque de cada âncora
-- (Gui/FESA 4d, Alfredo 3d, Monforte 3d, DAMAPEL 1d, G100 2d, Marson 0d) — a
-- régua que o gerador aplica reproduz o número que ela apurou.

INSERT INTO board_hunt_frentes
    (nome, subtitulo, project_id, contato_id, originador_contact_id, originador_rotulo, fase, status, nota)
VALUES
    ('G100 Brasil', 'rede ~195 líderes', 35, 26393, 26393, NULL, 1, 'ativo',
     'Canal, não assento. Caçar dentro.'),

    ('Premix / Guidolin', '#54 · nutrição animal', 54, 3043, 3043, NULL, 2, 'ativo',
     'Reativando. WA 04/08; FUP #999728 vence 11/08 (task, não reunião).'),

    ('Ariolino / FBN Brasil', '#376 · conector rede familiar', NULL, 376, NULL, 'Dani', 2, 'ativo',
     'Quente no tom, esfriou por drift. Café terça 11 ou quinta 13 (#999762).'),

    ('Gui / FESA', 'Carlos G. Nosé', NULL, 26692, 674, NULL, 3, 'ativo',
     'Café marcado p/ segunda.'),

    ('Alfredo / Hindiana', 'Alfredo Villela', 32, 157, NULL, 'relação direta', 3, 'ativo',
     'Âncora de advisory fixo. Cutucada-almoço enviada 04/08.'),

    ('Milpel', '~8.000 func · fundador sem sucessão', NULL, 18707, 18707, NULL, 2, 'ativo',
     'Alvo de M&A + assento (governança/sucessão). Board hunt E originação no mesmo alvo. Portão = NDA (Edu já pediu).'),

    ('Eduardo Marson', 'Westwing · conector Milpel', NULL, 18707, NULL, 'relação direta', 3, 'ativo',
     'CEO/Chairman Westwing #18707. Amigo. Trouxe a Milpel — é o conector do deal.'),

    ('DAMAPEL', 'família De Domenico', 56, 26695, 5486, NULL, 4, 'ativo',
     'Escopo + 6 perguntas + 3 escritórios enviados 06/08. Bola com o Marcelo.'),

    ('Amiralian / Phisalia', '#1515 → virou advisory', 51, 1515, 16282, NULL, 4, 'ativo',
     'Call 07/08 pivotou p/ Advisory. Proposta enviada, aguarda retorno.'),

    -- Sem âncora de propósito: o canal era a Alba, e os dois contatos "Grupo
    -- Natureza" no CRM são duplicata um do outro. Escolher no chute daria à
    -- frente parada uma temperatura inventada.
    ('Grupo Natureza', '#55 · O Boticário', 55, NULL, 5332, NULL, 1, 'hold',
     'On-hold — retomar 2027 (lembrete #999727).'),

    ('Vallen', 'cliente desde 2024 · via Alba', 24, NULL, 5332, NULL, 6, 'prova',
     'PROVA de que a rede paga — engajamento de conselho ativo. Referência, não frente.')
ON CONFLICT (nome) DO NOTHING;
