# COS Diligencia — Triggers Operacionais Pendentes

Camada de "alertas operacionais" do INTEL (Chief of Staff). Detector roda em
cima do classificador de mensagens (cron `classify-messages`, hourly) e emite
`action_proposals` quando reconhece sinais criticos.

Arquivo principal: `app/services/operational_alerts.py`.
Wire-up: `app/services/message_classifier.py::classify_pending_batch`.

## Status atual (10/06/2026)

| Trigger | Nome | Status | Notas |
|---|---|---|---|
| P1 | operational_risk | implementado | cirurgia/afastamento/luto/atestado de funcionaria-chave |
| P2 | active_recruitment | pendente | curriculo/CV/indicacao cross-ref com vagas RACI ativas |
| P3 | kpi_discrepancy | pendente | material novo com KPIs divergentes de atas anteriores |
| P4 | stuck_raci_item | pendente | item vencido + 0 update em 30+ dias |

## P2 — Active recruitment

**Sinal:** mensagem com palavras tipo "curriculo", "CV", "indicacao", "recebi do",
"te apresento" — geralmente com anexo (PDF/imagem) ou link.

**Cross-reference:** RACI items ativos (status='in_progress' ou 'pending') que
mencionem vagas em aberto. Para Vallen no MVP: "Concierge", "Gerente Comercial".
Talvez modelar como `operational_watch.vacancies` ou pegar das raci_items por
keywords no titulo.

**Output:** action_proposal tipo `active_recruitment` com:
- title: "Avaliar candidato no contexto da vaga X"
- description: nome do candidato (se extraivel), vaga linkada, link pra anexo
- options: ['marcar entrevista', 'pedir mais info', 'rejeitar']

**Dificuldade:** anexo nao chega no `messages.conteudo` — precisa olhar
`messages.media_url` ou tabela de anexos. Confirmar.

## P3 — KPI discrepancy

**Sinal:** novo arquivo (sheet, PDF) chegou via Gmail ou WhatsApp anexo +
contem numeros (faturamento, atendimentos, conversao).

**Cross-reference:** ultimas 2-3 atas do mesmo cliente (`conselhoos_atas` ou
similar). Extrair KPIs por LLM (Haiku, structured JSON), comparar com baseline.

**Output:** action_proposal tipo `kpi_discrepancy` com bloco de pauta proposto
pro proximo conselho.

**Dificuldade:** OCR/parsing de PDFs ja existe (procurar `services/ocr*`). LLM
extraction de KPIs precisa schema curado por cliente — Vallen tem 4-5 KPIs
relevantes (faturamento, ticket medio, retorno, NPS).

## P4 — Stuck RACI item

**Sinal:** SQL puro, roda no cron diario (nao depende de mensagem).
- raci_items vencidos (due_date < NOW())
- sem atualizacao ha 30+ dias (no update timestamp)

**Output:** action_proposal tipo `stuck_raci` com sugestao: "Item X em risco
estrutural — propor remocao ou redesign na proxima ata".

**Wire:** novo cron diario `/api/cron/raci-stuck-check` ou step no daily-sync.

## Configuracao de funcionarias-chave (P1)

Atualmente hardcoded em `operational_alerts.py::KEY_PERSONNEL`. Migrar pra
config persistente (system_memories ou tabela `operational_watch`) assim que
houver mais de 2-3 empresas monitoradas. Estrutura proposta:

```sql
CREATE TABLE operational_watch (
    id SERIAL PRIMARY KEY,
    company TEXT NOT NULL,
    person_name TEXT NOT NULL,
    role TEXT,
    criticality TEXT, -- 'high' | 'medium' | 'low'
    notes TEXT,
    criado_em TIMESTAMP DEFAULT NOW()
);
```

## Caso real que motivou (10/06/2026)

Dra. Thalita (Vallen Clinic, contact 5715) escreveu para Renato:

> "Veridiana me avisou agora que irá fazer uma cirurgia segunda (ela disse que
> não conseguiu escolher a data e acabaram de avisar)"

Veridiana e recepcionista chave da Vallen — sai segunda sem plano de
cobertura. INTEL nao sinalizou proativamente. Renato deu feedback duro:
"INTEL nao esta inteligente". P1 cobre.

Proposta gerada (apos backfill): #689, urgency=high,
title="Alerta operacional Vallen Clinic: Veridiana (cirurgia)".
