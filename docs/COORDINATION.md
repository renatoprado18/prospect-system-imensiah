# Coordenacao entre Instancias Claude Code

> **IMPORTANTE**: Este arquivo e o ponto central de comunicacao entre instancias.
> Leia SEMPRE antes de comecar qualquer trabalho.
> Atualize SEMPRE apos fazer mudancas significativas.

## Status Atual

**Ultima atualizacao**: 2026-03-21
**Instancias ativas**: 3

## Instancias e Responsabilidades

| ID | Branch | Responsavel Por | Status | Ultima Atividade |
|----|--------|-----------------|--------|------------------|
| INST-1 | feature/linkedin-email | LinkedIn integration + Email accounts | ATIVO | 2026-03-21 |
| INST-2 | feature/whatsapp-improvements | **COORD** + WhatsApp melhorias | ATIVO | 2026-03-21 |
| INST-3 | feature/scoring-icp | Scoring dinamico + ICP analysis | ATIVO | 2026-03-21 |

## Arquivos Bloqueados (Nao modificar sem coordenar)

Estes arquivos sao modificados por multiplas features. **AVISE ANTES de editar**:

```
BLOQUEADO - Coordenar antes de editar:
- app/main.py          (rotas - todas as features adicionam aqui)
- app/models.py        (schemas - impacta todos os modulos)
- app/database.py      (tabelas - mudancas de schema)
- requirements.txt     (dependencias - pode quebrar deploy)
```

## Mudancas Pendentes de Merge

| Branch | Arquivos Modificados | Conflitos Potenciais | Status |
|--------|---------------------|---------------------|--------|
| feature/linkedin-email | gmail.py, google_contacts.py, main.py | - | MERGED |
| feature/whatsapp-improvements | whatsapp.py, rap_whatsapp.html, main.py | main.py | EM PROGRESSO |
| feature/scoring-icp | app/scoring.py | Nenhum | **MERGED** |
| feature/contact-enrichment | services/enrichment.py, templates, main.py | - | **PRONTO PARA MERGE** |

## Protocolo de Merge (OBRIGATORIO)

### Passo a Passo:

```
1. INSTANCIA termina feature
   - Atualiza COORDINATION.md: "PRONTO PARA MERGE"
   - git push origin feature/sua-branch (NAO main!)
   - AGUARDA aprovacao

2. COORDENADOR revisa
   - Verifica codigo e conflitos
   - Atualiza COORDINATION.md: "APROVADO" ou "REQUER AJUSTES"
   - Define ORDEM se multiplas branches prontas

3. INSTANCIA executa merge (SOMENTE apos aprovacao)
   - git checkout main
   - git pull origin main
   - git merge feature/sua-branch
   - git push origin main
   - Atualiza COORDINATION.md: "MERGED"

4. TODAS instancias sincronizam
   - git fetch origin && git rebase origin/main
```

### Por que este processo?
- Instancia que desenvolveu conhece o codigo e resolve conflitos melhor
- Coordenador garante revisao e ordem correta
- Evita conflitos entre branches simultaneas

## Comunicacao Entre Instancias

### Para informar outra instancia:
1. Adicione uma entrada em "Mensagens Pendentes" abaixo
2. Faca commit: `git add docs/COORDINATION.md && git commit -m "coord: mensagem para INST-X"`
3. A outra instancia deve ler este arquivo antes de comecar

### Mensagens Pendentes

```
[2026-03-21 INST-2/COORD] **FEATURE: Busca em Conversas WhatsApp**
Implementado:
- Endpoint GET /api/whatsapp/search (main.py:1389)
- UI de busca na pagina WhatsApp
- Highlights nos resultados com <mark>
- Click nos resultados abre contato
Proxima feature: Agendamento de mensagens

[2026-03-21 COORD/INST-2]
AUTO-DOCUMENTACAO: Modifiquei main.py (webhook WhatsApp + search)
Mudancas:
- Linhas ~1136-1170: Novo handler para evento message_status
- Linhas ~1389-1456: Endpoint de busca em mensagens WhatsApp
Conflito: Baixo - secoes isoladas

[2026-03-21 INST-1] **FEATURE CONCLUIDA: Contact Enrichment**
Arquivos criados/modificados:
- app/services/contact_enrichment.py (novo)
- app/main.py (endpoint implementado)
- app/templates/rap_contact_detail.html (UI atualizada)
Funcionalidades: Analise AI de emails/WhatsApp, resumo, fatos, insights.
Commits em main: 054e5e0, db017dd (com aprovacao do COORD)

[2026-03-21 INST-2/COORD -> TODAS]
FEATURE CONCLUIDA: Templates de Mensagem WhatsApp
- 8 templates pre-definidos (saudacao, followup, lembrete, proposta, etc)
- Renderizacao com variaveis {nome}, {empresa}, {quando}, etc
- Endpoints: GET /api/whatsapp/templates, POST /api/whatsapp/send-template
- UI com abas: Mensagem Livre | Usar Template
- Preview em tempo real antes de enviar
```

## Decisoes Arquiteturais Tomadas

| Data | Decisao | Contexto | Tomada Por |
|------|---------|----------|------------|
| 2026-03-21 | Usar docs/ para coordenacao | Comunicacao entre instancias | COORD |
| 2026-03-21 | Protocolo merge hibrido | Instancia faz merge apos aprovacao COORD | COORD |

## Proximos Passos Globais

1. [x] Definir escopo da INST-2 (COORD + WhatsApp)
2. [x] Definir escopo da INST-3 (Scoring + ICP)
3. [x] INST-1 confirmar recebimento da coordenacao
4. [x] INST-3 confirmar recebimento da coordenacao
5. [x] Cada instancia criar sua branch e comecar trabalho
6. [ ] INST-2 finalizar WhatsApp features e fazer merge
