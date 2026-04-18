# Modulos do Sistema INTEL - Referencia

## Services (app/services/) — 60 modulos

### Relacionamentos
| Modulo | Funcao | Funcoes Principais |
|--------|--------|--------------------|
| circulos.py | Circulos C1-C5 + health score | `calcular_score_circulo()`, `calcular_health_score()`, `recalcular_todos_circulos()` |
| briefings.py | Briefings pre-reuniao com IA | `generate_briefing()`, `get_contacts_needing_briefing()` |
| briefing_context.py | Contexto enriquecido para briefings | `get_contexto_enriquecido()`, `detectar_promessas_pendentes()` |
| contact_enrichment.py | Enriquecimento de contatos | `auto_enrich_priority_contacts()`, `enrich_with_context()` |
| contact_dedup.py | Deteccao de duplicatas | `find_duplicates()`, `merge_contacts()` |
| contact_intelligence.py | Inteligencia por contato | `get_insight()`, `chat()` |
| dashboard.py | API unificada dashboard | `get_full_dashboard()`, `get_dashboard_stats()` |
| search.py | Busca avancada | `search_contacts()`, `search_global()` |
| timeline.py | Timeline de contato | `get_timeline()` |
| duplicados.py | Gestao de duplicados | `find_duplicate_groups()` |

### Projetos
| Modulo | Funcao | Funcoes Principais |
|--------|--------|--------------------|
| projects.py | CRUD projetos + milestones | `get_project()`, `add_milestone()`, `update_milestone()`, `get_project_briefing_context()` |
| project_enrichment.py | Enriquecimento IA | `enrich_project()` |
| project_smart_update.py | Smart Update + Parecer IA | `analyze_project_updates()`, `generate_project_analysis()`, `_fetch_group_messages()` |
| payment_cycle.py | Ciclo financeiro automatizado | `send_payment_email()`, `check_payment_replies()`, `create_next_cycle()` |
| task_context.py | Contexto de tarefas | `get_task_context()`, `generate_action_suggestion()` |
| tasks_sync.py | Sync Google Tasks | `update_task()`, `complete_task()` |

### Comunicacao
| Modulo | Funcao | Funcoes Principais |
|--------|--------|--------------------|
| whatsapp_sync.py | Sync WhatsApp | `sync_all_chats()`, `process_webhook()` |
| whatsapp_import.py | Import .txt WhatsApp | `import_chat()` |
| whatsapp_batch_import.py | Import em lote | `batch_import()` |
| whatsapp_notifications.py | Notificacoes WA | `send_notification()` |
| gmail_sync.py | Sync Gmail | `sync_all_contacts()`, `count_messages_for_email()` |
| email_triage.py | Triagem de emails | `triage_emails()` |
| inbox.py | Inbox unificado | `get_unified_inbox()` |
| realtime_analyzer.py | Analise de intencao | `analyze_message()`, `detect_intent_with_ai()` |
| action_proposals.py | Propostas de acao | `create_proposal()`, `dismiss_stale_on_reply()`, `expire_old_proposals()` |
| action_executor.py | Execucao de acoes | `execute_action()` |
| smart_fup.py | Follow-up automatico | `check_pending_fups()` |
| analyzer_feedback.py | Feedback do analyzer | `record_feedback()` |

### IA & Automacao
| Modulo | Funcao | Funcoes Principais |
|--------|--------|--------------------|
| ai_agent.py | Orquestrador IA | `call_claude()`, `run_daily_generation()` |
| smart_triggers.py | Automacoes | `run_all_triggers()` |
| health_predictions.py | Previsao de churn | `generate_health_predictions()` |
| message_suggestions.py | Sugestao de msgs | `suggest_reconnect()`, `suggest_birthday()` |
| digest_generator.py | Resumos diarios | `generate_daily_digest()` |
| photo_validation.py | Validacao de foto | `validate_profile_photo()` |
| notifications.py | SSE notifications | `send_notification()` |
| push_notifications.py | Browser push | `send_push()` |

### Conteudo
| Modulo | Funcao | Funcoes Principais |
|--------|--------|--------------------|
| editorial_calendar.py | Calendario editorial | `generate_post()`, `schedule_post()` |
| hot_takes.py | Hot takes LinkedIn | `generate_hot_take()` |
| news_hub.py | Agregador noticias | `fetch_news()`, `categorize()` |
| campaign_service.py | Campanhas | `create_campaign()`, `execute_step()` |
| campaign_executor.py | Executor campanhas | `process_pending_steps()` |
| content_matcher.py | Match conteudo-contato | `match_articles()` |
| business_matcher.py | Match negocio | `find_matches()` |

### Veiculos
| Modulo | Funcao | Funcoes Principais |
|--------|--------|--------------------|
| veiculos.py | Manutencao + OCR | `extrair_dados_nf_foto()`, `finalizar_ordem_servico()`, `garantir_pasta_nf_drive()` |
| oficinas.py | Cadastro oficinas | `criar_oficina()`, `listar_oficinas()` |

### Integracao
| Modulo | Funcao | Funcoes Principais |
|--------|--------|--------------------|
| conselhoos_sync.py | Sync ConselhoOS | `get_dashboard_summary()`, `get_proximas_reunioes()` |
| linkedin_enrichment.py | Enriquecimento LinkedIn | `enrich_contact()` |
| linkedin_import.py | Import LinkedIn | `import_connections()` |
| calendar_sync.py | Sync calendario | `sync_events()` |
| calendar_events.py | Gestao eventos | `create_event()` |
| calendar_ai.py | IA para calendario | `suggest_meeting()` |
| apollo_service.py | Apollo.io API | `search_people()` |
| rodas_service.py | Sistema Rodas | `get_rodas()` |

---

## Integracoes (app/integrations/)

| Integracao | Arquivo | Funcao |
|------------|---------|--------|
| Google Calendar | google_calendar.py | Eventos, reunioes, Google Meet |
| Google Contacts | google_contacts.py | Sync contatos, OAuth |
| Google Drive | google_drive.py | Upload, folders, documentos |
| Google Tasks | google_tasks.py | Sync tarefas |
| Gmail | gmail.py | Leitura, envio, threading |
| WhatsApp | whatsapp.py + evolution_api.py | Evolution API: envio, recebimento, grupos |
| LinkedIn | linkedin.py | Enriquecimento basico |

---

## Templates (app/templates/) — 34 paginas

### Principais
| Template | Pagina | Descricao |
|----------|--------|-----------|
| rap_dashboard.html | / | Dashboard principal com morning briefing |
| rap_contacts.html | /contatos | Lista de contatos com filtros |
| rap_contact_detail.html | /contatos/{id} | Detalhe do contato com tabs |
| rap_circulos.html | /circulos | Dashboard de circulos |
| rap_briefings.html | /briefings | Geracao de briefings |
| rap_projetos.html | /projetos | Lista de projetos |
| rap_projeto_detail.html | /projetos/{id} | Detalhe com Smart Update + Parecer IA |
| rap_veiculos.html | /veiculos | Lista de veiculos |
| rap_veiculo_detalhe.html | /veiculos/{id} | Detalhe com upload NF via foto |
| rap_ordem_servico.html | /veiculos/{id}/os/{id} | OS com link NF no Drive |
| rap_oficinas.html | /oficinas | Gestao de oficinas |
| rap_calendario.html | /calendario | Calendario integrado |
| rap_inbox.html | /inbox | Inbox unificado |
| rap_whatsapp.html | /whatsapp | Chat WhatsApp |
| rap_campanhas.html | /campanhas | Gestao de campanhas |
| editorial.html | /editorial | Calendario editorial |
| hot_takes.html | /hot-takes | Hot takes LinkedIn |
| rap_analytics.html | /analytics | Analytics e metricas |
| rap_base.html | - | Template base (sidebar, CSS) |

---

## Banco de Dados — 65 tabelas

### Core
- `contacts`, `conversations`, `messages`, `users`

### Relacionamentos
- `contact_facts`, `contact_memories`, `circulos_config`

### Projetos
- `projects`, `project_members`, `project_milestones`, `project_notes`, `project_events`, `project_messages`, `project_whatsapp_groups`, `tasks`

### Veiculos
- `veiculos`, `veiculo_itens_manutencao`, `veiculo_manutencoes`, `veiculo_ordens_servico`, `veiculo_documentos`, `oficinas`

### IA & Automacao
- `ai_suggestions`, `ai_automations`, `ai_digests`, `action_proposals`, `analyzer_feedback`, `health_predictions`, `message_templates`

### Comunicacao
- `google_accounts`, `calendar_events`, `calendar_sync_state`, `whatsapp_instances`

### Conteudo
- `editorial_posts`, `hot_takes`, `news_articles`, `campaigns`, `campaign_steps`

### Documentos
- `documentos`, `documento_links`
