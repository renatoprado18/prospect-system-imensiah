"""Teto por seção e validade — o verificador tem que achar o que diz achar.

O RISCO ESPECÍFICO desta feature: "nenhuma entrada vencida" e "meu regex não
casou com nada" saem idênticos na tela. Um marcador de seção errado (o emoji do
título muda, alguém renomeia "Fechado" para "Concluído") faz o script certificar
em silêncio um board que ele nunca leu — o mesmo modo de falha que já apareceu
aqui em filtro com vocabulário errado e em medidor que não mede a si mesmo.

Por isso os testes abaixo checam o DENOMINADOR (quantas entradas foram casadas
contra quantas existem), não só o veredito.

Rodar: .venv/bin/python -m pytest tests/test_verifica_boards_secao.py -v
"""
import importlib.util
import os
from datetime import date, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _carrega(mem_dir):
    """Carrega o script apontando MEM pro diretório de teste."""
    spec = importlib.util.spec_from_file_location(
        "vb", os.path.join(_ROOT, "scripts", "verifica_boards.py"))
    vb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vb)
    vb.MEM = str(mem_dir)
    return vb


def _board(mem, nome, entradas, cabecalho="## 📓 Log"):
    """Escreve um board mínimo com a seção que acumula."""
    corpo = "---\nname: x\n---\n\n## 🔒 Estado\n\ntabela aqui\n\n" + cabecalho + "\n\n"
    corpo += "\n\n".join(entradas) + "\n"
    (mem / nome).write_text(corpo, encoding="utf-8")


@pytest.fixture
def mem(tmp_path):
    return tmp_path


def test_acha_as_entradas_denominador_bate(mem):
    """CONTROLE POSITIVO: as 3 entradas datadas têm que ser casadas — se o regex
    achar 0 de 3, o veredito 'nada vencido' seria mentira."""
    vb = _carrega(mem)
    hoje = vb._hoje()
    velha = hoje - timedelta(days=30)
    _board(mem, "session_locks.md", [
        f"- **{velha.day:02d}/{velha.month:02d} (CoS→Dev) — recado antigo.** corpo",
        f"- **{hoje.day:02d}/{hoje.month:02d} (Dev→CoS) — recado de hoje.** corpo",
        "  - sub-item sem data, não é entrada",
        f"- **{velha.day:02d}/{velha.month:02d} noite (Dev) — outro antigo.** corpo",
    ])
    _, vencidos = vb.medir_secoes(
        "session_locks.md", str(mem / "session_locks.md"), hoje)
    assert len(vencidos) == 2, f"esperava 2 vencidas, achou {len(vencidos)}: {vencidos}"
    assert all(v[0] == 30 for v in vencidos), f"idades erradas: {vencidos}"


def test_entrada_dentro_da_validade_nao_vence(mem):
    """CONTROLE NEGATIVO: alarme que dispara pra tudo não é alarme."""
    vb = _carrega(mem)
    hoje = vb._hoje()
    ontem = hoje - timedelta(days=1)
    _board(mem, "session_locks.md", [
        f"- **{ontem.day:02d}/{ontem.month:02d} (Dev) — recente.** corpo",
    ])
    _, vencidos = vb.medir_secoes(
        "session_locks.md", str(mem / "session_locks.md"), hoje)
    assert vencidos == []


def test_secao_ausente_nao_certifica_em_silencio(mem):
    """Se o marcador não bate, não pode sair 'tudo em dia' — tem que sair NADA
    medido. O teste existe pra travar o modo de falha, não pra elogiar a saída:
    hoje a função devolve listas vazias, e é por isso que a checagem de
    denominador acima é obrigatória em qualquer mudança de marcador."""
    vb = _carrega(mem)
    hoje = vb._hoje()
    velha = hoje - timedelta(days=99)
    _board(mem, "session_locks.md",
           [f"- **{velha.day:02d}/{velha.month:02d} (Dev) — antiquíssimo.** corpo"],
           cabecalho="## 📮 Recados")            # marcador DIFERENTE do configurado
    estouros, vencidos = vb.medir_secoes(
        "session_locks.md", str(mem / "session_locks.md"), hoje)
    assert (estouros, vencidos) == ([], []), \
        "seção não encontrada tem que devolver vazio, nunca um veredito"


def test_teto_de_secao_dispara(mem):
    """A seção estoura mesmo com o arquivo inteiro pequeno — é o alarme
    ANTECIPADO: quem cresce é a seção, e o total é a consequência."""
    vb = _carrega(mem)
    hoje = vb._hoje()
    vb.SECOES["session_locks.md"] = [("## 📓 Log", 512, 7, "teste")]
    _board(mem, "session_locks.md", [
        f"- **{hoje.day:02d}/{hoje.month:02d} (Dev) — " + ("x" * 800) + ".** corpo",
    ])
    estouros, _ = vb.medir_secoes(
        "session_locks.md", str(mem / "session_locks.md"), hoje)
    assert len(estouros) == 1 and "teto de seção" in estouros[0], estouros


def test_data_impossivel_nao_vence(mem):
    """31/02 é entrada malformada, não entrada velha — inventar uma data faria
    o script mandar descer conteúdo por um erro de digitação."""
    vb = _carrega(mem)
    hoje = vb._hoje()
    _board(mem, "session_locks.md", ["- **31/02 (Dev) — data impossível.** corpo"])
    _, vencidos = vb.medir_secoes(
        "session_locks.md", str(mem / "session_locks.md"), hoje)
    assert vencidos == []


def test_virada_de_ano_nao_inventa_futuro(mem):
    """Board lido em janeiro com entrada de dezembro: é do ano passado, e a
    idade tem que ser positiva — não uma data no futuro com idade negativa."""
    vb = _carrega(mem)
    jan = date(2027, 1, 5)
    d = vb._parse_data(28, 12, jan)
    assert d == date(2026, 12, 28), d
    assert (jan - d).days == 8
