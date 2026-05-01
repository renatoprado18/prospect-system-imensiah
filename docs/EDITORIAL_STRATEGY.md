# Editorial LinkedIn — Estratégia e Próximos Passos

> Baseado na análise de 9 posts publicados em abril/2026.
> Engagement médio atual: 1.58% (meta: 2.8-3.2% em 60 dias)

## O que funciona
- IA + provocação + contraste (4.67% engagement)
- Títulos com tensão: "X faz Y, mas Z acontece"
- Números específicos + linguagem direta
- Temas: IA aplicada, governança com casos reais

## O que NÃO funciona
- Economia macro genérica (0% engagement)
- Tom institucional/acadêmico
- Falta de call-to-action
- Sem conexão pessoal

## Implementar (próxima sessão)

### 1. Feedback Loop na IA
- Incluir top 5 e bottom 5 posts (com métricas) no prompt de geração
- Prompt: "gere controvérsia respeitosa" + exemplos dos melhores
- Atualizar semanalmente os exemplos

### 2. Coleta de Métricas em 4 Pontos
- 6h: pico inicial
- 24h: engagement orgânico
- 72h: alcance estendido (atual)
- 7 dias: cauda longa
- Meia-vida: 80% em 48h, 15% em 48h-7d, 5% residual

### 3. Business Score (novo KPI)
```
Score = (Cliques perfil × 3) + (Comentários C-level × 2) + 
        (Compartilhamentos × 1.5) + (Mensagens DM × 5)
```

### 4. Frequência e Horários
- 4 posts/semana (reduzir de 5)
- Ter-Qui: 8h-10h e 17h-19h
- Seg: 14h-16h
- Sex: até 15h
- Sáb: 9h-11h (conteúdo pessoal/inspiracional)

### 5. Análise Semanal Automática
- Cron segunda de manhã
- Gera relatório com insights + ajusta prompt da IA
- Compara semana atual vs anterior

### 6. A/B Testing
- 50% texto puro vs 50% texto+imagem
- 4 semanas para conclusão

---

## F1 Shipped — 2026-05-01

Commits: `f7480e4` (feature) + `65b1445` (bug fix auto-resolve)

| Item | Status | Onde |
|------|--------|------|
| Feedback loop top/bottom no prompt | ✅ | `editorial_pdca.get_top_bottom_examples()` injetado em `auto_publisher.select_weekly_posts` e `generate_weekly_briefing` |
| Coleta em 4 pontos (6h/24h/72h/7d) | ✅ | Cron `editorial-metrics-reminder` (11h SP) + `-evening` (20h SP), dedup via `editorial_metrics_history` |
| Alerta cadência semanal | ✅ | Seção nova em `cron_daily_morning_briefing`: quinta+ sem post → ⚠️ |
| Bug fix: auto-resolve `weekly_editorial` | ✅ | `action_proposals.auto_resolve_weekly_editorial()` plugado em `get_pending_proposals()` |

**Checkpoint agendado**: routine `trig_01GJRJ2MYoC1qjeDGa9zoHf9` roda em 2026-05-15 (9h SP) — mede engagement rate vs baseline 1.58%, comenta no commit se subiu, abre PR de tuning de prompts se caiu/igual.

**Pendente F2** (esperar dados do checkpoint para Business Score; horários e briefing podem ir antes):
1. Business Score como KPI
2. Análise semanal automática estendida
3. Horários otimizados em `schedule_selected_posts`
