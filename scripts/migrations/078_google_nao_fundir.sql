-- 078 — "essa ficha do Google fica" também vira registro
--
-- A 077 resolveu metade do problema: pares de contatos do INTEL que o Renato já
-- decidiu que não são a mesma pessoa. Faltava a outra metade, e ela apareceu no
-- mesmo dia: ao limpar as fichas repetidas do Google (22/08), ele mandou MANTER
-- três pares — #359, #21541 e #4281. Eles continuaram aparecendo como "órfã" na
-- rodada seguinte, porque a decisão era sobre uma ficha do GOOGLE e o registro
-- só sabia falar de contato do INTEL.
--
-- É o mesmo defeito de sempre, no mesmo dia, em outra roupa: decisão que não tem
-- onde ser escrita volta a ser perguntada, e a ferramenta vira tarefa do Renato
-- ([[feedback_ferramenta_nao_vira_tarefa_do_renato]]).
--
-- POR QUE TABELA SEPARADA E NÃO UMA COLUNA NA 077. A chave aqui é
-- (contato do INTEL, resourceName do Google) — um par de universos diferentes,
-- não dois ids da mesma tabela. Enfiar os dois casos numa tabela só exigiria
-- colunas nulas alternadas e um CHECK dizendo "ou uma coisa ou outra", que é
-- como se convida o próximo leitor a escrever no par errado.
--
-- SEM FK pro Google, obviamente: o `google_rid` é id de sistema externo. Se a
-- ficha for apagada de lá por outro caminho, a linha vira inócua — e continua
-- correta enquanto durar, porque resourceName do Google não é reciclado.

CREATE TABLE IF NOT EXISTS google_nao_fundir (
    id          SERIAL PRIMARY KEY,
    contact_id  INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    google_rid  TEXT    NOT NULL,
    motivo      TEXT    NOT NULL,
    decidido_por TEXT   NOT NULL DEFAULT 'renato',
    criado_em   TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT google_nao_fundir_par UNIQUE (contact_id, google_rid)
);

CREATE INDEX IF NOT EXISTS idx_google_nao_fundir_contact ON google_nao_fundir (contact_id);

COMMENT ON TABLE google_nao_fundir IS
    'Fichas do Google que o Renato decidiu MANTER separadas do contato INTEL '
    'correspondente. Lido por verifica_google.py e orfas.py, pra decisao tomada '
    'nao voltar a ser perguntada a cada rodada.';
