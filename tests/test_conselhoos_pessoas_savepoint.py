"""Uma empresa com problema cegava todas as seguintes no sync de pessoas.

Contexto (22/11/26), 30 dias em prod. O `sync-conselhoos-raci` reportava:

    "Error syncing pessoas for Vallen Clinic: ... violates foreign key
     constraint ... Key (contact_id)=(14911) is not present in table contacts"
    "Error syncing pessoas for Alba Consultoria: current transaction is
     aborted, commands ignored until end of transaction block"

Dois defeitos somados, e a segunda linha é consequência da primeira:

① o `intel_contact_id` vive no ConselhoOS (outro banco, sem FK possível) e
   aponta para ficha que o INTEL pode ter APAGADO num merge — em 06/08 as 4
   fichas do Renato viraram a #23419 e o ConselhoOS seguiu apontando para uma
   das mortas. O INSERT estourava a FK.
② todas as empresas compartilham uma conexão, e o `except` por empresa engolia
   a exceção SEM rollback: a transação ficava abortada e cada query seguinte
   morria. A **Alba não tinha defeito nenhum** — era vítima da Vallen.

E o alcance era pior que o relatado: como nada é commitado numa transação
abortada, o sync de pessoas estava INTEIRAMENTE inoperante, não parcialmente.

O que estes testes travam: SAVEPOINT por empresa, órfão virando `skipped`
nominado em vez de erro de FK, e o órfão chegando a `errors` para acender no
`cron-health` em vez de sumir num skip silencioso.
"""
import pytest

from database import get_db
from services.conselhoos_raci_sync import ConselhoOSRaciSyncService


class FakeCursor:
    """Cursor que falha numa empresa e registra se houve rollback de savepoint.

    Simula o comportamento real do Postgres: depois de um erro, toda query
    seguinte falha até vir o ROLLBACK TO SAVEPOINT. É isso que reproduz a
    contaminação — sem ele o teste passaria mesmo com o defeito.
    """

    def __init__(self, falhar_em_project_id=None, contatos_existentes=()):
        self.falhar_em = falhar_em_project_id
        self.contatos = set(contatos_existentes)
        self.abortada = False
        self.comandos = []
        self._resultado = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.comandos.append(s)

        if s.startswith("ROLLBACK TO SAVEPOINT"):
            self.abortada = False
            return
        if s.startswith("SAVEPOINT") or s.startswith("RELEASE SAVEPOINT"):
            return

        if self.abortada:
            raise RuntimeError(
                "current transaction is aborted, commands ignored until end of "
                "transaction block"
            )

        if s.startswith("SELECT 1 FROM contacts"):
            self._resultado = (1,) if params[0] in self.contatos else None
            return
        if "FROM project_members" in s and s.startswith("SELECT"):
            self._resultado = None
            return
        if s.startswith("INSERT INTO project_members"):
            if self.falhar_em is not None and params[0] == self.falhar_em:
                self.abortada = True
                raise RuntimeError(
                    'insert or update on table "project_members" violates foreign '
                    'key constraint "project_members_contact_id_fkey"'
                )
            self._resultado = None
            return
        self._resultado = None

    def fetchone(self):
        return self._resultado

    def fetchall(self):
        return []


# As 4 pessoas reproduzem o cenário medido em prod: dois terceiros válidos, o
# órfão de verdade (Renata Comin, #21264 apagado — a ficha viva é outra) e o
# SELF (#14911, uma das 4 fichas do Renato fundidas na #23419 em 06/08), que
# também não existe mas NÃO é dado a consertar.
PESSOAS = [
    {"nome": "Amadeo Comin", "intel_contact_id": 179, "papel": "conselheiro",
     "cargo": None, "empresa_id": 1, "empresa_nome": "Vallen Clinic"},
    {"nome": "Renata Comin", "intel_contact_id": 21264, "papel": "sócia",
     "cargo": None, "empresa_id": 1, "empresa_nome": "Vallen Clinic"},
    {"nome": "Renato de Faria e Almeida", "intel_contact_id": 14911,
     "papel": "conselheiro", "cargo": None, "empresa_id": 1,
     "empresa_nome": "Vallen Clinic"},
    {"nome": "Clarice Costa", "intel_contact_id": 17899, "papel": "sócia",
     "cargo": None, "empresa_id": 2, "empresa_nome": "Alba Consultoria"},
    {"nome": "Israel Reinstein", "intel_contact_id": 2690, "papel": "sócio",
     "cargo": None, "empresa_id": 2, "empresa_nome": "Alba Consultoria"},
]


def _rodar(monkeypatch, cursor, pessoas=PESSOAS):
    """Executa o sync com o ConselhoOS e o INTEL stubados."""
    svc = ConselhoOSRaciSyncService()

    class FakeConselhoOSCursor:
        def execute(self, *a, **k):
            pass

        def fetchall(self):
            return pessoas

    class FakeConselhoOSConn:
        def cursor(self):
            return FakeConselhoOSCursor()

        def close(self):
            pass

    class FakeIntelConn:
        def cursor(self):
            return cursor

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(svc, "_get_conselhoos_conn", lambda: FakeConselhoOSConn())
    monkeypatch.setattr(svc, "_find_or_create_project",
                        lambda nome, eid: {"Vallen Clinic": 24, "Alba Consultoria": 26}[nome])
    monkeypatch.setattr("services.conselhoos_raci_sync.get_db", lambda: FakeIntelConn())
    return svc.sync_pessoas_to_project_members()


VIVOS = {179, 17899, 2690}      # os 3 terceiros com ficha no INTEL


class TestOrfaoNaoEstouraFK:
    def test_contato_inexistente_e_pulado_e_nomeado(self, monkeypatch):
        """O órfão real: a Renata Comin aponta pro #21264, que não existe."""
        r = _rodar(monkeypatch, FakeCursor(contatos_existentes=VIVOS))

        assert r["orfaos"] == [{"contact_id": 21264,
                                "pessoa": "Renata Comin",
                                "empresa": "Vallen Clinic"}]
        assert "violates foreign key" not in " ".join(r["errors"])

    def test_as_outras_pessoas_da_mesma_empresa_entram(self, monkeypatch):
        """O órfão não pode custar os colegas dele: o Amadeo é da Vallen."""
        r = _rodar(monkeypatch, FakeCursor(contatos_existentes=VIVOS))
        assert r["added"] == 3      # Amadeo + Clarice + Israel

    def test_orfao_chega_a_errors_com_nome_e_empresa(self, monkeypatch):
        """Para acender no cron-health. Pular em silêncio trocaria um erro de FK
        por uma falha calada, e o conserto é fora do INTEL."""
        r = _rodar(monkeypatch, FakeCursor(contatos_existentes=VIVOS))
        assert len(r["errors"]) == 1
        msg = r["errors"][0]
        assert "Renata Comin" in msg
        assert "Vallen Clinic" in msg
        assert "21264" in msg
        assert "ConselhoOS" in msg

    def test_sem_orfao_nao_inventa_erro(self, monkeypatch):
        r = _rodar(monkeypatch, FakeCursor(contatos_existentes=VIVOS | {21264}))
        assert r["orfaos"] == []
        assert r["errors"] == []
        assert r["added"] == 4      # os 3 + a Renata; o self segue fora


class TestOProprioRenatoNaoEOrfao:
    """A ficha self sai ANTES da checagem de existência, e não como órfão.

    O #14911 é uma das 4 fichas do Renato (`SELF_CONTACT_IDS`), fundidas na
    #23419 em 06/08 — logo ela de fato não existe e a checagem de órfão a
    pegaria. Mas o veredito estaria certo pelo motivo errado, e o relatório
    pediria um conserto que NÃO deve ser feito: a #23419 já é `project_member`
    dos projetos 24 e 26 (e de outros 28), é a que `users.id=1` aponta e a que
    todo consumidor de `project_members` exclui para não ouvir o próprio eco.
    Alarme que pede a ação errada é pior que silêncio.
    """

    def test_self_nao_entra_na_lista_de_orfaos(self, monkeypatch):
        r = _rodar(monkeypatch, FakeCursor(contatos_existentes=VIVOS))
        ids = [o["contact_id"] for o in r["orfaos"]]
        assert 14911 not in ids

    def test_self_nao_aparece_em_errors(self, monkeypatch):
        r = _rodar(monkeypatch, FakeCursor(contatos_existentes=VIVOS))
        assert "Renato" not in " ".join(r["errors"])

    def test_self_e_contado_como_skipped(self, monkeypatch):
        """Pulado, não perdido: o número tem que fechar."""
        r = _rodar(monkeypatch, FakeCursor(contatos_existentes=VIVOS))
        assert r["added"] + r["skipped"] == len(PESSOAS)

    def test_self_pulado_mesmo_se_a_ficha_VOLTAR_a_existir(self, monkeypatch):
        """Se alguém "consertar" o `intel_contact_id` no ConselhoOS, o sync não
        pode passar a inserir o Renato como membro-terceiro."""
        r = _rodar(monkeypatch, FakeCursor(contatos_existentes=VIVOS | {21264, 14911}))
        assert r["orfaos"] == []
        assert r["added"] == 4, "entram os 4 terceiros; o self fica fora"

    def test_a_ficha_canonica_tambem_e_self(self, monkeypatch):
        """#23419 vem de `users.id=1` e precisa estar coberta — é a que o
        ConselhoOS passaria a apontar depois de um remapeamento."""
        pessoas = [dict(p) for p in PESSOAS]
        pessoas[2]["intel_contact_id"] = 23419
        r = _rodar(monkeypatch, FakeCursor(contatos_existentes=VIVOS | {21264, 23419}),
                   pessoas=pessoas)
        assert r["orfaos"] == []
        assert r["added"] == 4


class TestUmaEmpresaNaoCegaAsOutras:
    def test_falha_na_vallen_nao_contamina_a_alba(self, monkeypatch):
        """O defeito de 30 dias. A Alba vinha com 'current transaction is
        aborted' sem ter problema nenhum."""
        cur = FakeCursor(falhar_em_project_id=24,
                         contatos_existentes=VIVOS | {21264})
        r = _rodar(monkeypatch, cur)

        erros = " ".join(r["errors"])
        assert "Vallen" in erros
        assert "Alba" not in erros, "a Alba não tem defeito — era contaminação"
        assert "current transaction is aborted" not in erros

    def test_o_trabalho_da_empresa_sa_sobrevive(self, monkeypatch):
        cur = FakeCursor(falhar_em_project_id=24,
                         contatos_existentes=VIVOS | {21264})
        r = _rodar(monkeypatch, cur)
        assert r["added"] == 2, "Clarice e Israel, da Alba, têm que entrar"

    def test_houve_rollback_para_o_savepoint(self, monkeypatch):
        """É o rollback que desfaz a contaminação — sem ele o `except` apenas
        esconde a primeira falha e entrega as seguintes como defeito próprio."""
        cur = FakeCursor(falhar_em_project_id=24,
                         contatos_existentes=VIVOS | {21264})
        _rodar(monkeypatch, cur)
        assert any(c.startswith("ROLLBACK TO SAVEPOINT") for c in cur.comandos)
        assert sum(c == "SAVEPOINT empresa" for c in cur.comandos) == 2

    def test_empresa_sem_erro_libera_o_savepoint(self, monkeypatch):
        """Savepoint não liberado acumula no servidor em sync longo."""
        cur = FakeCursor(contatos_existentes=VIVOS | {21264})
        _rodar(monkeypatch, cur)
        assert sum(c == "RELEASE SAVEPOINT empresa" for c in cur.comandos) == 2


class TestContratoDoBancoReal:
    """Os três comandos de savepoint são válidos no Postgres que usamos.

    O resto do arquivo roda contra um cursor falso: isto existe para que um erro
    de sintaxe não passe por causa disso.
    """

    def test_savepoint_release_e_rollback_sao_aceitos(self):
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SAVEPOINT empresa")
            cur.execute("SELECT 1")
            cur.execute("RELEASE SAVEPOINT empresa")
            cur.execute("SAVEPOINT empresa")
            with pytest.raises(Exception):
                cur.execute("SELECT * FROM tabela_que_nao_existe_xyz")
            cur.execute("ROLLBACK TO SAVEPOINT empresa")
            cur.execute("SELECT 1")          # a transação voltou a funcionar
            assert cur.fetchone() is not None
