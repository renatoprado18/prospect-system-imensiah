# Dossiê — Relação de CDAs · Regularização das 7 empresas (Reorg #47)
**Para:** Priscila (LENA Contábil) · **cc** Dr. João Piccino · Andressa
**De:** Renato de Faria e Almeida Prado
**Data:** 25/07/2026
**Assunto:** insumo para a "relação de CDAs de todos os CNPJs + Renato PF" (pedido do Piccino em 25/07)

> **Status honesto:** a parte **estadual (PGE-SP)** foi levantada hoje na fonte oficial e está **completa**. A parte **federal (PGFN/União)** — que é onde está o passivo — só pôde ser estimada hoje (Regularize fica fechado no fim de semana); o **levantamento federal oficial, CDA a CDA, sai na segunda** (roteiro delegado à Andressa). Este documento serve pra Priscila **começar a organizar já**.

---

## As 7 empresas + Renato PF

| Empresa | CNPJ |
|---|---|
| Carambola Tecnologia Ltda | 19.297.737/0001-46 |
| AB Estacionamentos | 30.712.151/0001-33 |
| Pare.Net | 10.731.821/0001-51 |
| Starfruit | 28.354.577/0001-10 |
| Makerfest | 24.553.387/0001-71 |
| Framboesa | 33.412.797/0001-93 |
| Empreseira | 03.277.284/0001-56 |
| Renato de Faria e Almeida Prado (PF) | 257.504.788-90 |

---

## PARTE 1 — Dívida Ativa ESTADUAL (PGE-SP) ✅ levantada hoje (fonte oficial)

Consulta feita em 25/07/2026 no *Site do Contribuinte* da PGE-SP (Consulta de Débitos Inscritos na Dívida Ativa), por CNPJ/CPF:

| Entidade | Dívida ativa estadual |
|---|---|
| **Carambola** | **1 CDA — Taxa Judiciária: R$ 238,78** (CDA nº 1425231130, origem TJ, referência 04/07/2025, inscrita 08/07/2025) |
| AB, Pare.Net, Starfruit, Makerfest, Framboesa, Empreseira | **Nenhum débito** |
| Renato PF (CPF) | **Nenhum débito** |

**Leitura:** no plano estadual o grupo está limpo (só uma taxa judicial trivial na Carambola). **Todo o passivo relevante é FEDERAL.**

---

## PARTE 2 — Dívida Ativa FEDERAL (PGFN/União) ⚠️ PRELIMINAR (confirmar na fonte 2ª feira)

Números **agregados** que já tínhamos mapeados (não é a relação CDA-a-CDA — é o retrato de tamanho por empresa, a ser confirmado no Regularize/e-CAC):

| Empresa | Dívida ativa federal (aprox.) | Composição |
|---|---|---|
| **Carambola** | **~R$ 2,12 M** | Simples ~R$1,27M · **Previdenciário ~R$706k** · demais ~R$146k |
| **AB Estacionamentos** | ~R$ 440 k | a detalhar |
| **Pare.Net** | ~R$ 52 k | a detalhar |
| **Starfruit** | ~R$ 9,5 k | a detalhar |
| **Makerfest** | *a confirmar* | verificar se **parcelada ou quitada** |
| **Framboesa** | *a confirmar* | verificar se **parcelada ou quitada** |
| **Empreseira** | *a confirmar* | verificar se **parcelada ou quitada** — ⚠️ é HOLDING, manter descolada da holding nova |
| **Renato PF** | *a levantar* | extrato PGFN + e-CAC do CPF |

---

## PARTE 3 — Template do levantamento CDA-a-CDA (o que preencher por inscrição)

Para cada CDA de cada CNPJ + do CPF, capturar:

| Campo | Fonte |
|---|---|
| Nº da inscrição / CDA | Regularize (PGFN) · extrato de inscrições |
| Tributo / natureza (Simples, Previdenciário, etc.) | Regularize / e-CAC |
| Valor consolidado | Regularize / e-CAC |
| **Data de inscrição em Dívida Ativa** | Regularize — **crítico p/ prescrição** |
| Situação: ajuizada / a ajuizar / parcelada / suspensa | Regularize |
| Nº do processo de execução fiscal + vara/foro (se ajuizada) | Regularize / Eproc-EF TJSP / Justiça Federal |
| Último ato da Fazenda + data · suspensão art. 40 LEF (desde quando) | processo de execução |

---

## PARTE 4 — Alertas estratégicos (ler antes de decidir qualquer coisa)

1. **Datas > valores.** A relação não pode ser só "quanto se deve" — sem a **data de inscrição/citação** de cada CDA não dá pra calcular quão perto está da **prescrição intercorrente** (art. 40 LEF), e sem isso qualquer decisão de *prescrever × transacionar × baixar* é chute.
2. **Transacionar/parcelar = confissão que REINICIA o prazo** (art. 174 §ún. IV CTN, Súmula 653 STJ) — só vale onde o desconto supera o valor de deixar prescrever. Foi o SISPAR descumprido da Carambola que reiniciou o relógio.
3. **O risco real é PESSOAL (Renato PF), não a empresa** (que tem PL ~zero): dissolução irregular presumida → **redirecionamento ao sócio-administrador (Súmula 435)**. Por isso encerrar **formalmente** (comunicando órgãos, distrato na Junta) protege; encerrar mal (shell parado) expõe.
4. **Jaboticabeiras (café) e a holding nova NÃO se contaminam** — nunca garantidora, sucessora, destino de ativos ou sócia comum das empresas que estão sendo encerradas.

---

*Documento de trabalho — a Parte 2 vira definitiva quando o levantamento federal (2ª feira) substituir os valores preliminares pela relação CDA-a-CDA da fonte.*
