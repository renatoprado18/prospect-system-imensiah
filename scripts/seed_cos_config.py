#!/usr/bin/env python3
"""
Seed/refresh do CoS Config no system_memories.

Cria uma nova linha tipo='cos_config' com o conteudo abaixo. O briefing 7h
e outros services do INTEL-como-Chief-of-Staff leem `get_active_cos_config()`
(retorna o mais recente). Manter historico via INSERT — nao tem update.

Rodar:
    python scripts/seed_cos_config.py            # usa DATABASE_URL do env atual
    DATABASE_URL=<prod_url> python scripts/seed_cos_config.py  # forca prod

Origem do conteudo: sessao de extracao com Renato em jun/2026, trio ratificado
(prioridades v5, politicas duraveis, mandato do CoS digital).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
sys.stdout.reconfigure(line_buffering=True)

from services.system_memory import save_system_memory, get_active_cos_config


COS_CONFIG_CONTENT = """# CoS Config — Renato (sessao de extracao jun/2026)

## Bloco 1 — Prioridades estrategicas Q2-Q3 2026 (RATIFICADO v5)

5 frentes no nivel alto. Sub-rubricas so onde signal vale mais que simplicidade.

| # | Frente | Peso | Como medir |
|---|---|---|---|
| 1 | imensIAH + Assespro (canal) | 30% | PMF + pilots Assespro avancando |
| 2 | ConselhoOS new biz + Wadhwani (canal) | 20% | Pipeline de pagantes novos |
| 3 | Vida pessoal | 30% | Sub: 3a=20%, 3b=10% |
| 3a | Familia + decisao SP/Japao | 20% | Piso presenca + progresso decisao |
| 3b | Saude fisica | 10% | Sono, treino, medicos preventivos |
| 4 | Almeida Prado (firma + Vallen) | 15% | Receita + entrega Vallen |
| 5 | Capital relacional (Despertar -> Villela) | 5% | Avanco relacao Itausa |

= 100%.

## Itens fora do quadro de prioridades
- Alba -> politica de gate ativo (Bloco 2)
- Wadhwani -> decisao one-way door pendente com brief reservado (Bloco 3)
- INTEL/sistema -> orcamento de tempo (Bloco 3)

## Bloco 2 — Politicas duraveis (RATIFICADO)

### A — Triagem agressiva
- A1. Cold outreach LinkedIn sem indicacao: CoS responde "sem interesse" educado e arquiva direto. Renato ve lote semanal (domingo briefing).
- A2. Cold outreach de fornecedor / SaaS / agencia / recrutador: CoS responde "sem interesse" sem avisar. Renato nunca ve.

### B — Agenda blindada
- B1. Horarios proibidos: nada antes de 9h, nada depois de 19h. Quarta 15h-18h = bloco estrategico (3h foco profundo em imensIAH / decisao SP-Japao / Wadhwani). CoS recusa qualquer reuniao nesse slot.
- B2. Tamanho define canal: < 30min via WA/voz (nao vai pro calendar); 30-60min calendar com pauta obrigatoria; > 60min com pre-leitura 24h antes.

### C — Pisos de vida (com progressao)
- C1. Saude fisica:
  - Jun-Ago/26: treino 2x/semana, sono >= 7h/noite
  - Set-Dez/26: treino 3x/semana, sono >= 7h/noite, check-up 1x set/26
  - A partir Jan/27: treino 4x/semana, sono >= 7h/noite, mantem check-up anual
- C2. Familia: 1 jantar/semana com cada filho individualmente. 1 atividade/semana so com Emma (nao-domestica). Domingo sagrado, zero trabalho, sistema nao notifica nada.

### D — Gate Alba
- 1 acao proativa/mes ate 30/09/2026 (proposta, reuniao provocada, intro pedida)
- Alerta no briefing dia 25 se nenhuma acao executada no mes
- Sem proposta qualificada ate 30/09 = kill automatico com comunicacao pronta pelo CoS

### E1 — WhatsApp
- Profissionais ativos (Vallen RACI, Despertar, Assespro, conselhos): CoS le + sumariza no briefing 7h + propoe rascunhos. Renato so entra se decisao dele.
- Pessoais/familia: sem filtro. Renato le quando quer.
- Broadcast/marketing/comunidade: arquiva, resumo semanal.
- Zumbis e demais: tudo em bg. CoS so avisa se mention OU "assunto de interesse" (lista abaixo).

### E2 — LinkedIn
- Posts: 2 hot takes + 1 artigo/semana. CoS rascunha tudo na segunda 7h. So Renato publica (CoS nunca posta direto — decisao de marca pessoal).
- Comentarios estrategicos: CoS rascunha usando framework salvo. Renato posta.
- Comentario toxico: zero resposta. Mute & move on. CoS nao avisa.
- Solicitacao de conexao (ICP automatico):
  - Aceita: founder PME / conselheiro / executivo senior / investidor
  - Recusa: recrutador / vendor SaaS / perfis sem 2 grau
  - Lote de aceitos/rejeitados no briefing semanal de domingo

### Filtro de "assunto de interesse" (E1 zumbis + relevancia briefing)
- Frente 1 (imensIAH/Assespro): imensIAH, Assespro, NeoGovernanca, IA aplicada, founder PME, planejamento estrategico, agente AI, governanca nascente
- Frente 2 (ConselhoOS/Wadhwani): ConselhoOS, Wadhwani, conselho consultivo, conselho administracao, governanca corporativa, RACI, ata, Venture Partner, deal flow, board, conselheiro independente
- Frente 3 (Vida): Emma, Emanuele Sakamoto, Renato DAP / Renato Dansieri, Manuela Dansieri, Daniela, Orestes, mudanca SP, interior SP, Japao, escola dos filhos, separacao
- Frente 4 (Almeida Prado/Vallen): Vallen Clinic, Almeida Prado consultoria, RACI Vallen, devolutiva tecnica, Thalita Mendes (lideranca Vallen)
- Frente 5 (Capital relacional): Rodolfo Villela, Itausa, Associacao Despertar, Cecilia Zanotti (lideranca Despertar/Itausa)
- Marca pessoal: estrategia + AI, board governance, IA etica, IA pra PMEs, conselheiro independente, AI agentic

## Bloco 3 — Mandato do CoS digital (RATIFICADO)

### M1 — Agency em comunicacao
- Cold LinkedIn / vendor / recrutador / SaaS: CoS age (responde "sem interesse" + arquiva)
- Cliente pagante (Vallen/Thalita) operacional (horario, status RACI): CoS responde direto
- Cliente pagante (Vallen/Thalita) estrategico: CoS rascunha, Renato ratifica < 4h horario comercial
- Intro pedida a Renato: CoS rascunha, Renato ratifica
- Contato a C0 (familia, Villela, socios): sempre Renato. CoS pode preparar contexto.
- Publicacao LinkedIn: sempre Renato publica
- Imprensa/jornalista: sempre Renato + escalation automatica

### M2 — Agency em agenda
- CoS aceita/recusa reuniao nos slots permitidos (9-19h, fora quarta 15-18h, fora domingo)
- CoS propoe remanejamento < 24h se conflito
- CoS marca com C2+ (network), Renato ratifica no briefing
- CoS NAO marca com C0/C1 (familia, Thalita, Villela, socios) sem confirmar

### M3 — Decisao financeira (conservador)
- < R$ 200: CoS autoriza direto, registra
- R$ 200 a R$ 2.000: CoS recomenda, Renato ratifica em 24h
- > R$ 2.000: Renato decide do zero

### M4 — Agency em sistema
- CoS cria tasks, marca follow-ups, propoe proximas acoes
- CoS arquiva/snooze ruido (com log auditavel)
- CoS NAO move decisoes one-way door (SP/Japao, Wadhwani) sem Renato
- CoS NAO altera contatos C0/C1 sem confirmar

### M5 — Orcamento INTEL/sistema
- Cap 4h/semana de Renato em melhorias
- Feature nova exige ROI declarado em "minutos seus economizados/semana"
- Sem ROI mensuravel em 30 dias = candidata a kill

### M6 — Decisoes pendentes com brief reservado
- Wadhwani: 90min agendado num slot estrategico de quarta nas proximas 2 semanas. Outputs: sim/nao, criterio deal flow, ponto de saida se 6 meses sem conversao.
- Mudanca SP/Japao: brief cumulativo ~3h ate set/26, vetor por opcao (filhos, Emma, fiscal/imigracao, base cliente, qualidade vida).

### M7 — Escalation triggers (override total)
- Familia (filhos, Emma, Daniela, Orestes)
- Emergencia saude
- Imprensa/midia (qualquer jornalista)
- Cliente pagante (Vallen/Thalita) com sinal de churn ou problema grave
- Conselho oficial com problema grave
- Solicitacao direta de Villela ou pessoa C0

## Notas operacionais
- Briefing 7h (na pratica 08h BRT): formato CoS opinativo, ordenado por peso v5, 1 linha de confronto obrigatoria, fecho com numeros chave.
- Atualizacoes do CoS config: novo INSERT (mantem historico). `get_active_cos_config` retorna o mais recente.
"""


def main() -> int:
    titulo = "CoS Config v5 — Renato (jun/2026)"
    tags = ["cos", "config", "trio", "prioridades", "politicas", "mandato", "v5"]

    existing = get_active_cos_config()
    if existing:
        print(f"[INFO] CoS config existente: id={existing['id']}, criado_em={existing['criado_em']}")
        print(f"[INFO] Vou inserir nova versao (historico preservado).")

    new_id = save_system_memory(
        titulo=titulo,
        conteudo=COS_CONFIG_CONTENT,
        tipo="cos_config",
        tags=tags,
        fonte="cos_extraction_session",
    )
    if new_id:
        print(f"[OK] CoS config salvo: id={new_id}")
        print(f"[OK] Tamanho conteudo: {len(COS_CONFIG_CONTENT)} chars")
        return 0
    print("[ERROR] save_system_memory retornou None — checar logs")
    return 1


if __name__ == "__main__":
    sys.exit(main())
