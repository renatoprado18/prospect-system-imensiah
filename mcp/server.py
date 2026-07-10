"""
server.py — CoPiloto MCP server (transporte stdio).

Superficie de trabalho profundo do Renato. Expoe o grafo core (projetos, tasks,
docs, contatos), memoria semantica, percepcao e ConselhoOS (read-only) para uma
superficie rica (Claude Desktop / Claude Code / Cursor). Quem raciocina e o
Claude do outro lado; este server so da acesso a dados + escrita controlada.

Rodar:  python mcp/server.py       (stdio; o cliente MCP inicia o processo)
Config: ver mcp/README.md

O server NAO tem inteligencia propria. Toda escrita e auditada em mcp_audit_log.
SEM tools de ENVIO (WA/email) no MVP — so grava no dado.
"""

import json
import logging
from typing import Any, Dict, Optional

from mcp.server.fastmcp import FastMCP

import db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("copilot_mcp.server")

mcp = FastMCP("copiloto-renato")


def _json(obj: Any) -> str:
    """Serializa pra string JSON estavel (datas -> str). O modelo le como texto."""
    return json.dumps(obj, ensure_ascii=False, default=str, indent=2)


# ===========================================================================
# LEITURA
# ===========================================================================
@mcp.tool()
def search_projects(query: Optional[str] = None, status: Optional[str] = None) -> str:
    """Busca projetos do Renato por texto livre (nome/descricao/empresa) e/ou status.
    Use pra localizar um projeto antes de abri-lo. status ex: 'ativo', 'pausado', 'concluido'.
    Retorna lista resumida (id, nome, status, prioridade, empresa, tags)."""
    return _json(db.search_projects(query=query, status=status))


@mcp.tool()
def get_project(id: int) -> str:
    """Abre um projeto completo: metadados + tasks + notas + documentos vinculados.
    Use pra ter a visao 360 antes de trabalhar (ex: projeto 28 = Exportacao Direta Cafe Jabo)."""
    proj = db.get_project(id)
    return _json(proj) if proj else _json({"error": f"projeto {id} nao encontrado"})


@mcp.tool()
def search_tasks(project_id: Optional[int] = None, status: Optional[str] = None,
                due_before: Optional[str] = None, contact_id: Optional[int] = None) -> str:
    """Busca tarefas com filtros combinaveis. project_id: so as do projeto. status:
    'pending'/'completed'/'cancelled'. due_before: ISO 'YYYY-MM-DD' (vencendo ate).
    contact_id: tarefas ligadas a um contato. Sem filtro = tarefas mais urgentes."""
    return _json(db.search_tasks(project_id=project_id, status=status,
                                 due_before=due_before, contact_id=contact_id))


@mcp.tool()
def get_task(id: int) -> str:
    """Detalhe de uma tarefa por id (titulo, descricao, status, vencimento, projeto, contato)."""
    t = db.get_task(id)
    return _json(t) if t else _json({"error": f"task {id} nao encontrada"})


@mcp.tool()
def search_contacts(query: str) -> str:
    """Busca contatos por nome/apelido/empresa/cargo. Retorna resumo com circulo de
    relacionamento (1=mais intimo), health_score e resumo AI."""
    return _json(db.search_contacts(query))


@mcp.tool()
def get_contact(id: int) -> str:
    """Contato completo + ultimas mensagens (WhatsApp/email). Use antes de draftar
    qualquer comunicacao pra esse contato."""
    c = db.get_contact(id)
    return _json(c) if c else _json({"error": f"contato {id} nao encontrado"})


@mcp.tool()
def get_project_documents(project_id: int) -> str:
    """Lista os documentos vinculados a um projeto (pesquisas, drafts, anexos arquivados)."""
    return _json(db.get_project_documents(project_id))


@mcp.tool()
def get_document(id: int) -> str:
    """Le um documento por id (nome, conteudo/descricao, tags, entidades vinculadas)."""
    d = db.get_document(id)
    return _json(d) if d else _json({"error": f"documento {id} nao encontrado"})


@mcp.tool()
def search_memories(query: str, k: int = 6) -> str:
    """Busca semantica na memoria do Renato (decisoes, compromissos, padroes, sinteses).
    Retorna os k trechos mais relevantes. Use pra recuperar contexto historico antes de
    decidir ou draftar. Cai pra busca por palavra-chave se o provider de embedding estiver off."""
    return _json(db.search_memories(query, k=k))


@mcp.tool()
def search_group_messages(group: Optional[str] = None, query: Optional[str] = None,
                          days: int = 30, k: int = 40) -> str:
    """Mensagens dos grupos WhatsApp de conselho (ex: reportes de RACI no grupo
    'Conselho Vallen'). Fonte bruta dos updates que os conselheiros mandam — vive
    separada das DMs (tabela group_messages). group: nome do grupo (ex 'vallen').
    query: filtra conteudo. days: janela em dias. Use ANTES de afirmar se algo foi
    reportado no grupo — nunca diga 'nao sei se enviaram' sem consultar aqui."""
    return _json(db.search_group_messages(group=group, query=query, days=days, k=k))


@mcp.tool()
def get_cockpit() -> str:
    """Percepcao do momento: sinais abertos + tarefas vencidas + agenda das proximas 24h.
    Use no inicio de uma sessao de trabalho pra saber o que precisa de acao."""
    return _json(db.get_cockpit())


@mcp.tool()
def get_conselho(empresa: Optional[str] = None) -> str:
    """ConselhoOS (read-only): reunioes, itens RACI e decisoes dos conselhos.
    empresa: filtra por nome (ex: 'Vallen'). Sem empresa = todas as visiveis.
    Retorna [] se a integracao ConselhoOS nao estiver configurada."""
    return _json(db.get_conselho(empresa=empresa))


# ===========================================================================
# ESCRITA (controlada + auditada). SEM ENVIO.
# ===========================================================================
@mcp.tool()
def create_task(titulo: str, project_id: Optional[int] = None,
                due_date: Optional[str] = None, descricao: Optional[str] = None) -> str:
    """Cria uma tarefa. titulo obrigatorio. project_id opcional (vincula a projeto).
    due_date ISO 'YYYY-MM-DD'. descricao opcional. Origem marcada como 'mcp_copilot'
    e auditada. NAO envia nada — so registra a tarefa."""
    try:
        return _json(db.create_task(titulo=titulo, project_id=project_id,
                                    due_date=due_date, descricao=descricao))
    except Exception as e:
        return _json({"error": str(e)})


@mcp.tool()
def update_task(id: int, campos: Dict[str, Any]) -> str:
    """Atualiza uma tarefa. campos = objeto com quaisquer de: status, titulo, descricao,
    due_date, project_id, prioridade, contact_id. Ex corrigir classificacao:
    {"status":"completed"} ou {"project_id": 28, "due_date": "2026-07-20"}. Auditado."""
    try:
        return _json(db.update_task(id, campos))
    except Exception as e:
        return _json({"error": str(e)})


@mcp.tool()
def create_document(project_id: int, titulo: str, conteudo: str,
                   tipo: Optional[str] = None) -> str:
    """Arquiva um documento (pesquisa, draft, relatorio) num projeto. conteudo = texto
    completo (markdown ok). tipo ex: 'research', 'draft', 'ata'. Cria a row em
    documentos + vincula ao projeto. Auditado. Ex: arquivar pesquisa de cafe no projeto 28."""
    try:
        return _json(db.create_document(project_id=project_id, titulo=titulo,
                                        conteudo=conteudo, tipo=tipo))
    except Exception as e:
        return _json({"error": str(e)})


@mcp.tool()
def create_note(project_id: int, texto: str) -> str:
    """Adiciona uma nota a um projeto (observacao rapida, draft curto, decisao). Auditado."""
    try:
        return _json(db.create_note(project_id=project_id, texto=texto))
    except Exception as e:
        return _json({"error": str(e)})


@mcp.tool()
def save_memory(kind: str, key: str, value: str) -> str:
    """Grava/atualiza uma memoria persistente (kind+key = chave unica; upsert).
    kind ex: 'fact'/'preference'/'decision'. Gera embedding pra busca semantica futura.
    Use pra o CoPiloto lembrar de algo entre sessoes. Auditado."""
    try:
        return _json(db.save_memory(kind=kind, key=key, value=value))
    except Exception as e:
        return _json({"error": str(e)})


if __name__ == "__main__":
    mcp.run()  # transporte stdio por padrao
