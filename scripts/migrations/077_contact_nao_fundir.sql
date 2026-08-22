-- 077 — o "não, esses dois não são a mesma pessoa" vira registro, não lembrança
--
-- DECISÃO DO RENATO, 22/08/2026, quando o verificador propôs fundir #1360 e
-- #4376 (602 mensagens): *"Douglas Bassi e Orestes já passamos por isso: os dois
-- trabalhavam na mesma empresa e compartilhavam um número de telefone fixo."*
--
-- O PROBLEMA NÃO É O PAR, É O "JÁ PASSAMOS POR ISSO". A decisão foi tomada,
-- valeu, e voltou a ser perguntada — porque não havia onde escrevê-la. O mesmo
-- caso está no `session_locks_historico` de 14/08 com outro par: *"#2 Amâncio
-- NÃO se funde com #3 Alba (duas empresas no mesmo prédio)"*. Prosa em memo não
-- é lida por script nenhum: toda rodada de `verifica_google.py` ou de
-- `duplicados.py` reapresenta o par, e cada reapresentação gasta a atenção dele
-- de novo. Ferramenta que repropõe o que já foi decidido vira tarefa do Renato
-- ([[feedback_ferramenta_nao_vira_tarefa_do_renato]]).
--
-- POR QUE TABELA E NÃO ARQUIVO VERSIONADO. O precedente natural seria o
-- `scripts/fatos_canonicos.json`, que guarda "a verdade uma vez" e até tem campo
-- `errado` pros padrões que não podem voltar. Mas aqui a chave é um PAR DE IDS
-- de contato, e id de banco muda entre local e prod — gravá-los num arquivo do
-- repo é exatamente o que [[feedback_no_hardcoded_contact_ids]] proíbe, e o
-- arquivo passaria a mentir no outro alvo. Id de contato mora no banco.
--
-- POR QUE O CHECK DE ORDEM. Sem normalizar, (1360, 4376) e (4376, 1360) são
-- linhas diferentes e o UNIQUE não impede a duplicata — o par ficaria registrado
-- duas vezes e bastaria consultar pela ordem "errada" pra não achar nada. O
-- CHECK obriga a forma canônica; quem insere ordena antes.
--
-- ON DELETE CASCADE de propósito: se uma das fichas for absorvida num merge
-- LEGÍTIMO com um terceiro, o par deixa de existir e a decisão sobre ele perde o
-- objeto. Manter a linha órfã apontaria pra um id reciclado por outra pessoa —
-- que é como se cria um "não fundir" sobre quem ninguém decidiu nada.

CREATE TABLE IF NOT EXISTS contact_nao_fundir (
    id          SERIAL PRIMARY KEY,
    contact_a   INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    contact_b   INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    motivo      TEXT NOT NULL,
    decidido_por TEXT NOT NULL DEFAULT 'renato',
    criado_em   TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT contact_nao_fundir_ordem CHECK (contact_a < contact_b),
    CONSTRAINT contact_nao_fundir_par  UNIQUE (contact_a, contact_b)
);

-- Consulta quente: "este par está liberado?" nos dois sentidos.
CREATE INDEX IF NOT EXISTS idx_contact_nao_fundir_a ON contact_nao_fundir (contact_a);
CREATE INDEX IF NOT EXISTS idx_contact_nao_fundir_b ON contact_nao_fundir (contact_b);

COMMENT ON TABLE contact_nao_fundir IS
    'Pares que o Renato já decidiu que NAO sao a mesma pessoa. Consultado por '
    'verifica_google.py e pelos caminhos de merge, pra decisao tomada nao voltar '
    'a ser perguntada. Ordem canonica: contact_a < contact_b.';
