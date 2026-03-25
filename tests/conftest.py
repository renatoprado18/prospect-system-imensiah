"""
Pytest fixtures compartilhadas para todos os testes.
"""
import pytest
from datetime import datetime, timedelta


@pytest.fixture
def contact_familia():
    """Contato com tag familia - deve ser Circulo 1"""
    return {
        "id": 1,
        "nome": "Maria Silva",
        "tags": ["familia", "mae"],
        "total_interacoes": 50,
        "ultimo_contato": datetime.now().isoformat(),
        "empresa": None,
        "cargo": None,
        "contexto": "personal"
    }


@pytest.fixture
def contact_conselho():
    """Contato de conselho - deve ser Circulo 2"""
    return {
        "id": 2,
        "nome": "Joao Diretor",
        "tags": ["conselho", "board", "vallen"],
        "total_interacoes": 20,
        "ultimo_contato": (datetime.now() - timedelta(days=10)).isoformat(),
        "empresa": "Vallen Clinic",
        "cargo": "CEO",
        "contexto": "professional"
    }


@pytest.fixture
def contact_ativo():
    """Contato ativo com muitas interacoes - deve ser Circulo 2-3"""
    return {
        "id": 3,
        "nome": "Carlos Cliente",
        "tags": ["cliente", "vip"],
        "total_interacoes": 35,
        "ultimo_contato": (datetime.now() - timedelta(days=5)).isoformat(),
        "empresa": "Tech Corp",
        "cargo": "CTO",
        "linkedin": "https://linkedin.com/in/carlos",
        "contexto": "professional"
    }


@pytest.fixture
def contact_conhecido():
    """Contato ocasional - deve ser Circulo 4"""
    return {
        "id": 4,
        "nome": "Ana Networking",
        "tags": [],
        "total_interacoes": 8,
        "ultimo_contato": (datetime.now() - timedelta(days=45)).isoformat(),
        "empresa": "Startup X",
        "cargo": "Founder",
        "contexto": "professional"
    }


@pytest.fixture
def contact_arquivo():
    """Contato sem interacao - deve ser Circulo 5"""
    return {
        "id": 5,
        "nome": "Pedro Antigo",
        "tags": [],
        "total_interacoes": 0,
        "ultimo_contato": None,
        "empresa": None,
        "cargo": None,
        "contexto": None
    }


@pytest.fixture
def sample_contacts(contact_familia, contact_conselho, contact_ativo, contact_conhecido, contact_arquivo):
    """Lista de contatos de teste"""
    return [contact_familia, contact_conselho, contact_ativo, contact_conhecido, contact_arquivo]
