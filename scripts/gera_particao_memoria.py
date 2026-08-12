#!/usr/bin/env python3
"""Gera a proposta de partição do MEMORY.md em 2 níveis (retro 14/08).

Nível 1 = alarme passivo: entra em TODA sessão porque evita erro que eu cometeria
sem perceber. Nível 2 = consulta sob demanda: eu já sei o que procuro.
Teste: "se eu não abrir isto, eu erro sem perceber?"

O ônus da prova é para FICAR no nível 1 — a lista abaixo é explícita, e tudo que
não está nela desce por default.
"""
import html, os, re, collections

MEM = "/Users/rap/.claude/projects/-Users-rap-prospect-system/memory"
INDEX = os.path.join(MEM, "MEMORY.md")
OUT = os.path.expanduser("~/cockpit/memoria_particao.html")

N1 = {
 "Identidade, voz e tratamento": (
   "Erro que passa liso: o texto sai plausível e errado, e ninguém revisa a forma de tratamento.", [
   "user_role","project_posicionamento_desenha_a_saida","feedback_nome_completo_formal",
   "feedback_voice_cortesia","feedback_lingua_portugues_correto","feedback_voice_renato_mensagens",
   "feedback_content_voice","project_voice_policy","reference_david_panico_tupa",
   "reference_oaap_sigla","reference_obme","feedback_catapora_idiom"]),

 "Anti-alucinação e verificar antes de afirmar": (
   "O modo de falha é afirmar com confiança. Se a regra não está no contexto, ela não age.", [
   "feedback_anti_hallucination","feedback_verify_before_relay","feedback_nao_relatar_rascunho_como_enviado",
   "feedback_camada_cega_a_email_planilha_pje","feedback_meia_frase_que_confirma",
   "feedback_procedencia_em_peca_assinada","feedback_timeline_triage",
   "feedback_checar_acao_renato_antes_de_oferecer","feedback_verificar_dia_semana",
   "feedback_checar_agenda_antes_propor","feedback_nao_inferir_compromisso",
   "feedback_plano_condicional_nao_ratificado","feedback_inbound_ancora_nao_e_a_mensagem_acionavel",
   "feedback_wa_single_message_blindness","feedback_cos_action_blindness"]),

 "Como trabalhar com você (CoS)": (
   "Define o que faço sozinho, o que te trago e em que formato. Sem isso eu te viro tarefa.", [
   "feedback_nao_perguntar_age","feedback_cos_autonomy_policy","feedback_no_demolition_learn_by_use",
   "feedback_ferramenta_nao_vira_tarefa_do_renato","feedback_toque_entrega_nao_cobra",
   "feedback_no_cold_outreach","feedback_contato_precisa_motivo","feedback_entrega_visual_html_local",
   "reference_cockpit_local_page","feedback_notifications","feedback_notificacao_valor_medido",
   "feedback_superficie_nova_mata_o_aviso","feedback_cockpit_regra","feedback_task_state_drift",
   "feedback_passive_labeling_over_manual","feedback_markdown_blockquote_copy",
   "feedback_wa_silencio_produtor","feedback_cross_group_before_send",
   "feedback_consultar_cargo_antes_classificar","feedback_no_hardcoded_contact_ids",
   "feedback_aguardar_terceiro_on_hold","feedback_super_conector_playbook",
   "feedback_pro_bono_apartidaria_ano_eleitoral","reference_true_health_group",
   "feedback_tonha_routing_andressa","feedback_tonha_silence_rules"]),

 "Método e medição": (
   "O padrão de erro que mais se repetiu aqui. Hoje mesmo seis destes mudaram a frente.", [
   "feedback_medir_antes_de_construir","feedback_medidor_que_nao_mede_a_si_mesmo",
   "feedback_mecanismo_que_nao_mede_o_denominador","feedback_regua_cobertura_parcial",
   "feedback_otimizar_o_mensuravel_erra_o_alvo","feedback_controle_positivo_pega_o_furo_real",
   "feedback_consumidor_morto_wiring","feedback_consumidor_externo_invisivel_ao_grep",
   "feedback_filtro_vocabulario_errado_falha_calado","feedback_guarda_abstencao_vira_fabrica",
   "feedback_maquina_ouve_o_proprio_eco","feedback_prompt_nao_le_comentario",
   "feedback_estado_local_morre_no_sync_externo"]),

 "Armadilha técnica — falha silenciosa ou destrutiva": (
   "Erram calado ou apagam dado. Descobrir depois custa caro; algumas são irreversíveis.", [
   "feedback_hora_do_banco_e_utc","feedback_calendar_events_tz","feedback_query_tz_brt",
   "project_tz_consistency","reference_railway_deploy_intel_api","reference_railway_projects",
   "feedback_railway_switchover_route_probe","reference_db_target_protocol","feedback_parallel_sessions",
   "session_locks","feedback_amend_em_sessao_paralela","feedback_cascade_chain_warning",
   "reference_contact_merge_fix","reference_telefone_br_normalizacao",
   "feedback_audit_link_tables_antes_de_bulk","feedback_no_multiuser_intel","feedback_modal_transparency",
   "feedback_env_var_whitespace","feedback_import_sombreado_unboundlocal",
   "feedback_intel_conselhoos_sync_lacuna","feedback_neon_source_of_truth_proposals",
   "feedback_dev_db_cron_runs_stale","reference_voyage_rate_limits",
   "feedback_ler_extracted_text_anexos_wa","feedback_evolution_restart","feedback_worker_vercel_url",
   "feedback_memoria_com_teto","feedback_memoria_identidade_por_arquivo","reference_memory_architecture",
   "feedback_dev_isolation","feedback_db_sync_scope","feedback_dup_check_webhook_vs_manual",
   "reference_wa_envio_gravacao_messages","reference_whatsapp_lid_migration",
   "feedback_anthropic_admin_api_units"]),
}

texto = open(INDEX, encoding="utf-8").read()
# custo real de cada slug: bytes do proprio link + fatia do hook da linha que o hospeda
custo, tema_de = {}, {}
for linha in texto.split("\n"):
    if not linha.strip().startswith("-"):
        continue
    achados = re.findall(r'(\[[^\]]*\]\(([a-zA-Z0-9_]+)\.md\))', linha)
    if not achados:
        continue
    bytes_links = sum(len(m.encode()) for m, _ in achados)
    resto = len(linha.encode()) - bytes_links
    tema = (linha.split("—")[0].lstrip("- ").strip() or "—")[:38]
    tema = re.sub(r'\[|\]|\(.*?\)|\*', '', tema).strip() or "—"
    for markup, slug in achados:
        custo[slug] = len(markup.encode()) + resto / len(achados)
        tema_de[slug] = tema

n1_slugs = {s for _, (_, ss) in N1.items() for s in ss}
todos = set(custo)
ausentes = sorted(n1_slugs - todos)          # nivel 1 que nao esta no indice hoje
n2 = sorted(todos - n1_slugs, key=lambda s: (tema_de[s], s))

kb1 = sum(custo.get(s, 0) for s in n1_slugs if s in todos) / 1024
kb2 = sum(custo[s] for s in n2) / 1024
total_kb = len(texto.encode()) / 1024

grupos2 = collections.OrderedDict()
for s in n2:
    grupos2.setdefault(tema_de[s], []).append(s)

def chk(slug, extra=""):
    return (f'<label class="it{extra}"><input type=checkbox checked>'
            f'<code>{html.escape(slug)}</code></label>')

secs = []
for titulo, (porque, slugs) in N1.items():
    presentes = [s for s in slugs if s in todos]
    kb = sum(custo[s] for s in presentes) / 1024
    itens = "".join(chk(s) for s in slugs)
    secs.append(f'<section><h3>{html.escape(titulo)} '
                f'<span class=n>{len(slugs)} · ~{kb:.1f} KB</span></h3>'
                f'<p class=pq>{html.escape(porque)}</p><div class=grid>{itens}</div></section>')

secs2 = []
for tema, slugs in grupos2.items():
    itens = "".join(f'<code>{html.escape(s)}</code>' for s in slugs)
    secs2.append(f'<div class=t2><b>{html.escape(tema)}</b> <span class=n>{len(slugs)}</span>'
                 f'<div class=g2>{itens}</div></div>')

nota_ausentes = ""
if ausentes:
    nota_ausentes = ('<p class=warn>⚠️ No nível 1 mas <b>fora do índice hoje</b> (entram como linha nova): '
                     + ", ".join(f"<code>{html.escape(s)}</code>" for s in ausentes) + "</p>")

doc = f"""<!doctype html><meta charset=utf-8>
<title>Partição da memória — proposta para a retro de 14/08</title>
<style>
:root{{--ink:#2b2b2b;--soft:#6b6b6b;--gold:#b8935a;--line:#e6e0d4;--bg:#faf7f1}}
*{{box-sizing:border-box}}
body{{font:15px/1.55 -apple-system,'Avenir Next',Segoe UI,sans-serif;color:var(--ink);
background:var(--bg);margin:0;padding:32px 36px;max-width:1080px}}
h1{{font:600 26px/1.2 'Playfair Display',Georgia,serif;margin:0 0 6px}}
h2{{font:600 18px/1.3 'Playfair Display',Georgia,serif;margin:34px 0 6px;
border-bottom:2px solid var(--gold);padding-bottom:6px}}
h3{{font-size:15px;margin:20px 0 2px}}
.sub{{color:var(--soft);margin:0 0 22px}}
.cards{{display:flex;gap:12px;flex-wrap:wrap;margin:0 0 14px}}
.card{{background:#fff;border:1px solid var(--line);border-radius:10px;padding:12px 16px;min-width:132px}}
.card b{{font-size:25px;display:block;color:var(--gold);line-height:1.1}}
.card span{{color:var(--soft);font-size:12px}}
.teste{{background:#fff;border-left:3px solid var(--gold);padding:12px 16px;margin:18px 0;border-radius:0 8px 8px 0}}
.pq{{color:var(--soft);font-size:13px;margin:2px 0 8px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(305px,1fr));gap:2px}}
.it{{display:flex;gap:7px;align-items:baseline;padding:3px 6px;border-radius:5px}}
.it:hover{{background:#fff}}
code{{font:12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;color:#4a4a4a;word-break:break-all}}
.n{{color:var(--soft);font-weight:400;font-size:12px}}
.t2{{background:#fff;border:1px solid var(--line);border-radius:9px;padding:9px 13px;margin:7px 0}}
.g2{{display:flex;flex-wrap:wrap;gap:4px 12px;margin-top:5px;opacity:.72}}
.warn{{background:#fdf4ef;border:1px solid #e8d5c4;border-radius:8px;padding:10px 14px;font-size:13px}}
.foot{{color:var(--soft);font-size:12.5px;margin-top:30px;border-top:1px solid var(--line);padding-top:14px}}
@media print{{.it:hover{{background:none}}}}
</style>

<h1>Partição da memória em dois níveis</h1>
<p class=sub>Proposta para a retro de sexta, 14/08 · o índice hoje tem <b>{len(todos)}</b> entradas e
<b>{total_kb:.1f} KB</b> de um teto de 24</p>

<div class=cards>
<div class=card><b>{len(n1_slugs)}</b><span>ficam no nível 1</span></div>
<div class=card><b>~{kb1:.1f} KB</b><span>índice sempre carregado</span></div>
<div class=card><b>{len(n2)}</b><span>descem pro nível 2</span></div>
<div class=card><b>~{kb2:.1f} KB</b><span>saem do carregamento fixo</span></div>
<div class=card><b>{(1-kb1/total_kb)*100:.0f}%</b><span>de folga recuperada</span></div>
</div>

<div class=teste>
<b>O teste, item a item:</b> “se eu não abrir isto, eu erro sem perceber?”<br>
<b>Sim</b> → nível 1: precisa estar no contexto <i>antes</i> de eu saber que preciso dele.
<b>Não</b> → nível 2: quando o assunto aparece, eu já sei o que procurar — e o recall semântico alcança.
<br><br>
A partição <b>não é por tema</b>. Um dossiê da Vallen é consultado; “Alexandre &gt; Ismar &gt; nunca direto
Rafael” é um alarme — some do nível 1 e eu escrevo pro Rafael sem nunca ter sabido que havia regra.
<b>O ônus da prova é para ficar:</b> tudo que não está listado abaixo desce por default.
</div>

<h2>Nível 1 — alarme passivo ({len(n1_slugs)} entradas, ~{kb1:.1f} KB)</h2>
<p class=sub>Desmarque o que você acha que não precisa estar sempre carregado.</p>
{nota_ausentes}
{''.join(secs)}

<h2>Nível 2 — consulta sob demanda ({len(n2)} entradas, ~{kb2:.1f} KB)</h2>
<p class=sub>Continuam existindo, buscáveis e linkáveis — só deixam de ocupar todas as sessões.
Agrupados pelo tema da linha atual do índice.</p>
{''.join(secs2)}

<p class=foot>
<b>O que este número não decide.</b> A telemetria de leitura que subiu hoje mede quais memos são de fato
abertos — e tem um viés que precisa ficar dito: memória cujo hook no índice já basta
(<code>feedback_voice_cortesia</code> entrega “grato, nunca obrigado” na própria linha) marca
<b>zero leituras para sempre</b> e está funcionando. Por isso o nível 1 se decide por julgamento seu,
agora; o nível 2 se afina com dado em 2–3 semanas.<br><br>
<b>Custo por entrada:</b> ~53 B de link (33 só de nome de arquivo) + ~25 B de hook. É por isso que cortar
prosa rendeu 5% e mover entradas rende 60%: o índice cresce por memória, não por texto.
</p>
"""
os.makedirs(os.path.dirname(OUT), exist_ok=True)
open(OUT, "w", encoding="utf-8").write(doc)
print(f"nível 1: {len(n1_slugs)} entradas · ~{kb1:.1f} KB")
print(f"nível 2: {len(n2)} entradas · ~{kb2:.1f} KB")
print(f"total hoje: {len(todos)} entradas · {total_kb:.1f} KB")
if ausentes:
    print(f"⚠️ no nível 1 mas fora do índice: {ausentes}")
print(f"→ {OUT}")
