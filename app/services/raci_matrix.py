"""
RACI genérico — a matriz de um projeto, venha ela de onde vier.

O QUE ISTO RESOLVE (pedido do Renato 28/07/26, task #999703)
------------------------------------------------------------
Ver o RACI de QUALQUER projeto no INTEL e imprimir em PDF on-brand num
clique, pra compartilhar com quem não tem acesso ao sistema.

Antes disto, "RACI" era duas coisas incompatíveis:

  - no ConselhoOS (outro Neon), `raci_itens` estruturado — mas só existe pra
    empresa de conselho. A reorg das 7 empresas (#47) não é conselho, e é
    justamente onde o Renato precisa mandar a matriz pro Piccino e pra
    Priscila;
  - no INTEL, RACI como TEXTO dentro de uma `project_note` (a #268 do #47) —
    legível por humano, inútil pra máquina: não ordena por prazo, não filtra
    por status, não imprime.

A ESCOLHA DE ARQUITETURA: UNIR NA LEITURA, NÃO SINCRONIZAR
----------------------------------------------------------
São dois Neons sem sync ([[feedback_intel_conselhoos_sync_lacuna]]). A
tentação óbvia — copiar o RACI do ConselhoOS pra dentro do INTEL — criaria a
terceira cópia do mesmo fato, e cópia que não se reconcilia é exatamente a
classe de defeito que já mordeu aqui (a nota #302 que sobrevive contradizendo
o memo certo). Então cada fonte é lida NA FONTE, em cada requisição, e a
união só existe em memória.

ESCRITA: NA FONTE TAMBÉM (write-through, 29/07)
------------------------------------------------
A primeira versão era read-only em relação ao ConselhoOS. Durou um dia: o
caso real que motivou a tela — atualizar o RACI da Vallen depois da reunião —
esbarrava em 57 itens dos quais ZERO eram editáveis, porque todos vêm do
ConselhoOS. Uma matriz que mostra tudo e deixa mexer em nada não serve pro
momento em que ela é usada.

Renato decidiu abrir a escrita (29/07). O que NÃO muda é a regra de cópia:
editar aqui grava NA FONTE — item de conselho é `UPDATE` no ConselhoOS, item
do INTEL é `UPDATE` no INTEL. Nada é espelhado, então não nasce a terceira
cópia. Precedente já existia: `conselhoos_raci_sync` escreve lá desde sempre
quando a task INTEL fecha.

O que a escrita cross-DB exige de cuidado (o schema dos dois lados NÃO é o
mesmo — ver `_update_conselhoos`):
  - `area`, `acao`, `prazo` e `status` são NOT NULL no ConselhoOS; no INTEL
    `prazo` é opcional. Apagar prazo de item de conselho é rejeitado com
    mensagem, não com erro 500 do banco;
  - `status` é ENUM (`raci_status`) lá e CHECK aqui — valor inválido é
    barrado antes do INSERT, dos dois lados;
  - `concluido_relatado_em` NÃO é tocado ao concluir. É o campo que o
    `raci_weekly_report` usa pra saber o que ainda não foi anunciado no
    grupo; preenchê-lo aqui faria a conclusão nascer "já relatada" e sumir
    do relatório sem nunca ter sido dita.

DELETE segue INTEL-only. Apagar linha de RACI de conselho é destruir registro
de ata de uma empresa que não é minha, por um caminho que não é o dela — e
ninguém pediu isso.

COMO UM PROJETO ACHA O RACI DE CONSELHO DELE
---------------------------------------------
`projects.empresa_id` -> `empresas.conselhoos_empresa_id` -> empresa do
ConselhoOS (migration 056). O elo projeto->empresa não existia; `projects`
só tinha `empresa_relacionada`, TEXT livre preenchido em 5 de 28 projetos
ativos e com grafias que não casam com `empresas.nome_canonico`.

Projeto sem `empresa_id` (a maioria) simplesmente não tem fonte-conselho: a
matriz é só a do INTEL, e isso não é erro nem estado degradado.
"""
import logging
import os
import re
import unicodedata
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from database import get_db

logger = logging.getLogger(__name__)


# Os 4 status são os mesmos dos dois lados (enum `raci_status` no ConselhoOS,
# CHECK na tabela do INTEL). A ordem aqui é a de exibição.
STATUS_ORDER = ["atrasado", "pendente", "em_andamento", "concluido"]

STATUS_LABEL = {
    "atrasado": "Atrasado",
    "pendente": "Pendente",
    "em_andamento": "Em andamento",
    "concluido": "Concluído",
}

FONTE_INTEL = "intel"
FONTE_CONSELHOOS = "conselhoos"


def _conselhoos_url() -> str:
    """URL do ConselhoOS lida em CALL-TIME + strip().

    Mesmo motivo de `raci_smart_updates._conselhoos_url`: a Vercel cola '\\n'
    no valor ([[feedback_env_var_whitespace]]) e uma constante de módulo lida
    no import fica vazia pra quem seta a env depois. Ler na constante já
    custou um diagnóstico falso ("item não encontrado no RACI" quando quem
    faltava era a conexão).
    """
    return (os.getenv("CONSELHOOS_DATABASE_URL") or "").strip()


def _as_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _normalize(row: Dict, fonte: str) -> Dict:
    """
    Uma linha de qualquer fonte no MESMO formato.

    `status_efetivo` é derivado, não lido: um item pendente cujo prazo passou
    é `atrasado` para quem lê, mesmo que a coluna diga `pendente`. Derivar na
    leitura (em vez de um cron que reescreve status) evita que a matriz
    dependa de um job ter rodado — e o board já registra o custo de status
    que envelhece sem ninguém mexer.

    Item SEM prazo nunca vira atrasado. É o caso das responsabilidades
    permanentes ("coordenação da cadência"), que não vencem; marcá-las de
    vermelho seria falso-atrasado, o que corroeu a credibilidade do RACI do
    Vallen em 13/07.
    """
    prazo = _as_date(row.get("prazo"))
    status = (row.get("status") or "pendente").strip()

    status_efetivo = status
    if status != "concluido" and prazo and prazo < date.today():
        status_efetivo = "atrasado"

    dias = (prazo - date.today()).days if prazo else None

    # Quando fechou (073). `concluido_em_fonte` distingue o que foi carimbado no
    # ato do que veio do backfill por relato — que é limite superior, não a data
    # exata. Quem consome a métrica precisa poder separar os dois.
    concluido_em = row.get("concluido_em")
    concluido_dia = concluido_em.date() if isinstance(concluido_em, datetime) else _as_date(concluido_em)

    return {
        "fonte": fonte,
        "id": row.get("id"),
        "uid": f"{fonte}:{row.get('id')}",
        "concluido_em": concluido_dia.isoformat() if concluido_dia else None,
        "concluido_em_br": concluido_dia.strftime("%d/%m/%Y") if concluido_dia else None,
        "concluido_em_fonte": row.get("concluido_em_fonte"),
        # No prazo só é resposta quando existem AS DUAS datas. Sem isso seria
        # `False` por ausência de dado, que é o jeito silencioso de transformar
        # item não medido em item atrasado.
        "concluido_no_prazo": (concluido_dia <= prazo) if (concluido_dia and prazo) else None,
        "area": (row.get("area") or "").strip() or None,
        "acao": (row.get("acao") or "").strip(),
        "r": (row.get("responsavel_r") or "").strip() or None,
        "a": (row.get("responsavel_a") or "").strip() or None,
        "c": (row.get("responsavel_c") or "").strip() or None,
        "i": (row.get("responsavel_i") or "").strip() or None,
        "prazo": prazo.isoformat() if prazo else None,
        "prazo_br": prazo.strftime("%d/%m/%Y") if prazo else None,
        "dias_para_prazo": dias,
        "status": status,
        "status_efetivo": status_efetivo,
        "status_label": STATUS_LABEL.get(status_efetivo, status_efetivo),
        "notas": (row.get("notas") or "").strip() or None,
        "task_id": row.get("task_id"),
        # O ponteiro para a linha de ata que originou esta execução (084). Tem de
        # sobreviver à normalização porque é ele que distingue "a mesma coisa
        # declarada nos dois lugares" de "duplicata" — sem ele, a detecção acusa
        # como duplicado todo item importado, que por construção tem o texto
        # idêntico ao da sua própria ata.
        "conselhoos_raci_id": (str(row["conselhoos_raci_id"])
                               if row.get("conselhoos_raci_id") else None),
        # Editar vale nas duas fontes (write-through, 29/07). Remover, não:
        # `removivel` é o que separa mexer numa linha de destruí-la.
        "editavel": True,
        "removivel": fonte == FONTE_INTEL,
        # O ConselhoOS não aceita item sem prazo (coluna NOT NULL). A tela usa
        # isto pra avisar ANTES, em vez de deixar o usuário limpar o campo e
        # descobrir no erro que aquilo nunca foi possível.
        "prazo_obrigatorio": fonte == FONTE_CONSELHOOS,
    }


def _fetch_intel(cursor, project_id: int) -> List[Dict]:
    cursor.execute("""
        SELECT id, area, acao, responsavel_r, responsavel_a, responsavel_c,
               responsavel_i, prazo, status, notas, task_id,
               concluido_em, concluido_em_fonte, conselhoos_raci_id
          FROM raci_itens
         WHERE project_id = %s
    """, (project_id,))
    return [_normalize(dict(r), FONTE_INTEL) for r in cursor.fetchall()]


def _fetch_conselhoos(empresa_uuid: str) -> List[Dict]:
    """
    Itens do ConselhoOS pra uma empresa. READ-ONLY.

    Falha graciosa de propósito: sem a env, com o outro Neon fora do ar ou
    com a tabela ausente, devolve [] e a matriz mostra só o lado INTEL. A
    página não pode cair por causa de um banco que nem é o dela — mas quem
    chama recebe o aviso por `_fetch_conselhoos_status` pra poder dizer na
    tela que a fonte está incompleta, em vez de mentir um RACI menor.
    """
    itens, _ = _fetch_conselhoos_status(empresa_uuid)
    return itens


def _fetch_conselhoos_status(empresa_uuid: str):
    """Devolve (itens, erro_ou_None)."""
    url = _conselhoos_url()
    if not url:
        return [], "CONSELHOOS_DATABASE_URL não configurada"

    try:
        import psycopg2
        import psycopg2.extras

        conn = psycopg2.connect(url, connect_timeout=5)
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            # `intel_task_id AS task_id` (05/10/26) — o elo EXISTIA e era jogado
            # fora aqui. O board registrava "o elo `intel_task_id` existe e está
            # vazio"; medido hoje em prod ele está **32% preenchido** (39 de 122;
            # 35 de 80 na Vallen). Vazio estava o lado INTEL (0 de 25 no projeto
            # 24) — e esta query nem chegava a ler o lado cheio, então o elo não
            # alcançava a matriz nem para ser medido. `_normalize` já lê
            # `row.get("task_id")`: faltava a coluna vir.
            cur.execute("""
                SELECT id, area, acao, responsavel_r, responsavel_a,
                       responsavel_c, responsavel_i, prazo, status, notas,
                       concluido_em, concluido_em_fonte,
                       intel_task_id AS task_id
                  FROM raci_itens
                 WHERE empresa_id = %s
            """, (empresa_uuid,))
            rows = cur.fetchall()
        finally:
            conn.close()
        return [_normalize(dict(r), FONTE_CONSELHOOS) for r in rows], None
    except Exception as e:
        logger.warning(f"RACI ConselhoOS indisponível para {empresa_uuid}: {e}")
        return [], str(e)


def _sort_key(item: Dict):
    """
    Ordem de leitura: o que está atrasado primeiro, concluído por último; e
    dentro do bucket, por prazo mais próximo. Item sem prazo vai pro fim do
    seu bucket — não some, mas também não disputa o topo com quem tem data.
    """
    bucket = STATUS_ORDER.index(item["status_efetivo"]) if item["status_efetivo"] in STATUS_ORDER else 9
    sem_prazo = item["prazo"] is None
    return (bucket, sem_prazo, item["prazo"] or "", item["acao"].lower())


def _acumulado(itens: List[Dict]) -> Dict:
    """O que ACONTECEU na frente — não o que está aberto.

    POR QUE EXISTE (pedido da CoS, 10/08/26). A tela mostrava "10 abertos, 5
    atrasados" e o Renato afirmou não ter números para dar substância ao próprio
    posicionamento — tendo 52 de 62 fechados desde abril. Decidiu sobre si mesmo
    com foto parcial. "Só o que está aberto" está certo para o dia a dia e errado
    para responder o que a governança produziu.

    A RÉGUA DIZ SOBRE QUANTOS ELA FALA. `no_prazo` é calculado apenas sobre os
    itens que têm AS DUAS datas — conclusão e prazo. `sem_data` e `cobertura`
    saem junto, sempre: a coluna `concluido_em` nasceu em 11/08/26 e o passado
    só foi recuperado onde havia relato, então dizer "44 no prazo" sem dizer
    "de 10 medidos entre 54 concluídos" seria inventar precisão
    ([[feedback_regua_cobertura_parcial]]).
    """
    concluidos = [i for i in itens if i["status"] == "concluido"]
    com_data = [i for i in concluidos if i["concluido_em"]]
    avaliaveis = [i for i in com_data if i["concluido_no_prazo"] is not None]
    datas = sorted(i["concluido_em"] for i in com_data) if com_data else []

    # SÓ O CARIMBADO NO ATO SUSTENTA A AFIRMAÇÃO. O backfill veio de
    # `concluido_relatado_em`, que é a data do RELATÓRIO SEMANAL — e medido em
    # 11/08 ele se revelou pior que impreciso: é EM LOTE. No Vallen, 4 itens com
    # prazos de 15 a 30/04 foram todos carimbados em 11/05, a data do relatório
    # que os reportou juntos. Comparar essa data com o prazo de cada um produz
    # "atraso" que nunca existiu — o item fechou em abril e só foi relatado em
    # maio. Publicar isso como pontualidade seria o Renato se acusando de um
    # descumprimento que os dados não mostram.
    exatos = [i for i in avaliaveis if i["concluido_em_fonte"] == "gatilho"]
    no_prazo_exatos = [i for i in exatos if i["concluido_no_prazo"]]
    reconstruidos = [i for i in avaliaveis if i["concluido_em_fonte"] != "gatilho"]

    return {
        "total": len(itens),
        "concluidos": len(concluidos),
        # A frase que o Renato usa — "52 de 62 desde abril". Esta é sólida: não
        # depende de data nenhuma, só de status.
        "rotulo": f"{len(concluidos)}/{len(itens)}" if itens else None,
        "desde": datas[0] if datas else None,
        "ultimo_fechamento": datas[-1] if datas else None,

        # Pontualidade PUBLICÁVEL — só o que foi carimbado no ato.
        "medidos": len(exatos),
        "no_prazo": len(no_prazo_exatos),
        "fora_prazo": len(exatos) - len(no_prazo_exatos),
        "rotulo_pontualidade": (
            f"{len(no_prazo_exatos)} de {len(exatos)} medidos" if exatos else None
        ),

        # Contexto, explicitamente NÃO publicável.
        "reconstruidos": len(reconstruidos),
        "sem_data": len(concluidos) - len(com_data),
        "cobertura_pct": round(len(com_data) / len(concluidos) * 100) if concluidos else None,
        "aviso_reconstruido": (
            "Datas anteriores a 11/08/2026 vêm do relatório semanal, que reporta "
            "itens em lote: a data é do relato, não do fechamento, e produz atraso "
            "aparente. Não usar para afirmar pontualidade."
        ) if reconstruidos else None,
    }


def _chave_dedup(acao: str) -> str:
    """Texto da ação reduzido a uma chave comparável: minúsculas, sem acento,
    sem pontuação, espaços colapsados.

    Normalizar NÃO é similaridade. Duas ações só casam aqui se forem a MESMA
    frase escrita com caixa/acento/pontuação diferentes — a distância entre
    "Contrato da Dra. Camila — redigir" e "contrato da dra camila redigir" é
    zero, e entre coisas diferentes é infinita. É o que permite detectar sem
    risco de fundir.
    """
    s = unicodedata.normalize("NFKD", (acao or "").lower().strip())
    s = s.encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", s).split())


def _detectar_duplicatas(itens: List[Dict]) -> List[Dict]:
    """Pares (INTEL, ConselhoOS) que são o MESMO item nas duas fontes.

    DETECTAR, NUNCA FUNDIR — e a assimetria é o argumento inteiro. Um falso
    positivo aqui atrapalha um envio, que é recuperável com um clique; um falso
    positivo numa FUSÃO apaga responsabilidade de cliente do painel, e ninguém
    descobre. Por isso a detecção usa só a chave normalizada e os itens
    continuam TODOS na lista, cada um marcado.

    Por que não `SequenceMatcher`, que já existe em `raci_publicada.py`: medido
    em 05/10 no próprio conjunto, "Regra de repasse da **Dra. Daniela**" ×
    "Acordo de repasse da **Dra. Sayonê**" dá 0.58 — duas médicas diferentes,
    com contratos diferentes. Qualquer corte que pegue os pares de vocabulário
    divergente ("a Gestora" × "Jéssica") passa por cima desse 0.58 e funde
    contrato de médica. Similaridade serve pra ORDENAR candidato a revisão
    humana, não pra decidir identidade.

    COBERTURA, DITA EM VOZ ALTA (medido em prod, 05/10): na Vallen a chave pega
    **9 pares** de ~17 reais; na Alba, **10** de ~12. O resíduo são justamente
    os de vocabulário divergente, que texto nenhum resolve — fecham por elo
    (`task_id` dos dois lados) quando o lado INTEL for populado, e até lá ficam
    visivelmente duplicados, que é o estado honesto.
    """
    por_chave: Dict[str, Dict[str, List[Dict]]] = {}
    for it in itens:
        chave = _chave_dedup(it.get("acao"))
        if not chave:
            continue
        por_chave.setdefault(chave, {FONTE_INTEL: [], FONTE_CONSELHOOS: []})
        por_chave[chave].setdefault(it["fonte"], []).append(it)

    pares = []
    for chave, lados in por_chave.items():
        a, b = lados.get(FONTE_INTEL) or [], lados.get(FONTE_CONSELHOOS) or []
        if not (a and b):
            continue
        for i in a:
            for c in b:
                # LIGADO POR PONTEIRO NÃO É DUPLICATA — é a MESMA coisa, dita
                # duas vezes de propósito: a ata registra a deliberação, o INTEL
                # registra a execução dela, e o `conselhoos_raci_id` declara que
                # são a mesma. Sem esta linha, depois da importação de 06/10 a
                # detecção acusava 80 "duplicatas" na Vallen e 27 na Alba —
                # todas falsas, porque todo item importado tem, por construção,
                # texto idêntico à ata que o originou. A guarda de 409 bloquearia
                # 100% dos envios ao grupo do cliente, e uma guarda que grita
                # sempre é uma guarda que ninguém lê.
                #
                # O que ela continua pegando é o que importa: a transcrição
                # manual não pareada, que é a duplicação de verdade.
                if i.get("conselhoos_raci_id") and \
                        str(i["conselhoos_raci_id"]) == _split_uid(c["uid"])[1]:
                    continue
                pares.append({
                    "motivo": "texto_identico",
                    "acao": i["acao"],
                    "intel_uid": i["uid"],
                    "conselhoos_uid": c["uid"],
                    # Divergência de estado entre as cópias é informação: diz que
                    # alguém atualizou UM lado. Era o trabalho manual que a CoS
                    # fez à mão em 05/10 pra não atualizar só metade de cada item.
                    "status_divergente": i["status"] != c["status"],
                })
    return pares


def get_matrix(project_id: int, status: Optional[str] = None) -> Dict:
    """
    A matriz RACI de um projeto, unindo as fontes disponíveis.

    `status`: filtro opcional sobre `status_efetivo` ('atrasado', 'pendente',
    'em_andamento', 'concluido'). O resumo é sempre do conjunto COMPLETO — um
    filtro que também encolhesse o resumo esconderia justamente o que se quer
    ver ("quantos atrasados existem" enquanto se olha os concluídos).
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT p.id, p.nome, p.tipo, p.status, p.empresa_id,
                   e.nome_canonico AS empresa_nome,
                   e.conselhoos_empresa_id
              FROM projects p
              LEFT JOIN empresas e ON e.id = p.empresa_id
             WHERE p.id = %s
        """, (project_id,))
        projeto = cursor.fetchone()
        if not projeto:
            return {"error": "projeto não encontrado", "project_id": project_id}
        projeto = dict(projeto)

        itens = _fetch_intel(cursor, project_id)

    fontes = [{"fonte": FONTE_INTEL, "itens": len(itens), "erro": None}]

    # PASSO 4 (06/10/26) — A LEITURA É SÓ-INTEL. Antes, a matriz concatenava as
    # duas bases e cada deliberação que existia dos dois lados aparecia duas
    # vezes (Vallen +70%, Alba +83%). Com os ponteiros da 084/085 no lugar, a
    # execução mora num lugar só.
    #
    # ⚠️ MAS A BUSCA AO CONSELHOOS CONTINUA — e a distinção é o ponto. Ela não
    # alimenta mais a LISTA; alimenta a CONFERÊNCIA. Se eu simplesmente parasse
    # de consultar, `duplicatas` passaria a devolver zero sem ter olhado, e a
    # guarda de 409 do envio ao grupo do cliente viraria um verificador que
    # certifica o que não mediu — o modo de falha mais caro desta casa. Depois
    # da importação, o esperado é zero DE VERDADE; se aparecer algo, é sinal de
    # que a importação regrediu, e é exatamente isso que se quer saber.
    conselho_itens, erro_cos = [], None
    empresa_uuid = projeto.get("conselhoos_empresa_id")
    if empresa_uuid:
        conselho_itens, erro_cos = _fetch_conselhoos_status(str(empresa_uuid))
        fontes.append({
            "fonte": FONTE_CONSELHOOS,
            "empresa": projeto.get("empresa_nome"),
            "itens": len(conselho_itens),
            "erro": erro_cos,
            # O front usa `fontes` para decidir se mostra a legenda "itens
            # marcados ConselhoOS gravam lá". Com a leitura unificada isso
            # deixou de ser verdade, e legenda que sobrevive à mudança vira
            # instrução errada numa tela que grava em banco de cliente.
            "exibida": False,
            "papel": "conferência de duplicata — não entra na lista",
        })

    resumo = {s: 0 for s in STATUS_ORDER}
    for it in itens:
        resumo[it["status_efetivo"]] = resumo.get(it["status_efetivo"], 0) + 1

    # O RESUMO APAGAVA MOVIMENTO (05/10/26). `status_efetivo` converte todo item
    # com prazo vencido em `atrasado` — inclusive os `em_andamento`. Como quase
    # tudo no Vallen está com prazo vencido, a coluna "em andamento" do painel é
    # ESTRUTURALMENTE zero e nunca informou nada: medido nos 39 abertos, o status
    # bruto tem 17 `em_andamento` e o painel mostrava 0. Em 05/10 a CoS gravou
    # movimento real em 12 itens a partir do grupo e nenhum apareceu na tela.
    #
    # Atrasado e em-andamento são dimensões diferentes — uma é sobre PRAZO, a
    # outra sobre ATIVIDADE. Colapsar as duas perde exatamente o sinal de que a
    # frente respira, que é o que sustenta a conversa com o cliente.
    #
    # `resumo` fica intacto (o template indexa `dados.resumo[status]`); a segunda
    # dimensão vem ao lado, para que ninguém precise escolher entre as duas.
    resumo_bruto = {}
    for it in itens:
        resumo_bruto[it["status"]] = resumo_bruto.get(it["status"], 0) + 1
    atrasados_com_movimento = sum(
        1 for it in itens
        if it["status_efetivo"] == "atrasado" and it["status"] == "em_andamento"
    )
    movimento = {
        "resumo_bruto": resumo_bruto,
        "atrasados_com_movimento": atrasados_com_movimento,
        # A frase pronta, para o painel e para o grupo não precisarem recalcular
        # (e divergirem): "30 atrasados, dos quais 17 com movimento".
        "rotulo": (
            f"{resumo.get('atrasado', 0)} atrasados, dos quais "
            f"{atrasados_com_movimento} com movimento"
        ) if resumo.get("atrasado") else None,
    }

    # Calculado ANTES do filtro, como o resumo: acumulado que encolhe quando se
    # olha "só os atrasados" esconderia justamente o que ele existe pra mostrar.
    acumulado = _acumulado(itens)

    # Detecção ANTES do filtro também: duplicata que só aparece quando se olha
    # "os atrasados" seria duplicata que a guarda do envio não vê.
    # A detecção recebe os dois lados DE PROPÓSITO, mesmo com a lista sendo só
    # INTEL: ela compara execução com ata, e sem o segundo lado não compara nada.
    duplicatas = _detectar_duplicatas(itens + conselho_itens)
    _uids_dup = {u for p in duplicatas for u in (p["intel_uid"], p["conselhoos_uid"])}
    for it in itens:
        it["duplicado"] = it["uid"] in _uids_dup

    # Não poder checar NÃO é "não há duplicata". Quem consome isto (a guarda de
    # 409 no envio ao grupo) precisa distinguir "conferi e está limpo" de "não
    # consegui conferir", senão a abstenção vira carimbo de aprovação.
    #
    # ⚠️ MAS "não há o que conferir" é a TERCEIRA resposta, e esquecê-la custou
    # caro: a primeira versão (06/10) usava `bool(empresa_uuid) and not erro_cos`,
    # o que marcava como NÃO-CONFERIDO todo projeto sem vínculo ConselhoOS — isto
    # é, a MAIORIA deles. A guarda passaria a recusar o envio de RACI desses
    # projetos para sempre, alegando falha numa conferência que nunca teve o que
    # conferir. Guarda que bloqueia o caso são é tão inútil quanto guarda que
    # libera o caso podre; as duas acabam desligadas.
    #
    # Sem segunda fonte não existe duplicata ENTRE fontes: a conferência é
    # vacuamente completa, e dizer `True` aqui é a verdade, não uma concessão.
    duplicatas_checadas = (not empresa_uuid) or (not erro_cos)

    if status:
        itens = [it for it in itens if it["status_efetivo"] == status]

    itens.sort(key=_sort_key)

    return {
        "project": {
            "id": projeto["id"],
            "nome": projeto["nome"],
            "tipo": projeto["tipo"],
            "status": projeto["status"],
            "empresa": projeto.get("empresa_nome"),
        },
        "itens": itens,
        "total": len(itens),
        "resumo": resumo,
        "movimento": movimento,
        "acumulado": acumulado,
        "duplicatas": duplicatas,
        "duplicatas_total": len(duplicatas),
        "duplicatas_checadas": duplicatas_checadas,
        "fontes": fontes,
        "filtro_status": status,
        "gerado_em": datetime.now().strftime("%d/%m/%Y %H:%M"),
    }


# ==================== envio pro grupo do projeto ====================

_EMOJI_STATUS = {"atrasado": "🚨", "em_andamento": "🔄",
                 "pendente": "⏳", "concluido": "✅"}

_TITULO_BUCKET = {
    "atrasado": "Atrasados",
    "em_andamento": "Em andamento",
    "pendente": "Pendentes",
}


def _primeiro_nome(nome: Optional[str]) -> str:
    """'Jéssica (cobrindo Veridiana)' -> 'Jéssica'. No grupo todo mundo sabe
    quem é quem; o parêntese come a linha e empurra o prazo pra outra."""
    if not nome:
        return "—"
    limpo = nome.split("(")[0].strip()
    return limpo.split("/")[0].strip() or nome.strip()


def _cortar(texto: str, n: int = 95) -> str:
    texto = (texto or "").strip().replace("\n", " ")
    return texto if len(texto) <= n else texto[: n - 1].rstrip() + "…"


def format_for_whatsapp(matrix: Dict, incluir_concluidos: bool = False) -> str:
    """
    A matriz como texto de WhatsApp, pra mandar no grupo do projeto.

    SEM NUMERAÇÃO, e isso é decisão, não esquecimento. O `parse_raci_update`
    escuta os grupos e interpreta "3 concluído" pela ordem do
    `generate_raci_report` — que NÃO é esta ordem (a daqui inclui itens do
    INTEL que aquele report não enxerga, e ordena por `status_efetivo`
    derivado). Mandar numerado convidaria uma resposta que acertaria o item
    errado — exatamente o desalinhamento que a trava de 29/07 existe pra
    conter. Bullet não convida número.

    Concluídos entram só como contagem por padrão: quem lê o RACI no grupo
    quer saber o que falta. O texto é editável antes de sair, então listar é
    escolha de quem envia, não default de quem gera.
    """
    p = matrix.get("project") or {}
    itens = matrix.get("itens") or []
    resumo = matrix.get("resumo") or {}

    linhas = [f"📋 *RACI — {p.get('nome') or 'projeto'}*",
              f"_{date.today().strftime('%d/%m/%Y')}_", ""]

    for bucket in ("atrasado", "em_andamento", "pendente"):
        do_bucket = [i for i in itens if i["status_efetivo"] == bucket]
        if not do_bucket:
            continue

        # MOVIMENTO, dentro do bucket de atrasados (07/10/26). `status_efetivo`
        # converte TODO item com prazo vencido em `atrasado` — inclusive os que
        # estão `em_andamento` de verdade. Como quase tudo na Vallen está com
        # prazo vencido, o bucket "em andamento" fica estruturalmente vazio e o
        # grupo do cliente recebia 18 linhas indistinguíveis, com os contratos
        # que a Lara enviou anteontem parecendo tão parados quanto um item que
        # ninguém tocou desde agosto.
        #
        # Atrasado é sobre PRAZO; em-andamento é sobre ATIVIDADE. Colapsar as
        # duas perde exatamente o sinal que sustenta a conversa com o cliente —
        # e a RACI que o Renato mandou à mão em 05/10 dizia "11 em andamento,
        # 11 com prazo vencido", ou seja, ele já fazia isso manualmente.
        #
        # O `get_matrix` já calcula `movimento`; aqui só se deixa de jogar fora.
        com_mov = [i for i in do_bucket if i.get("status") == "em_andamento"]
        sufixo = ""
        if bucket == "atrasado" and com_mov:
            sufixo = f" — _{len(com_mov)} com movimento_"
        linhas.append(f"{_EMOJI_STATUS[bucket]} *{_TITULO_BUCKET[bucket]} "
                      f"({len(do_bucket)}):*{sufixo}")
        # Os que andam primeiro: quem lê rola a lista de cima para baixo e
        # desiste no meio; enterrar o que se mexeu embaixo do que não se mexeu
        # entrega a pior leitura possível da frente.
        for it in sorted(do_bucket, key=lambda x: x.get("status") != "em_andamento"):
            prazo = f" ({it['prazo_br']})" if it.get("prazo_br") else ""
            marca = "🔄 " if (bucket == "atrasado" and it.get("status") == "em_andamento") else ""
            linhas.append(f"• {marca}{_cortar(it['acao'])} — *{_primeiro_nome(it.get('r'))}*{prazo}")
        linhas.append("")

    concluidos = [i for i in itens if i["status_efetivo"] == "concluido"]
    if concluidos:
        if incluir_concluidos:
            linhas.append(f"✅ *Concluídos ({len(concluidos)}):*")
            for it in concluidos:
                linhas.append(f"• {_cortar(it['acao'])}")
            linhas.append("")
        else:
            linhas.append(f"✅ *{len(concluidos)} concluído"
                          f"{'s' if len(concluidos) > 1 else ''}* desde o início.")
            linhas.append("")

    if not itens:
        linhas.append("_Nenhum item na matriz._")

    total = sum(resumo.values()) if resumo else len(itens)
    linhas.append(f"_{total} itens no total._")
    return "\n".join(linhas).strip()


# CORRIGIDO 29/07: o teto de 4.096 foi criado em 28/07 sobre uma premissa
# FALSA — "a Evolution corta em silêncio". Ela não corta. Medido contra a
# própria API (`chat/findMessages`, que devolve o que foi ENTREGUE): digests de
# **6.463**, 6.017 e 5.748 chars chegaram inteiros, terminando em frase
# completa, e um preview de RACI de 4.669 chegou com o rodapé "_Fim do
# preview_" intacto. Se houvesse corte em 4.096, aquele preview teria perdido
# os ~570 caracteres finais.
#
# O estrago do erro era ATIVO e mordia justamente aqui: a Vallen com os 50
# concluídos listados dá 4.774 chars, então o botão "Enviar no grupo" —
# adotado em 29/07 como o caminho oficial de segunda-feira — RECUSAVA o RACI
# completo sem nenhuma razão técnica.
#
# O teto agora é conservador e ancorado em evidência de entrega, não em spec
# que eu não verifiquei: 16.000 é ~2,5× o maior envio comprovadamente entregue.
# Acima disso a recusa continua, porque aí já não é limite de canal — é sinal
# de que algo montou texto demais.
WHATSAPP_MAX_CHARS = 16000

# Acima disto o texto ainda é ENVIADO, mas a tela avisa: ninguém lê 4 mil
# caracteres num grupo. É conselho de legibilidade, não limite de protocolo —
# a diferença importa, porque tratar preferência como limite técnico foi
# exatamente o erro que esta constante corrige.
WHATSAPP_LEGIBILIDADE_CHARS = 4096


# ==================== escrita (write-through, cada fonte na sua) ==========

_CAMPOS = ("area", "acao", "responsavel_r", "responsavel_a", "responsavel_c",
           "responsavel_i", "prazo", "status", "notas", "task_id")

# `task_id` fica de fora: do lado de lá a coluna é `intel_task_id` e quem a
# governa é o `conselhoos_raci_sync`. Deixar a tela escrever nela seria criar
# um segundo dono pro mesmo elo.
_CAMPOS_CONSELHOOS = ("area", "acao", "responsavel_r", "responsavel_a",
                      "responsavel_c", "responsavel_i", "prazo", "status",
                      "notas")

# NOT NULL no ConselhoOS (`\d raci_itens` do outro Neon, conferido 29/07).
_OBRIGATORIOS_CONSELHOOS = ("area", "acao", "prazo", "status")


def _split_uid(uid: Any):
    """
    `'intel:12'` / `'conselhoos:<uuid>'` -> `(fonte, ident)`.

    Id nu (`12`) é aceito como INTEL: era o formato da primeira versão da
    tela e continua chegando de link salvo ou aba aberta. `(None, None)`
    quando não dá pra dizer com certeza de qual fonte é — adivinhar aqui
    escreveria no banco errado.
    """
    texto = str(uid).strip()
    if ":" in texto:
        fonte, _, ident = texto.partition(":")
        fonte, ident = fonte.strip().lower(), ident.strip()
    else:
        fonte, ident = FONTE_INTEL, texto

    if fonte not in (FONTE_INTEL, FONTE_CONSELHOOS) or not ident:
        return None, None
    if fonte == FONTE_INTEL and not ident.lstrip("-").isdigit():
        return None, None
    return fonte, ident


def _limpar(valor: Any) -> Any:
    return (valor.strip() or None) if isinstance(valor, str) else valor


def _validar_status(campos: Dict) -> Optional[str]:
    """`status` é ENUM lá e CHECK aqui: os dois estouram feio com valor
    inválido. Barrar antes devolve 400 legível em vez de 500."""
    if "status" in campos:
        valor = _limpar(campos["status"])
        if valor not in STATUS_ORDER:
            return f"status inválido: {campos['status']!r}"
    return None


def propor_pareamento(project_id: int) -> Dict:
    """Proposta de pareamento INTEL↔ConselhoOS para o passo 3 do plano.

    PROPÕE, nunca decide — e a distinção é a razão de existir da tela. A chave
    normalizada resolve sozinha os pares de texto idêntico (19 em prod: 9 na
    Vallen, 10 na Alba); o resíduo é o de vocabulário divergente ("a Gestora" ×
    "Jéssica"), e para ele a similaridade entra APENAS PARA ORDENAR os
    candidatos na tela. Nunca para casar: medido em 05/10 no próprio conjunto,
    "Regra de repasse da **Dra. Daniela**" × "Acordo de repasse da
    **Dra. Sayonê**" dá 0.58 — duas médicas, dois contratos. Confirmar é do
    Renato.

    "Sem par" é resposta VÁLIDA e esperada, não lacuna: são os itens de execução
    que nunca passaram por reunião (3 na Vallen — esteticista, Dra. Sayonê,
    Dra. Camila). Uma tela que só permitisse casar empurraria o usuário a
    inventar par para fechar a lista.
    """
    from difflib import SequenceMatcher

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT p.id, p.nome, e.conselhoos_empresa_id
              FROM projects p LEFT JOIN empresas e ON e.id = p.empresa_id
             WHERE p.id = %s
        """, (project_id,))
        proj = cursor.fetchone()
        if not proj:
            return {"error": "projeto não encontrado", "project_id": project_id}
        proj = dict(proj)

        cursor.execute("""
            SELECT id, acao, responsavel_r, prazo, status, conselhoos_raci_id,
                   sem_par_declarado_em
              FROM raci_itens WHERE project_id = %s ORDER BY id
        """, (project_id,))
        intel = [dict(r) for r in cursor.fetchall()]

    uuid_cos = proj.get("conselhoos_empresa_id")
    if not uuid_cos:
        return {"error": "projeto sem vínculo ConselhoOS", "project_id": project_id}

    cos_itens, erro = _fetch_conselhoos_status(str(uuid_cos))
    if erro:
        # Não poder ler o outro lado não é "não há par": parear sobre lista
        # vazia marcaria tudo como "sem par" e congelaria o erro no banco.
        return {"error": f"ConselhoOS indisponível: {erro}", "project_id": project_id}

    ja_apontados = {str(i["conselhoos_raci_id"]) for i in intel if i["conselhoos_raci_id"]}
    livres = [c for c in cos_itens if str(c["id"]) not in ja_apontados]

    pendentes, ja_pareados, declarados_sem_par = [], 0, 0
    for i in intel:
        if i["conselhoos_raci_id"]:
            ja_pareados += 1
            continue
        if i.get("sem_par_declarado_em"):
            # Já foi olhado e decidido: "nunca passou por conselho". Reofertar
            # seria pedir de novo um trabalho já feito — e foi exatamente o que
            # acontecia antes da 085, quando a decisão não tinha onde morar.
            declarados_sem_par += 1
            continue
        chave = _chave_dedup(i["acao"])
        exato = next((c for c in livres if _chave_dedup(c["acao"]) == chave), None)
        candidatos = []
        if exato:
            candidatos.append({"id": str(exato["id"]), "acao": exato["acao"],
                               "r": exato.get("r"), "prazo_br": exato.get("prazo_br"),
                               "status": exato.get("status"),
                               "score": 1.0, "exato": True})
        for c in livres:
            if exato and c["id"] == exato["id"]:
                continue
            s = SequenceMatcher(None, chave, _chave_dedup(c["acao"])).ratio()
            if s >= 0.45:
                candidatos.append({"id": str(c["id"]), "acao": c["acao"],
                                   "r": c.get("r"), "prazo_br": c.get("prazo_br"),
                                   "status": c.get("status"),
                                   "score": round(s, 2), "exato": False})
        candidatos.sort(key=lambda x: -x["score"])
        pendentes.append({
            "intel_id": i["id"],
            "acao": i["acao"],
            "r": i.get("responsavel_r"),
            "prazo_br": i["prazo"].strftime("%d/%m/%Y") if i.get("prazo") else None,
            "status": i.get("status"),
            "sugestao_automatica": exato is not None,
            "candidatos": candidatos[:5],
        })

    return {
        "project": {"id": proj["id"], "nome": proj["nome"]},
        "pendentes": pendentes,
        "total_pendentes": len(pendentes),
        "com_sugestao_exata": sum(1 for p in pendentes if p["sugestao_automatica"]),
        "ja_pareados": ja_pareados,
        "declarados_sem_par": declarados_sem_par,
        "conselhoos_livres": len(livres),
    }


def definir_par(intel_id: int, conselhoos_raci_id: Optional[str],
                sem_par: bool = False) -> Dict:
    """Grava o ponteiro, DECLARA que não há par, ou desfaz a decisão.

    Três estados, e a distinção entre os dois últimos é o conserto da migration
    085. Antes, "sem par" gravava NULL — o mesmo valor de "ainda não decidi" —
    e a decisão de quem olhou item por item não ficava em lugar nenhum:
    reabrir a tela mostrava tudo como pendente de novo, e não havia como
    responder se o pareamento tinha terminado.

      · `conselhoos_raci_id` preenchido → aponta para a linha de ata;
      · `sem_par=True` → declara, COM DATA, que esta execução nunca passou por
        reunião de conselho. Ausência vira afirmação, e afirmação se audita;
      · ambos vazios → desfaz, devolvendo o item a "não decidido".

    O índice único parcial da 084 garante que dois itens do INTEL não apontem
    para a mesma linha de ata; o CHECK da 085 impede o estado contraditório de
    apontar e declarar ao mesmo tempo. Nos dois casos quem recusa é o banco, não
    a boa intenção de quem chama.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                UPDATE raci_itens
                   SET conselhoos_raci_id = %s,
                       sem_par_declarado_em = CASE WHEN %s THEN NOW() ELSE NULL END,
                       atualizado_em = NOW()
                 WHERE id = %s
             RETURNING id, conselhoos_raci_id, sem_par_declarado_em
            """, (conselhoos_raci_id, bool(sem_par), intel_id))
            row = cursor.fetchone()
            if not row:
                return {"error": f"item INTEL {intel_id} não encontrado"}
            conn.commit()
        except Exception as e:
            conn.rollback()
            if "idx_raci_itens_conselhoos_raci_id" in str(e):
                return {"error": "essa linha do ConselhoOS já está pareada com "
                                 "outro item do INTEL — desfaça o outro antes"}
            return {"error": f"{type(e).__name__}: {e}"}
    r = dict(row)
    return {"ok": True, "intel_id": r["id"],
            "conselhoos_raci_id": str(r["conselhoos_raci_id"]) if r["conselhoos_raci_id"] else None,
            "sem_par_declarado_em": r["sem_par_declarado_em"].isoformat() if r.get("sem_par_declarado_em") else None}


def _ja_existe_no_conselhoos(project_id: int, acao: str) -> Optional[str]:
    """`uid` do item gêmeo no ConselhoOS, ou None.

    Mesma chave normalizada da detecção — nunca similaridade, pelo motivo
    registrado em `_detectar_duplicatas`.

    ⚠️ Projeto sem vínculo ConselhoOS, env ausente ou outro Neon fora do ar
    devolvem None e a criação SEGUE. Aqui a abstenção é a escolha certa, ao
    contrário da guarda do envio: bloquear criação por indisponibilidade de um
    banco que não é o nosso pararia o trabalho do dia por uma rede ruim, e o
    custo do erro é assimétrico — duplicata que nasce ainda é detectada na
    leitura e barrada no envio, que são as duas redes seguintes.
    """
    chave = _chave_dedup(acao)
    if not chave:
        return None
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT e.conselhoos_empresa_id
                  FROM projects p
                  LEFT JOIN empresas e ON e.id = p.empresa_id
                 WHERE p.id = %s
            """, (project_id,))
            row = cursor.fetchone()
        uuid = (dict(row).get("conselhoos_empresa_id") if row else None)
        if not uuid:
            return None
        itens, erro = _fetch_conselhoos_status(str(uuid))
        if erro:
            logger.warning("raci create: ConselhoOS indisponível (%s) — sigo sem checar", erro)
            return None
        for it in itens:
            if _chave_dedup(it.get("acao")) == chave:
                return it["uid"]
    except Exception as e:
        logger.warning("raci create: checagem de duplicata falhou (%s) — sigo sem checar", e)
    return None


def create_item(project_id: int, data: Dict) -> Dict:
    """Cria um item de RACI no lado INTEL. `acao` é o único obrigatório.

    GUARDA NA ORIGEM (05/10/26) — dedup na leitura é band-aid enquanto o produtor
    vive. Medido em prod hoje, o produtor NÃO é um cron: os 25 itens do projeto 24
    são todos `origem='manual'`, criados em 5 datas diferentes, e os 18 da Alba vêm
    de `raci_grupo_04set` e `ata_alba_07_08`. Ou seja, é uma SESSÃO transcrevendo
    ata/reunião para o INTEL numa empresa cujas mesmas ações já estão no
    ConselhoOS. Nenhum código copia COS→INTEL (os 10 itens com `origem='cos'` são
    do projeto 28, que não tem vínculo ConselhoOS — "cos" ali é outra coisa).

    Então a guarda tem de ficar onde a cópia nasce. Mesma assimetria do envio:
    recusar uma criação é recuperável com um flag; deixar nascer a duplicata custa
    uma linha repetida no grupo do cliente e um item que alguém vai atualizar pela
    metade. `permitir_duplicata=True` libera o caso legítimo, declarado.
    """
    acao = (data.get("acao") or "").strip()
    if not acao:
        return {"error": "acao é obrigatória"}
    erro = _validar_status({"status": data.get("status") or "pendente"})
    if erro:
        return {"error": erro}

    if not data.get("permitir_duplicata"):
        gemeo = _ja_existe_no_conselhoos(project_id, acao)
        if gemeo:
            return {
                "error": (
                    "esta ação já existe no ConselhoOS desta empresa "
                    f"(item {gemeo}) — criar aqui produziria a linha duplicada "
                    "que o grupo do cliente receberia duas vezes. Se for mesmo um "
                    "item distinto, reenvie com permitir_duplicata=true."
                ),
                "duplicata_de": gemeo,
            }

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO raci_itens
                (project_id, area, acao, responsavel_r, responsavel_a,
                 responsavel_c, responsavel_i, prazo, status, notas, origem, task_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            project_id,
            (data.get("area") or "").strip() or None,
            acao,
            (data.get("responsavel_r") or "").strip() or None,
            (data.get("responsavel_a") or "").strip() or None,
            (data.get("responsavel_c") or "").strip() or None,
            (data.get("responsavel_i") or "").strip() or None,
            data.get("prazo") or None,
            (data.get("status") or "pendente").strip(),
            (data.get("notas") or "").strip() or None,
            (data.get("origem") or "manual").strip(),
            data.get("task_id"),
        ))
        item_id = cursor.fetchone()["id"]
        conn.commit()
    return {"ok": True, "id": item_id}


def update_item(item_uid: Any, data: Dict) -> Dict:
    """
    Atualiza um item na FONTE dele. `item_uid` é `'intel:12'` ou
    `'conselhoos:<uuid>'` (id nu = INTEL, retrocompat).

    Só mexe nos campos que vieram — um PATCH que zerasse o que não foi
    enviado apagaria responsável por omissão.
    """
    fonte, ident = _split_uid(item_uid)
    if not fonte:
        return {"error": f"identificador de item inválido: {item_uid!r}"}
    if fonte == FONTE_CONSELHOOS:
        return _update_conselhoos(ident, data)
    return _update_intel(int(ident), data)


def _update_intel(item_id: int, data: Dict) -> Dict:
    campos = {k: data[k] for k in _CAMPOS if k in data}
    if not campos:
        return {"error": "nada a atualizar"}
    erro = _validar_status(campos)
    if erro:
        return {"error": erro}

    sets, valores = [], []
    for k, v in campos.items():
        sets.append(f"{k} = %s")
        valores.append(_limpar(v))
    sets.append("atualizado_em = CURRENT_TIMESTAMP")
    valores.append(item_id)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE raci_itens SET {', '.join(sets)} WHERE id = %s", valores)
        afetados = cursor.rowcount
        conn.commit()
    if not afetados:
        return {"error": "item não encontrado", "id": item_id}
    return {"ok": True, "uid": f"{FONTE_INTEL}:{item_id}", "fonte": FONTE_INTEL}


def _update_conselhoos(item_uuid: str, data: Dict) -> Dict:
    """
    `UPDATE` no banco do ConselhoOS. Escreve na fonte, não espelha.

    As duas diferenças de schema que precisam morrer aqui, e não no banco:
    os NOT NULL (mandar `None` viraria `IntegrityError` genérico, e o Renato
    leria "erro ao salvar" sem saber que aquele campo nunca foi opcional) e o
    ENUM de status. `concluido_relatado_em` fica intocado de propósito — é do
    `raci_weekly_report`, ver cabeçalho do módulo.
    """
    campos = {k: data[k] for k in _CAMPOS_CONSELHOOS if k in data}
    if not campos:
        return {"error": "nada a atualizar"}
    erro = _validar_status(campos)
    if erro:
        return {"error": erro}

    for k in _OBRIGATORIOS_CONSELHOOS:
        if k in campos and _limpar(campos[k]) in (None, ""):
            rotulo = "prazo" if k == "prazo" else k
            return {"error": f"o ConselhoOS não aceita item sem {rotulo} — "
                             f"preencha ou edite lá"}

    url = _conselhoos_url()
    if not url:
        return {"error": "CONSELHOOS_DATABASE_URL não configurada"}

    sets, valores = [], []
    for k, v in campos.items():
        # O cast é obrigatório: psycopg2 manda `status` como texto e o Postgres
        # não converte pro enum sozinho num UPDATE parametrizado.
        sets.append(f"{k} = %s::raci_status" if k == "status" else f"{k} = %s")
        valores.append(_limpar(v))
    sets.append("updated_at = NOW()")
    valores.append(item_uuid)

    try:
        import psycopg2
        import psycopg2.extras

        conn = psycopg2.connect(url, connect_timeout=5)
        try:
            cur = conn.cursor()
            cur.execute(
                f"UPDATE raci_itens SET {', '.join(sets)} WHERE id = %s::uuid",
                valores)
            afetados = cur.rowcount
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"RACI ConselhoOS: falha ao gravar {item_uuid}: {e}")
        return {"error": f"não consegui gravar no ConselhoOS: {e}"}

    if not afetados:
        return {"error": "item não encontrado", "id": item_uuid}
    return {"ok": True, "uid": f"{FONTE_CONSELHOOS}:{item_uuid}",
            "fonte": FONTE_CONSELHOOS}


def delete_item(item_uid: Any) -> Dict:
    """
    Remove item — só do lado INTEL. Ver cabeçalho: apagar linha de RACI de
    conselho é destruir registro de ata de uma empresa, por um caminho que não
    é o dela.
    """
    fonte, ident = _split_uid(item_uid)
    if not fonte:
        return {"error": f"identificador de item inválido: {item_uid!r}"}
    if fonte == FONTE_CONSELHOOS:
        return {"error": "item de conselho não se remove pelo INTEL — "
                         "apague no ConselhoOS"}

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM raci_itens WHERE id = %s", (int(ident),))
        afetados = cursor.rowcount
        conn.commit()
    if not afetados:
        return {"error": "item não encontrado", "id": ident}
    return {"ok": True, "uid": f"{FONTE_INTEL}:{ident}", "fonte": FONTE_INTEL}
