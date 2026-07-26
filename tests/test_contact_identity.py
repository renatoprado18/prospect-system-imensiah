"""
Testes da CASCATA DE IDENTIDADE do sync do Google (25/07).

O que esta coberto (sem tocar Neon, Google nem rede):
  - guarda do FIXO COMPARTILHADO: Douglas Bassi x Orestes (551135761505)
    nao podem casar — o caso real que motivou a guarda
  - criterio de similaridade de nome por tipo de linha (celular x fixo)
  - cascata: casa por gid / por mapa multi-conta / por e-mail / por
    telefone+nome / nao casa -> INSERT
  - mapa multi-conta {conta: resourceName} gravado e reusado
  - kill-switch CONTACT_SYNC_IDENTITY_CASCADE / CONTACT_SYNC_PHONE_MATCH
  - uniao de emails/telefones/contexto na linha compartilhada (sem ping-pong)
  - delete de uma conta nao apaga ficha que a outra conta ainda usa

Rodar: .venv/bin/python -m pytest tests/test_contact_identity.py -v
"""
import json
import os
import re
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

from services.contact_identity import (  # noqa: E402
    GOOGLE_IDS_COLUMN,
    GOOGLE_IDS_KEY,
    ContactIndex,
    contact_emails,
    contact_phones,
    google_ids_map,
    merge_contexto,
    merge_json_lists,
    names_match,
    phone_kind,
    significant_tokens,
    unlink_google_id,
)
from services.contact_dedup import normalize_phone  # noqa: E402
from integrations.google_contacts import _apply_contact  # noqa: E402


PESSOAL = "renato.almeida.prado@gmail.com"
PROFISSIONAL = "renato@almeida-prado.com"


# ==================== Fake DB ====================

class FakeCursor:
    """
    Mini-tabela `contacts` em memoria. Reconhece exatamente as queries que
    _apply_contact / link_google_id / unlink_google_id emitem.
    """

    def __init__(self, rows=None):
        self.rows = {r["id"]: self._blank(r) for r in (rows or [])}
        self._next_id = max(self.rows, default=0) + 1
        self._result = []
        self.rowcount = 0

    # -- helpers --
    @staticmethod
    def _blank(row):
        base = {
            "id": None, "nome": "", "emails": [], "telefones": [],
            "contexto": "", "google_contact_id": None,
            GOOGLE_IDS_COLUMN: {}, "empresa": "", "cargo": "",
            "foto_url": None, "linkedin": None, "aniversario": None,
            "enderecos": [], "relacionamentos": [], "origem": None,
        }
        base.update(row)
        return base

    def execute(self, sql, params=()):
        norm = " ".join(sql.split())
        self._result = []
        self.rowcount = 0

        if norm.startswith("SELECT id FROM contacts WHERE google_contact_id ="):
            gid = params[0]
            self._result = [
                {"id": r["id"]} for r in self.rows.values()
                if r.get("google_contact_id") == gid
            ][:1]

        elif norm.startswith("SELECT id, nome, emails, telefones, google_contact_id"):
            self._result = [dict(r) for r in self.rows.values()]

        elif norm.startswith("SELECT id FROM contacts WHERE emails @>"):
            wanted = json.loads(params[0])[0]["email"]
            self._result = [
                {"id": r["id"]} for r in self.rows.values()
                if any(e.get("email") == wanted for e in r.get("emails", []))
            ][:1]

        elif norm.startswith("SELECT nome, emails, telefones, contexto"):
            row = self.rows.get(params[0])
            if row:
                self._result = [{
                    "nome": row["nome"], "emails": row["emails"],
                    "telefones": row["telefones"], "contexto": row["contexto"],
                    GOOGLE_IDS_COLUMN: row[GOOGLE_IDS_COLUMN],
                }]

        elif norm.startswith(f"SELECT id, google_contact_id, {GOOGLE_IDS_COLUMN}"):
            gid, _key, account, _gid2 = params
            self._result = [
                {"id": r["id"], "google_contact_id": r.get("google_contact_id"),
                 GOOGLE_IDS_COLUMN: r[GOOGLE_IDS_COLUMN]}
                for r in self.rows.values()
                if r.get("google_contact_id") == gid
                or (r[GOOGLE_IDS_COLUMN].get(GOOGLE_IDS_KEY, {}) or {}).get(account) == gid
            ][:1]

        elif norm.startswith("INSERT INTO contacts"):
            (nome, empresa, cargo, emails, telefones, foto, li, agenda,
             ends, rels, gid, contexto, origem, ids_blob) = params
            new_id = self._next_id
            self._next_id += 1
            self.rows[new_id] = self._blank({
                "id": new_id, "nome": nome, "empresa": empresa, "cargo": cargo,
                "emails": json.loads(emails), "telefones": json.loads(telefones),
                "google_contact_id": gid, "contexto": contexto, "origem": origem,
                GOOGLE_IDS_COLUMN: json.loads(ids_blob),
            })
            self._result = [{"id": new_id}]
            self.rowcount = 1

        elif norm.startswith("UPDATE contacts SET nome ="):
            (nome, empresa, cargo, emails, telefones, foto, li, agenda,
             ends, rels, contexto, cid) = params
            row = self.rows[cid]
            row["nome"] = nome
            row["empresa"] = empresa or row["empresa"]
            row["cargo"] = cargo or row["cargo"]
            row["emails"] = json.loads(emails)
            row["telefones"] = json.loads(telefones)
            row["contexto"] = contexto
            self.rowcount = 1

        elif norm.startswith(f"UPDATE contacts SET {GOOGLE_IDS_COLUMN} = jsonb_set"):
            if "jsonb_build_object" in norm:          # link_google_id
                _path, _key, account, gid, gid2, cid = params
                row = self.rows[cid]
                blob = row[GOOGLE_IDS_COLUMN].setdefault(GOOGLE_IDS_KEY, {})
                blob[account] = gid
                if not row.get("google_contact_id"):
                    row["google_contact_id"] = gid2
            else:                                      # unlink_google_id
                _path, remaining, old_gid, fallback, cid = params
                row = self.rows[cid]
                row[GOOGLE_IDS_COLUMN][GOOGLE_IDS_KEY] = json.loads(remaining)
                if row.get("google_contact_id") == old_gid:
                    row["google_contact_id"] = fallback
            self.rowcount = 1

        elif norm.startswith("DELETE FROM contacts WHERE id ="):
            if params[0] in self.rows:
                del self.rows[params[0]]
                self.rowcount = 1

        elif norm.startswith("DELETE FROM contacts WHERE google_contact_id ="):
            victims = [r["id"] for r in self.rows.values()
                       if r.get("google_contact_id") == params[0]]
            for v in victims:
                del self.rows[v]
            self.rowcount = len(victims)

        else:  # pragma: no cover
            raise AssertionError(f"query nao prevista no fake: {norm[:120]}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return self._result


def parsed(nome, gid, account, emails=None, phones=None, empresa="", cargo=""):
    """Imita a saida de parse_google_contact."""
    return {
        "nome": nome,
        "empresa": empresa,
        "cargo": cargo,
        "emails": [{"email": e, "type": "other", "primary": False} for e in (emails or [])],
        "telefones": [{"number": p, "type": "mobile", "whatsapp": True} for p in (phones or [])],
        "foto_url": None,
        "linkedin": None,
        "aniversario": None,
        "enderecos": [],
        "relacionamentos": [],
        "google_contact_id": gid,
        "contexto": "personal" if "gmail.com" in account else "professional",
        "origem": f"google_{account}",
    }


@pytest.fixture(autouse=True)
def cascata_ligada(monkeypatch):
    monkeypatch.delenv("CONTACT_SYNC_IDENTITY_CASCADE", raising=False)
    monkeypatch.delenv("CONTACT_SYNC_PHONE_MATCH", raising=False)


# ==================== Nome + telefone ====================

class TestGuardaFixoCompartilhado:
    """
    551135761505 tem "Douglas Bassi" e "Orestes Alves de Almeida Prado" na base
    real: e um FIXO COMPARTILHADO, pessoas distintas. Merge cego por telefone
    corromperia dado. Este e o caso que a guarda existe pra barrar.
    """

    def test_douglas_e_orestes_nao_casam(self):
        assert phone_kind(normalize_phone("551135761505")) == "landline"
        assert names_match("Douglas Bassi", "Orestes Alves de Almeida Prado",
                           "landline") is False
        # nem afrouxando pra celular
        assert names_match("Douglas Bassi", "Orestes Alves de Almeida Prado",
                           "mobile") is False

    def test_cascata_nao_casa_douglas_com_orestes(self):
        cur = FakeCursor([{
            "id": 10, "nome": "Orestes Alves de Almeida Prado",
            "telefones": [{"number": "+55 11 3576-1505"}], "emails": [],
            "contexto": "personal", "google_contact_id": "gid-orestes",
            GOOGLE_IDS_COLUMN: {GOOGLE_IDS_KEY: {PESSOAL: "gid-orestes"}},
        }])
        idx = ContactIndex()
        novo = parsed("Douglas Bassi", "gid-douglas", PROFISSIONAL,
                      phones=["+55 11 3576-1505"])

        assert _apply_contact(cur, idx, novo, PROFISSIONAL) == "imported"
        assert len(cur.rows) == 2
        assert cur.rows[10]["nome"] == "Orestes Alves de Almeida Prado"

    def test_fixo_casa_so_com_contencao_total(self):
        # mesma pessoa escrita mais curta -> casa
        assert names_match("Orestes Alves de Almeida Prado",
                           "Orestes Almeida Prado", "landline") is True
        # duas pessoas da mesma casa -> nao casa
        assert names_match("Ana Silva", "Ana Costa", "landline") is False
        # mononimo nao identifica ninguem num fixo
        assert names_match("Andressa", "Andressa Souza", "landline") is False

    def test_celular_e_mais_frouxo_que_fixo(self):
        # 1o nome + 1 sobrenome em comum basta num celular
        assert names_match("Renato Prado", "Renato Almeida Prado", "mobile") is True
        assert names_match("Renato Prado", "Renato Almeida Prado", "landline") is True
        # sobrenome divergente nao passa nem no celular
        assert names_match("Renato Prado", "Renato Silva", "mobile") is False
        # mononimo passa so no celular
        assert names_match("Andressa", "Andressa Souza", "mobile") is True

    def test_primeiro_nome_sempre_obrigatorio(self):
        assert names_match("Joao Silva", "Pedro Silva", "mobile") is False
        assert names_match("Joao Silva", "Pedro Silva", "landline") is False

    def test_desconhecido_herda_o_rigor_do_fixo(self):
        assert phone_kind("12345") == "unknown"
        assert names_match("Andressa", "Andressa Souza", "unknown") is False

    def test_nome_identico_casa_em_qualquer_linha(self):
        """
        Sem esta saida o criterio rigoroso rejeitava ate ficha identica —
        "Voitel" x "Voitel" no mesmo fixo, 3 copias na base real.
        """
        assert names_match("Voitel", "VOITEL", "landline") is True
        assert names_match("Carol", "Carol", "unknown") is True
        # e continua barrando o caso real de fixo compartilhado
        assert names_match("Douglas Bassi", "Orestes Alves de Almeida Prado",
                           "landline") is False


class TestNormalizacao:
    def test_tokens_ignoram_particula_inicial_e_acento(self):
        assert significant_tokens("Orestes A. de Almeida Prado") == [
            "orestes", "almeida", "prado"
        ]
        assert significant_tokens("JOSÉ DA SILVA JR") == ["jose", "silva"]

    def test_phone_kind(self):
        assert phone_kind(normalize_phone("(11) 98415-3337")) == "mobile"
        assert phone_kind(normalize_phone("(11) 3576-1505")) == "landline"

    def test_telefone_sem_ddd_nao_identifica(self):
        assert contact_phones({"telefones": [{"number": "3576-1505"}]}) == []

    def test_email_de_setor_nao_identifica(self):
        got = contact_emails({"emails": [
            {"email": "  CONTATO@Empresa.com "},
            {"email": "Fulano@Empresa.com"},
        ]})
        assert got == ["fulano@empresa.com"]


# ==================== Cascata ====================

def _base_pessoal():
    """Ficha ja existente, vinda da conta pessoal."""
    return FakeCursor([{
        "id": 1,
        "nome": "Antonio Piccino",
        "emails": [{"email": "piccino@clinica.com.br"}],
        "telefones": [{"number": "+55 11 98415-3337"}],
        "contexto": "personal",
        "google_contact_id": "gid-pessoal",
        GOOGLE_IDS_COLUMN: {GOOGLE_IDS_KEY: {PESSOAL: "gid-pessoal"}},
    }])


class TestCascata:
    def test_casa_por_google_contact_id(self):
        cur, idx = _base_pessoal(), ContactIndex()
        rec = parsed("Antonio Piccino", "gid-pessoal", PESSOAL)
        assert _apply_contact(cur, idx, rec, PESSOAL) == "updated"
        assert len(cur.rows) == 1

    def test_casa_por_mapa_multi_conta(self):
        """
        A ficha ja registrou o gid da conta profissional no mapa mas a coluna
        escalar continua com o da pessoal — tem que reconhecer mesmo assim.
        """
        cur = _base_pessoal()
        cur.rows[1][GOOGLE_IDS_COLUMN][GOOGLE_IDS_KEY][PROFISSIONAL] = "gid-prof"
        idx = ContactIndex()
        rec = parsed("Antonio Piccino", "gid-prof", PROFISSIONAL)
        assert _apply_contact(cur, idx, rec, PROFISSIONAL) == "updated"
        assert len(cur.rows) == 1

    def test_casa_por_email_nao_primario(self):
        """
        O caminho antigo so comparava o e-mail PRIMARIO. Aqui o e-mail que
        casa e o segundo da lista.
        """
        cur, idx = _base_pessoal(), ContactIndex()
        rec = parsed("Antonio Piccino", "gid-prof", PROFISSIONAL,
                     emails=["outro@x.com", "PICCINO@Clinica.com.br"])
        assert _apply_contact(cur, idx, rec, PROFISSIONAL) == "updated"
        assert len(cur.rows) == 1
        assert google_ids_map(cur.rows[1])[PROFISSIONAL] == "gid-prof"

    def test_casa_por_telefone_mais_nome(self):
        """
        69% da base nao tem e-mail. Este e o tier que faltava — e o que
        segurava as 1.785 duplicatas uma-ficha-por-conta.
        """
        cur, idx = _base_pessoal(), ContactIndex()
        rec = parsed("Antonio Piccino", "gid-prof", PROFISSIONAL,
                     phones=["11984153337"])
        assert _apply_contact(cur, idx, rec, PROFISSIONAL) == "updated"
        assert len(cur.rows) == 1

    def test_nao_casa_insere(self):
        cur, idx = _base_pessoal(), ContactIndex()
        rec = parsed("Fulano Desconhecido", "gid-novo", PROFISSIONAL,
                     phones=["11999990000"])
        assert _apply_contact(cur, idx, rec, PROFISSIONAL) == "imported"
        assert len(cur.rows) == 2

    def test_telefone_igual_nome_diferente_insere(self):
        cur, idx = _base_pessoal(), ContactIndex()
        rec = parsed("Marcia Ferreira", "gid-novo", PROFISSIONAL,
                     phones=["11984153337"])
        assert _apply_contact(cur, idx, rec, PROFISSIONAL) == "imported"
        assert len(cur.rows) == 2

    def test_duas_contas_no_mesmo_lote_casam_entre_si(self):
        """
        Num full resync as duas contas passam na mesma execucao. O indice
        precisa enxergar o que a propria execucao acabou de inserir.
        """
        cur, idx = FakeCursor([]), ContactIndex()
        a = parsed("Antonio Piccino", "gid-a", PESSOAL, phones=["11984153337"])
        b = parsed("Antonio Piccino", "gid-b", PROFISSIONAL, phones=["11984153337"])
        assert _apply_contact(cur, idx, a, PESSOAL) == "imported"
        assert _apply_contact(cur, idx, b, PROFISSIONAL) == "updated"
        assert len(cur.rows) == 1


class TestMapaMultiConta:
    def test_mapa_guarda_as_duas_contas(self):
        cur, idx = _base_pessoal(), ContactIndex()
        rec = parsed("Antonio Piccino", "gid-prof", PROFISSIONAL,
                     phones=["11984153337"])
        _apply_contact(cur, idx, rec, PROFISSIONAL)
        mapa = google_ids_map(cur.rows[1])
        assert mapa == {PESSOAL: "gid-pessoal", PROFISSIONAL: "gid-prof"}

    def test_coluna_escalar_nao_e_sobrescrita(self):
        """Ela tem UNIQUE constraint e consumidores legados: preenche so se NULL."""
        cur, idx = _base_pessoal(), ContactIndex()
        rec = parsed("Antonio Piccino", "gid-prof", PROFISSIONAL,
                     phones=["11984153337"])
        _apply_contact(cur, idx, rec, PROFISSIONAL)
        assert cur.rows[1]["google_contact_id"] == "gid-pessoal"

    def test_insert_ja_nasce_com_o_mapa(self):
        cur, idx = FakeCursor([]), ContactIndex()
        rec = parsed("Gente Nova", "gid-x", PROFISSIONAL)
        _apply_contact(cur, idx, rec, PROFISSIONAL)
        novo = cur.rows[max(cur.rows)]
        assert google_ids_map(novo) == {PROFISSIONAL: "gid-x"}

    def test_index_reconhece_gid_vindo_do_mapa(self):
        idx = ContactIndex()
        idx.add_contact({
            "id": 7, "nome": "X", "emails": [], "telefones": [],
            "google_contact_id": "gid-a",
            GOOGLE_IDS_COLUMN: {GOOGLE_IDS_KEY: {PROFISSIONAL: "gid-b"}},
        })
        assert idx.by_gid["gid-a"] == 7
        assert idx.by_gid["gid-b"] == 7


class TestLinhaCompartilhada:
    def test_uniao_de_emails_e_telefones_sem_ping_pong(self):
        """
        Com a linha alimentada pelas duas contas, substituir emails/telefones
        pelo que veio de uma conta apagaria o que a outra escreveu — a cada
        sync. Tem que unir.
        """
        cur, idx = _base_pessoal(), ContactIndex()
        rec = parsed("Antonio Piccino", "gid-prof", PROFISSIONAL,
                     emails=["piccino@escritorio.com"], phones=["11984153337"])
        _apply_contact(cur, idx, rec, PROFISSIONAL)
        row = cur.rows[1]
        assert {e["email"] for e in row["emails"]} == {
            "piccino@clinica.com.br", "piccino@escritorio.com"
        }
        assert len(row["telefones"]) == 1          # mesmo numero, formatos diferentes
        assert row["contexto"] == "personal,professional"

    def test_nome_de_melhor_qualidade_vence(self):
        cur = _base_pessoal()
        cur.rows[1]["nome"] = "ANTONIO PICCINO"
        idx = ContactIndex()
        rec = parsed("Antônio Piccino", "gid-prof", PROFISSIONAL,
                     phones=["11984153337"])
        _apply_contact(cur, idx, rec, PROFISSIONAL)
        assert cur.rows[1]["nome"] == "Antônio Piccino"

    def test_sync_da_mesma_conta_nao_une(self):
        """Linha de uma conta so mantem a semantica antiga: Google manda."""
        cur, idx = _base_pessoal(), ContactIndex()
        rec = parsed("Antonio Piccino", "gid-pessoal", PESSOAL,
                     emails=["novo@x.com"])
        _apply_contact(cur, idx, rec, PESSOAL)
        assert [e["email"] for e in cur.rows[1]["emails"]] == ["novo@x.com"]
        assert cur.rows[1]["contexto"] == "personal"


class TestDelete:
    def test_delete_de_uma_conta_preserva_ficha_da_outra(self):
        cur = _base_pessoal()
        cur.rows[1][GOOGLE_IDS_COLUMN][GOOGLE_IDS_KEY][PROFISSIONAL] = "gid-prof"
        assert unlink_google_id(cur, PESSOAL, "gid-pessoal") == "unlinked"
        assert 1 in cur.rows
        assert google_ids_map(cur.rows[1]) == {PROFISSIONAL: "gid-prof"}
        # escalar apontava pro gid apagado -> repassa pra conta viva
        assert cur.rows[1]["google_contact_id"] == "gid-prof"

    def test_delete_da_ultima_conta_apaga(self):
        cur = _base_pessoal()
        assert unlink_google_id(cur, PESSOAL, "gid-pessoal") == "deleted"
        assert cur.rows == {}


class TestKillSwitch:
    def test_desligado_volta_a_duplicar(self, monkeypatch):
        """
        Com a flag OFF o comportamento antigo tem que continuar alcancavel —
        inclusive o defeito: mesma pessoa, gid de outra conta -> INSERT.
        """
        monkeypatch.setenv("CONTACT_SYNC_IDENTITY_CASCADE", "0")
        cur, idx = _base_pessoal(), ContactIndex()
        rec = parsed("Antonio Piccino", "gid-prof", PROFISSIONAL,
                     phones=["11984153337"])
        assert _apply_contact(cur, idx, rec, PROFISSIONAL) == "imported"
        assert len(cur.rows) == 2

    def test_desligado_mantem_fallback_de_email_primario(self, monkeypatch):
        monkeypatch.setenv("CONTACT_SYNC_IDENTITY_CASCADE", "0")
        cur, idx = _base_pessoal(), ContactIndex()
        rec = parsed("Antonio Piccino", "gid-prof", PROFISSIONAL,
                     emails=["piccino@clinica.com.br"])
        assert _apply_contact(cur, idx, rec, PROFISSIONAL,
                              legacy_email_fallback=True) == "updated"
        assert len(cur.rows) == 1

    def test_phone_match_off_mantem_email(self, monkeypatch):
        monkeypatch.setenv("CONTACT_SYNC_PHONE_MATCH", "0")
        cur, idx = _base_pessoal(), ContactIndex()
        so_telefone = parsed("Antonio Piccino", "gid-prof", PROFISSIONAL,
                             phones=["11984153337"])
        assert _apply_contact(cur, idx, so_telefone, PROFISSIONAL) == "imported"

        cur2, idx2 = _base_pessoal(), ContactIndex()
        com_email = parsed("Antonio Piccino", "gid-prof2", PROFISSIONAL,
                           emails=["piccino@clinica.com.br"])
        assert _apply_contact(cur2, idx2, com_email, PROFISSIONAL) == "updated"


class TestGuardaLinhaDeMuitos:
    def test_ramal_com_muitas_fichas_nao_identifica(self):
        rows = [{
            "id": i, "nome": f"Fulano Silva {i}", "emails": [],
            "telefones": [{"number": "1130000000"}],
            "google_contact_id": f"g{i}", GOOGLE_IDS_COLUMN: {},
            "contexto": "professional",
        } for i in range(1, 11)]
        cur, idx = FakeCursor(rows), ContactIndex()
        rec = parsed("Fulano Silva 3", "gid-novo", PESSOAL, phones=["1130000000"])
        assert _apply_contact(cur, idx, rec, PESSOAL) == "imported"


class TestHelpersDeUniao:
    def test_merge_json_lists_preserva_ordem_e_dedup(self):
        got = merge_json_lists(
            [{"number": "+55 11 98415-3337"}],
            [{"number": "11984153337"}, {"number": "1133334444"}],
            "number", normalize_phone,
        )
        assert [g["number"] for g in got] == ["+55 11 98415-3337", "1133334444"]

    def test_merge_contexto_dedup(self):
        assert merge_contexto("personal,professional", "professional") == \
            "personal,professional"
        assert merge_contexto(None, "personal") == "personal"
