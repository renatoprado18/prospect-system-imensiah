"""RACI parser - 3 padroes Fathom + convencao BR.

Usado no callback Fathom (criacao de tasks) E no investigator (leitura).
Centralizado pra ser fonte unica de verdade.

Memoria: feedback_raci_parsing.md
"""
import re
from dataclasses import dataclass
from typing import Optional

# Lista de nomes que indicam Renato (variantes)
RENATO_ALIASES = (
    "renato",
    "renato prado",
    "renato de faria",
    "renato de faria prado",
    "rdap",
    "renato a. prado",
    "renato a prado",
)

# Padrao 1: prefix "Nome:" ou "Nome + Nome:" no titulo (inicio da string)
# Aceita acentos PT-BR e suporta multi-nome via " + " ou ", "
_PREFIX_PATTERN = re.compile(
    r"^([A-ZÀ-Ý][a-zA-Zà-ÿ]+(?:\s+[A-ZÀ-Ý][a-zA-Zà-ÿ]+)*"
    r"(?:\s*[+,]\s*[A-ZÀ-Ý][a-zA-Zà-ÿ]+(?:\s+[A-ZÀ-Ý][a-zA-Zà-ÿ]+)*)*)\s*:\s+"
)

# Padroes 2/3: linhas R: e A: na descricao
_RACI_R_PATTERN = re.compile(r"(?:^|\n)\s*R\s*:\s*([^\n]+?)(?:\s*,\s*A\s*:|$|\n)", re.IGNORECASE)
_RACI_A_PATTERN = re.compile(r"(?:,|\n)\s*A\s*:\s*([^\n,]+)", re.IGNORECASE)


@dataclass
class RaciResult:
    responsible: Optional[str]  # nome do R parseado (ou A se convencao BR)
    is_renato: bool             # True se responsavel final eh Renato
    convention: str             # 'prefix' | 'international' | 'br' | 'none'
    source: str                 # 'prefix' | 'descricao_R' | 'descricao_R_BR' | 'none'
    confidence: float           # 0.0-1.0


def _is_renato(name: str) -> bool:
    if not name:
        return False
    n = name.strip().lower()
    # Trim trailing pontuacao
    n = n.rstrip(".,;:!?")
    return n in RENATO_ALIASES or any(n.startswith(a + " ") for a in RENATO_ALIASES)


def parse_raci(titulo: str, descricao: str) -> RaciResult:
    """Parser unificado dos 3 padroes. Retorna RaciResult.

    Ordem de prioridade:
    1. Prefix em titulo (mais confiavel — Fathom output)
    2. R: linha em descricao (RACI BR ou intl)
    3. None
    """
    titulo = (titulo or "").strip()
    descricao = descricao or ""

    # Padrao 1: prefix no titulo
    if titulo:
        m = _PREFIX_PATTERN.match(titulo)
        if m:
            name_full = m.group(1).strip()
            # Multi-nome ("Renata + Lara" / "Thalita + Amadeo") -> primeiro
            first = re.split(r"\s*[+,]\s*", name_full)[0].strip()
            is_ren = _is_renato(first)
            return RaciResult(
                responsible=first,
                is_renato=is_ren,
                convention="prefix",
                source="prefix",
                confidence=0.95,
            )

    # Padrao 2/3: R: linha em descricao
    if descricao and re.search(r"(?:^|\n)\s*R\s*:", descricao, re.IGNORECASE):
        m_r = _RACI_R_PATTERN.search(descricao)
        if m_r:
            r_name = m_r.group(1).strip().rstrip(".,;:")
            # Primeiro nome em casos "Lara Silva" -> "Lara Silva" (full); split por virgula
            r_first = r_name.split(",")[0].strip()
            r_is_renato = _is_renato(r_first)

            # Detect convencao BR: se R=Renato AND A presente AND A != "N/A"
            m_a = _RACI_A_PATTERN.search(descricao)
            a_name = (m_a.group(1).strip().rstrip(".,;:") if m_a else "")

            if r_is_renato and a_name and a_name.lower() not in ("n/a", "na", "none", "-", ""):
                # Convencao BR: R=accountable, A=quem executa
                # Renato e so accountable; executor real eh A
                a_is_renato = _is_renato(a_name)
                return RaciResult(
                    responsible=a_name,
                    is_renato=a_is_renato,
                    convention="br",
                    source="descricao_R_BR",
                    confidence=0.80,
                )

            # Padrao internacional ou R != Renato
            return RaciResult(
                responsible=r_first,
                is_renato=r_is_renato,
                convention="international",
                source="descricao_R",
                confidence=0.85,
            )

    return RaciResult(
        responsible=None,
        is_renato=True,  # default: assume Renato quando nao ha sinal
        convention="none",
        source="none",
        confidence=0.0,
    )


if __name__ == "__main__":
    # Smoke tests
    cases = [
        # (titulo, descricao, expected_is_renato, expected_source)
        ("Amadeo: ajustar relatorio analitico", "qualquer descricao", False, "prefix"),
        ("Renato: finalizar contrato", "", True, "prefix"),
        ("Lara: preparar lista de recalls", "RACI da reuniao", False, "prefix"),
        ("Revisao contrato Dr Marcelo", "RACI da reuniao.\nR: Renato, A: Amadeo", False, "descricao_R_BR"),
        ("Acompanhamento Kommo", "R: Lara, A: N/A", False, "descricao_R"),
        ("Generic task sem RACI", "", True, "none"),
        # Multi-nome
        ("Renata + Lara: estruturar campanha", "", False, "prefix"),
        ("Thalita + Amadeo: revisar processo", "", False, "prefix"),
        # R != Renato sem A
        ("Push pra produção", "Pre-reuniao.\nR: Amadeo", False, "descricao_R"),
        # Convencao BR completa
        ("Definir SLA", "Ata.\nR: Renato, A: Lara", False, "descricao_R_BR"),
        # R=Renato A=N/A → mantem Renato
        ("Aprovar proposta", "R: Renato, A: N/A", True, "descricao_R"),
    ]
    passed = 0
    failed = 0
    for titulo, descricao, expected_renato, expected_source in cases:
        result = parse_raci(titulo, descricao)
        ok = (result.is_renato == expected_renato and result.source == expected_source)
        status = "OK" if ok else "FAIL"
        if ok:
            passed += 1
        else:
            failed += 1
        print(
            f"[{status}] titulo='{titulo[:42]:<42}' "
            f"-> is_renato={result.is_renato} src={result.source} "
            f"resp={result.responsible!r} conv={result.convention} conf={result.confidence}"
        )
    print(f"\n{passed} passed, {failed} failed")
