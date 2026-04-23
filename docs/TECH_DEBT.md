# INTEL - Dívida Técnica

> Documentação dos itens identificados na auditoria de 23/04/2026.
> Itens críticos já foram resolvidos.

## Resolvido ✅

| Item | Commit |
|------|--------|
| `/api/admin/reset-db` sem auth | 886a4a8 (removido) |
| `/api/debug/dashboard` sem auth | 886a4a8 (auth adicionado) |
| CORS allow_origins=["*"] | 886a4a8 (restringido) |
| URL stale intel.rafrsp.com | 886a4a8 |
| 3 índices DB faltando | 886a4a8 |
| Mock data fake em Sugestões IA | b99a0b0 (removido) |
| showToast definido 2x | b99a0b0 (removido duplicata) |
| initDashboard duplicado (-96 linhas) | b99a0b0 |
| Cron 13 steps sequenciais → 3 fases paralelas | d577042 |
| Arquivos lixo na raiz | 886a4a8 |
| Migration one-time no startup | 886a4a8 |

## Pendente — Baixa Prioridade

### Conexões DB bare (71 de 155)
- 54% já usam `with get_db() as conn:` (84 instâncias)
- 71 usam `conn = get_db()` com `conn.close()` manual
- **Não há leaks reais** (todas têm close), mas `with` é mais seguro
- Esforço: grande (71 conversões manuais), risco: médio
- Recomendação: converter gradualmente ao tocar cada endpoint

### N+1 Queries (3 reais)
- `main.py:5140` — SELECT contato dentro de loop
- `project_enrichment.py:231` — SELECT emails dentro de loop
- `batch_operations.py:133` — SELECT contato dentro de loop
- Impacto: baixo (loops pequenos, <10 iterações tipicamente)

### Funções >200 linhas (15)
- `campaign_executor.py:execute_single_enrollment` (548 linhas)
- `veiculos.py:importar_plano_manutencao_prado` (332 linhas)
- `realtime_analyzer.py:detect_intent_with_ai` (331 linhas)
- `realtime_analyzer.py:generate_action_proposals` (304 linhas)
- `veiculos.py:get_timeline_manutencao` (304 linhas)
- `main.py:sync_whatsapp_history` (291 linhas)
- E mais 9 funções entre 200-290 linhas
- Recomendação: refatorar quando modificar a função

### 283 endpoints sem auth
- Maioria são APIs internas consumidas pelo frontend (mesma sessão)
- Risco real é baixo (CORS agora restrito, Vercel não expõe diretamente)
- Solução ideal: middleware global de auth para `/api/`
- Esforço: grande (requer testar cada endpoint)

### Bare except (101 instâncias)
- 35 em main.py, 66 em services
- Maioria são `except: pass` em loops de processamento
- Escondem bugs mas não causam problemas imediatos
- Recomendação: converter para `except Exception:` gradualmente

### Services duplicados
- `duplicados.py` e `contact_dedup.py` fazem coisas similares
- `whatsapp_batch_import.py` e `whatsapp_import.py` têm código duplicado
- `contact_enrichment.py` tem 3 funções de enriquecimento com overlap
- Recomendação: merge quando modificar

### Padrões inconsistentes
- ~30 services usam classes, ~32 usam funções
- Sem regra clara de quando usar qual
- Não afeta funcionalidade, só manutenibilidade
