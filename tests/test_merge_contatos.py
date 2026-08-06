"""O merge de fichas apaga contato. Estes testes guardam a ordem que impede perda.

Das 32 FKs que apontam pra `contacts`, a MAIORIA é CASCADE. Apagar a ficha
absorvida sem migrar antes levaria junto mensagens, fatos, conversas e tasks —
silenciosamente, porque CASCADE não dá erro.

Em 06/08 este script absorveu 293 fichas e religou 1.721+ registros. Um bug de
ordem ali teria apagado histórico de gente real, e o backup é o único caminho de
volta.

Rodar: .venv/bin/python -m pytest tests/test_merge_contatos.py -v
"""
import importlib.util
import os
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _mod():
    spec = importlib.util.spec_from_file_location("_merge", _ROOT / "scripts" / "merge_contatos.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["_merge"] = m
    spec.loader.exec_module(m)
    return m


class _CurFake:
    """Registra a ordem das operações — é ela que este teste protege."""

    def __init__(self):
        self.ops = []
        self.rowcount = 1

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.upper().startswith("DELETE FROM CONTACTS"):
            self.ops.append(("DELETE_CONTACT", s))
        elif s.upper().startswith("UPDATE"):
            self.ops.append(("UPDATE", s.split()[1]))
        elif s.upper().startswith("DELETE"):
            self.ops.append(("DELETE_LINHA", s.split()[2]))
        self.rowcount = 1

    def fetchall(self):
        return []

    def fetchone(self):
        return None


def test_apaga_a_ficha_so_no_fim():
    """A regra que impede perda: TODA FK migra antes do DELETE.

    Se o DELETE vier antes de qualquer UPDATE, o CASCADE leva junto mensagens e
    fatos da ficha absorvida — e não levanta erro nenhum.
    """
    m = _mod()
    cur = _CurFake()
    m.merge(cur, fica=100, absorvidas=[200], aplicar=True)

    tipos = [o[0] for o in cur.ops]
    assert "DELETE_CONTACT" in tipos, "não chegou a apagar a ficha"
    assert tipos.index("DELETE_CONTACT") == len(tipos) - 1, (
        f"DELETE veio antes do fim — CASCADE apagaria dado migrável. Ordem: {tipos}"
    )
    assert tipos.count("UPDATE") >= 25, "migrou poucas tabelas; faltou FK"


def test_dry_run_nao_apaga():
    """Sem --apply nada é destruído: é assim que se confere antes."""
    m = _mod()
    cur = _CurFake()
    m.merge(cur, fica=100, absorvidas=[200], aplicar=False)

    assert not any(o[0] == "DELETE_CONTACT" for o in cur.ops)


def test_cobre_as_32_fks():
    """FK que ficar de fora vira registro órfão OU dado apagado por CASCADE.

    O catálogo real tinha 32 em 06/08. Se alguém criar tabela nova apontando pra
    `contacts` e esquecer daqui, este número denuncia.
    """
    m = _mod()
    assert len(m.FKS) >= 32, f"a lista tem {len(m.FKS)} FKs; o catálogo tinha 32"
    tabelas = {t for t, _ in m.FKS}
    for essencial in ("messages", "contact_facts", "conversations", "tasks",
                      "group_messages", "users", "project_members"):
        assert essencial in tabelas, f"{essencial} fora da migração — perde dado"


def test_conflito_de_unique_e_resolvido_antes():
    """6 tabelas têm UNIQUE com contact_id: se as DUAS fichas já têm a mesma
    linha, o UPDATE cego viola a constraint e derruba o merge inteiro.

    Aconteceu de verdade em 06/08 com `project_members` — as duas fichas no
    mesmo projeto. A linha redundante da absorvida é descartada ANTES.
    """
    m = _mod()
    cur = _CurFake()
    m.merge(cur, fica=100, absorvidas=[200], aplicar=True)

    for tabela in m.CONFLITO:
        idx_del = [i for i, o in enumerate(cur.ops)
                   if o[0] == "DELETE_LINHA" and tabela in o[1]]
        idx_upd = [i for i, o in enumerate(cur.ops)
                   if o[0] == "UPDATE" and o[1] == tabela]
        assert idx_del and idx_upd, f"{tabela}: falta descarte ou update"
        assert idx_del[0] < idx_upd[0], (
            f"{tabela}: o UPDATE veio antes do descarte — viola o UNIQUE"
        )


def test_parse_recusa_manter_ficha_fora_do_grupo():
    """"MERGE 1-2 -> manter #99" é erro de digitação, não instrução. Aceitar
    apagaria as duas fichas e manteria uma que não estava no grupo."""
    m = _mod()
    merges, _ = m.parse_decisoes("MERGE 1-2 -> manter #99\nMERGE 3-4 -> manter #3")

    assert len(merges) == 1 and merges[0][0] == 3


def test_nao_marcados_como_diferentes_ficam_de_fora():
    m = _mod()
    merges, naos = m.parse_decisoes("NAO 5-6\nMERGE 7-8 -> manter #7")

    assert len(naos) == 1 and len(merges) == 1
    assert 5 not in [f for f, _ in merges]


def test_reconecta_quando_a_conexao_cai():
    """O Neon derruba conexão em lote longo. Sem reconexão o processo fica VIVO
    pendurado no socket: sem query, sem erro, sem progresso — foi o que
    aconteceu em 06/08 e me fez reportar 'gravando' por horas sem estar."""
    fonte = (_ROOT / "scripts" / "merge_contatos.py").read_text()
    assert "keepalives" in fonte, "sem keepalive o socket morre calado"
    assert "OperationalError" in fonte and "conectar()" in fonte, "não reconecta"
    assert "conn.commit()" in fonte and "por grupo" in fonte, "sem commit por grupo, cai e perde tudo"


def test_backup_antes_de_escrever():
    """Merge errado sem backup é irreversível — não há como reconstruir a ficha
    apagada."""
    fonte = (_ROOT / "scripts" / "merge_contatos.py").read_text()
    i_bkp = fonte.index("backup_fichas_merge.json")
    i_merge = fonte.index("m = merge(cur, fica, vivos, aplicar)")
    assert i_bkp < i_merge, "o backup tem que ser gravado ANTES do primeiro merge"
