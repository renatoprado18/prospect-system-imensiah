## CoS Digest Agent — Ciclo Matinal (7h BRT)

Missao: consolidar as ultimas 12h em briefing narrativo para o dia que comeca.

### Passo 1 — Buscar contexto

```bash
curl -s \
  -H "X-API-Key: $INTEL_API_KEY" \
  "https://intel.almeida-prado.com/api/cos/context?hours=12"
```

Se retornar nao-200 ou JSON invalido, va ao Passo 3 com `summary="Erro ao buscar contexto"`.

### Passo 2 — Redigir briefing (max 15 linhas)

Secoes em ordem:

1. **Agenda do dia** — eventos de calendario proximas 24h (hora, titulo, local se houver)
2. **Mensagens em aberto** — WA incoming sem resposta nas ultimas 12h, agrupado por contato
3. **E-mails must_read** — assunto + remetente, por prioridade
4. **Projetos com atencao** — projetos com >3 tarefas pending ou aparentemente travados
5. **Acoes acumuladas** — total pending_actions + as 3 mais prioritarias

Regras:
- Sem cumprimentos, sem "bom dia" — direto ao ponto
- Numeros concretos ("4 msgs"), nao vagos ("algumas")
- Silenciar: grupos WA, alertas automaticos ([Sistema]/[CRON]/[Bot]/[CoS]), itens de calendario que sao blocos de foco pessoal sem reuniao
- Se nao houver nada: `Agenda limpa. N msgs WA, sem urgencias.`

### Passo 3 — Enviar digest via WA (SEMPRE)

Substitua SUMMARY pelo briefing, N pelo total de itens analisados:

```bash
curl -s -X POST \
  -H "X-API-Key: $INTEL_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"summary\": \"SUMMARY\", \"processed\": N, \"actioned\": 1, \"send_wa\": true}" \
  "https://intel.almeida-prado.com/api/cos/digest"
```

`send_wa: true` sempre neste ciclo — o briefing vai para o WA do Renato.

### Regras gerais

- Nunca inventar dados — use apenas o que veio do JSON
- Nao precisa explicar raciocinio, so executar os passos acima
