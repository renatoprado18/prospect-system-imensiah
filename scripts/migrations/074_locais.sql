-- 074 — LUGAR vira entidade: endereço deixa de morar pendurado numa pessoa
--
-- POR QUE (fila #58, `#999837`, desenho do Renato). Hoje um lugar não existe no
-- modelo: ele aparece como campo de outra coisa. Medido em 13/08/26 no INTEL de
-- produção — 812 de 8.899 contatos têm `enderecos` (9,1%) e 100 de 410 eventos
-- têm `location` (24,4%), mais `oficinas.endereco`/`oficinas.cidade` como
-- terceira ilha. Nenhuma dessas formas responde "quem se relaciona com este
-- lugar, e como".
--
-- O CASO QUE ORIGINOU A FRENTE, com os números na mão: a Fazenda Jaboticabeiras
-- não tem ficha. O endereço dela (CEP 37800-000, Guaxupé) está pendurado no
-- contato #20594 — a esposa do gerente. E o nome dela virou nome de PESSOA em
-- duas fichas, a #5363 e a #5365 ("Rosimari/Rosimeri Fazenda Jaboticabeiras"),
-- porque não havia onde mais colocá-lo. Quem procura a fazenda não a encontra
-- em lugar nenhum; quem apaga a ficha da Fernanda perde o endereço junto.
--
-- POR QUE DUAS TABELAS, e não uma coluna `local` em contacts. A relação
-- pessoa↔lugar tem PAPEL, e o papel é a informação que decide: proprietário,
-- condômino, sócio, gerente. Sem ele, "Renato e a Fazenda Pitangueiras" não
-- distingue dono de visitante — e essa distinção já produziu erro em peça
-- escrita: um memo afirmou por sete semanas que ele era proprietário da
-- Jaboticabeiras, que é do pai; a dele é a Pitangueiras, em condomínio com os
-- irmãos. Papel numa coluna só serviria a UM vínculo por lugar, e um lugar tem
-- vários.
--
-- ⚠️ ISTO MUDA O CONTRATO: 162 → 164 tabelas. O baseline em
-- project_modelo_rede_entidades.md tem que ser atualizado no mesmo movimento,
-- senão o `verifica_modelo.py` passa a acusar divergência todo dia e a sessão
-- aprende a ignorar o verificador — que é pior do que não ter verificador.
--
-- POR QUE `chave_norm` COM UNIQUE. O backfill vem de texto livre digitado por
-- humanos, e o mesmo endereço aparece escrito de formas diferentes: "R. Dr.
-- Amâncio de Carvalho, 182 - Cj 202/203" (6 eventos) e "Alba e Bernhoeft
-- Recursos Humanos (Rua Dr. Amâncio de Carvalho 182, cj...)" (4 eventos) são o
-- MESMO lugar. A chave normalizada impede a duplicata exata; a duplicata por
-- redação diferente ela NÃO resolve, e não deve fingir que resolve — fundir
-- essas duas automaticamente seria decidir no ambíguo. O backfill sinaliza os
-- candidatos e a fusão fica para decisão humana.
--
-- O QUE NÃO ENTRA, de propósito: os 812 endereços residenciais/comerciais de
-- `contacts.enderecos`. Endereço de pessoa é atributo da pessoa e continua
-- onde está; copiá-lo para cá criaria duas verdades sobre o mesmo dado, que é
-- o defeito que esta auditoria vem catalogando. `locais` é para lugar que tem
-- relação com MAIS DE UMA pessoa ou existência própria — fazenda, sede, clube.
--
-- ⚠️ O MERGE DE CONTATOS PRECISA CONHECER `locais_contatos`. A lista em
-- services/contact_dedup.py (`_FK_TABLES_TO_CONTACTS`) é FIXA E MANUAL, e o
-- merge deleta a ficha secundária: tabela nova que não estiver lá tem o vínculo
-- apagado em CASCADE sem aviso — foi assim que o merge do Eduardo Marson comeu
-- ~17 mensagens em 16/06. Esta migration vem acompanhada da entrada na lista e
-- em `_COMPOSITE_UNIQUE_PARTNERS` (a UNIQUE abaixo envolve contact_id).

CREATE TABLE IF NOT EXISTS locais (
    id              SERIAL PRIMARY KEY,
    nome            TEXT NOT NULL,
    tipo            TEXT,
    logradouro      TEXT,
    cidade          TEXT,
    estado          TEXT,
    cep             TEXT,
    pais            TEXT DEFAULT 'Brasil',
    -- O texto original de onde o lugar veio. Guardar o bruto é o que permite
    -- refazer a normalização depois sem voltar à fonte — e conferir o que o
    -- backfill entendeu.
    endereco_bruto  TEXT,
    -- Normalizada (minúscula, espaços colapsados) só para impedir a duplicata
    -- EXATA. Não é identidade semântica: ver o comentário acima.
    chave_norm      TEXT UNIQUE,
    nota            TEXT,
    origem          TEXT,
    criado_em       TIMESTAMP DEFAULT now(),
    atualizado_em   TIMESTAMP DEFAULT now()
);

COMMENT ON TABLE locais IS
    'Lugar como entidade própria (074). Fazenda, sede, clube — o que tem relação com pessoas e existência independente delas. Endereço residencial de contato continua em contacts.enderecos.';
COMMENT ON COLUMN locais.chave_norm IS
    'Dedup EXATA apenas. Duas redações do mesmo endereço passam como lugares distintos, de propósito — fundir automaticamente seria decidir no ambíguo.';
COMMENT ON COLUMN locais.endereco_bruto IS
    'Texto original (ex.: calendar_events.location). Permite refazer a normalização e auditar o que o backfill entendeu.';

CREATE TABLE IF NOT EXISTS locais_contatos (
    id          SERIAL PRIMARY KEY,
    local_id    INTEGER NOT NULL REFERENCES locais(id)   ON DELETE CASCADE,
    contact_id  INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    -- proprietario | condomino | socio | gerente | funcionario | morador |
    -- visitante — lista aberta: papel é vocabulário do mundo dele, e fechá-lo
    -- agora obrigaria migration a cada palavra nova.
    papel       TEXT NOT NULL,
    nota        TEXT,
    criado_em   TIMESTAMP DEFAULT now(),
    -- A mesma pessoa pode ter DOIS papéis no mesmo lugar (sócio e gerente), e
    -- isso não é duplicata. Por isso o papel entra na chave.
    UNIQUE (local_id, contact_id, papel)
);

COMMENT ON TABLE locais_contatos IS
    'Quem se relaciona com o lugar, e COMO (074). O papel é a informação que decide: sem ele "Renato e a fazenda" não distingue dono de visitante.';

CREATE INDEX IF NOT EXISTS idx_locais_contatos_local   ON locais_contatos(local_id);
CREATE INDEX IF NOT EXISTS idx_locais_contatos_contact ON locais_contatos(contact_id);
CREATE INDEX IF NOT EXISTS idx_locais_cidade           ON locais(cidade);

-- O evento passa a poder apontar para o lugar canônico. SET NULL e não CASCADE:
-- apagar um lugar não pode apagar a reunião que aconteceu nele. `location`
-- continua existindo e continua sendo a fonte — esta coluna é enriquecimento,
-- não substituição, e enquanto houver evento com location e sem local_id isso
-- fica visível em vez de virar perda silenciosa.
ALTER TABLE calendar_events
    ADD COLUMN IF NOT EXISTS local_id INTEGER REFERENCES locais(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_calendar_events_local ON calendar_events(local_id);

COMMENT ON COLUMN calendar_events.local_id IS
    'Lugar canônico do evento (074). Enriquecimento de location, não substituto: location segue sendo a fonte crua vinda do Google.';
