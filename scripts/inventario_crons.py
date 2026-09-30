"""INVENTÁRIO DOS 54 CRONS — efeito real, lido do result_json.

Por que não `rows_affected`: ele é NULL em 42 dos 54 endpoints. O
`task-reconciler` aparece com 0 e fechou 12 tasks em 14 dias — o campo mede
instrumentação, não efeito, e usá-lo como critério de corte mataria o maior
produtor de fechamento do sistema ([[feedback_medidor_que_nao_mede_a_si_mesmo]]).

`result_json` está presente em 17.638 de 17.639 execuções. Cada endpoint devolve
seus próprios contadores; somando 14 dias por chave, "zero em toda a janela" é
uma afirmação sobre o EFEITO, não sobre a instrumentação.

⚠️ A distinção que decide o veredito: para um MONITOR, efeito zero é sucesso
(não alertou porque não havia o que alertar). Para um PRODUTOR, efeito zero é
morte. Os dois baldes saem separados de propósito.
"""
import sys, os, re, json, collections
sys.path.insert(0, os.path.join(os.getcwd(), 'app'))
from database import get_db

D = 14

# chaves que NÃO são efeito — parâmetros, totais de referência, relógios
RUIDO = {
    'limit', 'offset', 'next_offset', 'total_chats', 'total', 'quieto_min',
    'duration_ms', 'elapsed', 'window', 'window_days', 'stale_days', 'days',
    'batch', 'batch_size', 'page', 'pages', 'max', 'threshold', 'cap',
    'avaliados', 'candidatos', 'examined', 'scanned', 'checked', 'pending',
}
# erros contam à parte: não são "efeito útil", mas importam
ERRO_KEYS = {'errors', 'failed', 'erros', 'falhas', 'error_count'}

MONITOR = re.compile(r'health|heartbeat|monitor|canary|reminder|alerta')

src = open('workers/audio-transcriber/main.py').read()
declarados = {}
for m in re.finditer(r'^\s{4}\("([\w-]+)",\s*"(/api/cron/[^"]+)"', src, re.M):
    declarados[m.group(1)] = m.group(2).split('?')[0]
for m in re.finditer(r'^\s{4}\("([\w-]+)",\s*"(/api/cron/[^"]+)",\s*$', src, re.M):
    declarados.setdefault(m.group(1), m.group(2).split('?')[0])

def walk(o, out, pref=''):
    """soma recursiva das chaves numéricas; listas contam pelo tamanho"""
    if isinstance(o, dict):
        for k, v in o.items():
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                out[k] += v
            elif isinstance(v, list):
                out[k] += len(v)
            elif isinstance(v, dict):
                walk(v, out, pref)

with get_db() as conn:
    cur = conn.cursor()
    cur.execute("""
        SELECT split_part(path,'?',1) p, result_json, status, duration_ms
        FROM cron_runs WHERE started_at > NOW() - INTERVAL '%s days'
    """, (D,))
    agg = collections.defaultdict(lambda: {
        'n': 0, 'falhas': 0, 'ms': 0, 'keys': collections.Counter()})
    for r in cur.fetchall():
        a = agg[r['p']]
        a['n'] += 1
        a['ms'] += (r['duration_ms'] or 0)
        if r['status'] not in ('ok', 'success'):
            a['falhas'] += 1
        if r['result_json']:
            walk(r['result_json'], a['keys'])

print(f"╔═ INVENTÁRIO — {len(declarados)} crons ativos · {D} dias · efeito por result_json ═╗\n")

baldes = collections.defaultdict(list)
for nome, path in sorted(declarados.items()):
    a = agg.get(path)
    if not a or a['n'] == 0:
        baldes['sem_execucao'].append((nome, None, {}, 0, 0)); continue
    efeito = {k: v for k, v in a['keys'].items()
              if v and k not in RUIDO and k not in ERRO_KEYS}
    erros = sum(v for k, v in a['keys'].items() if k in ERRO_KEYS)
    min_tot = a['ms'] / 60000.0
    tup = (nome, a['n'], efeito, erros, min_tot)
    if a['falhas'] == a['n']:
        baldes['falha_total'].append(tup)
    elif MONITOR.search(nome):
        baldes['monitor'].append(tup)
    elif not efeito:
        baldes['zero'].append(tup)
    else:
        baldes['produz'].append(tup)

def linha(t):
    nome, n, efeito, erros, mins = t
    top = ' '.join(f"{k}={int(v)}" for k, v in
                   sorted(efeito.items(), key=lambda x: -x[1])[:4]) or '—'
    e = f" ⚠️{erros}err" if erros else ""
    nn = f"{n}x" if n else "0x"
    return f"   {nome:34} {nn:>7} {mins:6.0f}min  {top[:56]}{e}"

def bloco(titulo, key, nota):
    it = baldes[key]
    print(f"── {titulo} ({len(it)}) ──\n   {nota}")
    for t in sorted(it, key=lambda x: -(x[4] or 0)):
        print(linha(t))
    print()

bloco("🔴 SEM EXECUÇÃO NA JANELA", 'sem_execucao',
      "declarado e nunca rodou (⚠️ cron mensal/semanal pode ser falso positivo — conferir o trigger)")
bloco("🔴 FALHA EM 100% DAS EXECUÇÕES", 'falha_total',
      "gasta slot e nunca entrega")
bloco("🟠 PRODUTOR COM EFEITO ZERO EM 14 DIAS", 'zero',
      "rodou e nenhum contador de efeito saiu de zero — candidato a corte")
bloco("🔵 MONITOR (zero = funcionando, não morto)", 'monitor',
      "não alertar é o sucesso dele; o custo é slot+duração, não efeito ausente")
bloco("🟢 PRODUZ EFEITO MEDIDO", 'produz',
      "estes têm produção; a pergunta que sobra é se alguém CONSOME")

print("── CUSTO DE SLOT (top 10 por minutos de compute em 14d) ──")
todos = [t for k in baldes for t in baldes[k] if t[1]]
for t in sorted(todos, key=lambda x: -(x[4] or 0))[:10]:
    print(linha(t))

print(f"\n── RESUMO ──")
for k, lbl in [('sem_execucao','sem execução'), ('falha_total','falha total'),
               ('zero','produtor efeito zero'), ('monitor','monitor'), ('produz','produz')]:
    print(f"   {lbl:24} {len(baldes[k]):3}")
print(f"   {'TOTAL':24} {sum(len(v) for v in baldes.values()):3}")
