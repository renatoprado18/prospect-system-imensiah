"""Rotas de andaime (teste/seed/trigger manual) nao podem escrever nem disparar
integracao externa sem credencial.

Achado que originou (30/07/2026): `GET /api/tasks/sync/test` rodava
`tasks_service.full_sync()` — sync BIDIRECIONAL com o Google Tasks, isto e,
ESCRITA na conta do Renato — anonimo, em producao, com o docstring admitindo
"sem autenticacao". O vizinho imediato `POST /api/tasks/sync` faz exatamente o
mesmo trabalho e exige sessao. Grep em app/templates, app/static, scripts,
workers, mcp e api nao achou nenhum consumidor da rota /test.

Mesma classe do commit 26252d8 (`verify_cron_auth` liberando ~40 rotas
/api/cron/* sem secret). Por isso o teste nao checa so a instancia:

- TestClient prova 401 real, request de verdade, sem credencial nenhuma
  (a auth rejeita antes de qualquer efeito — nao precisa de banco).
- Uma varredura AST guarda a CLASSE: se alguem adicionar uma rota de andaime
  nova sem auth, ou tirar a guarda de uma destas, o teste quebra.

Rodar:
  PYTHONPATH=app .venv/bin/pytest tests/test_endpoint_auth_exposure.py -q
"""
import ast
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))
sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

MAIN_PY = os.path.join(_ROOT, "app", "main.py")

# (metodo, path) fechados em 30/07/2026. Cada um escrevia no banco ou disparava
# integracao externa (Google Tasks, WhatsApp, Anthropic) sem verificar nada.
ROTAS_FECHADAS = [
    ("GET", "/api/tasks/sync/test"),                        # full_sync -> Google Tasks (escrita)
    ("POST", "/api/action-proposals/test-notification"),    # INSERT + WhatsApp real pro Renato
    ("GET", "/api/action-proposals/test-notification"),
    ("DELETE", "/api/action-proposals/test-cleanup"),       # DELETE FROM action_proposals
    ("GET", "/api/action-proposals/test-cleanup"),
    ("POST", "/api/intel-chat/synthesize-now"),             # Anthropic + INSERT system_memories
    ("POST", "/api/veiculos/seed-prado"),                   # seed sem idempotencia
    ("POST", "/api/oficinas/seed"),                         # seed sem idempotencia
    # 31/07/2026 — a leva de 30/07 fechou ESCRITA; estas duas ficaram de fora e
    # nao deviam ter ficado.
    ("GET", "/api/tasks/test-list"),                        # despejava 30 tasks anonimamente
    ("POST", "/api/whatsapp/fix-contact"),                  # UPDATE em lote do dono da mensagem
]

# Nomes que contam como verificacao de auth no corpo de um handler.
# CHAMAR NAO E BARRAR (06/08/2026). `get_current_user` nao e porteiro: ele
# DEVOLVE o usuario (ou None) e quem decide barrar e a rota. Estava aqui dentro
# como se fosse porteiro, e por isso `POST /api/push/subscribe` contava como
# fechada — ela chama o getter, ignora o None e grava a inscricao de push de um
# ANONIMO com `user_id=None`. Uma regua que confunde consultar com barrar
# certifica exatamente o que deveria pegar.
#
# Agora ele so vale como auth se a rota tambem BARRAR (raise/redirect/401/403) —
# ver `_barra_de_verdade`.
GETTERS_QUE_NAO_BARRAM = {"get_current_user"}

AUTH_CALLS = {
    "require_scaffold_auth", "require_api_auth", "require_auth", "require_admin",
    "require_operador", "verify_cron_auth", "_empresas_require_auth",
    "verify_session_token",
    # Porteiros de CONSUMIDOR EXTERNO. Nao usam cookie nem X-API-Key porque quem
    # chama e um terceiro (worker, Tonia): o segredo vai no corpo/header proprio.
    # Sem estes nomes aqui, a regua lia essas rotas como "sem auth" e a proxima
    # varredura as fecharia de novo -- foi assim que o webhook do WhatsApp caiu.
    "check_worker_secret", "check_ingest_secret",
    # A validacao da assinatura Svix da Fathom mora DENTRO deste handler, nao no
    # corpo do endpoint. Ancorar no nome da funcao e nao no docstring: docstring
    # se reescreve sem querer, e a rota voltaria a contar como aberta.
    "handle_fathom_webhook",
}

# Segredos de PROVEDOR: o terceiro assina/carimba a requisicao do jeito dele.
# Google Drive manda `x-goog-channel-token`; Fathom assina via Svix
# (`webhook-signature`). Uma rota que confere um destes ESTA fechada, mesmo sem
# nenhum `Depends` no decorator.
TOKENS_DE_PROVEDOR = ("x-goog-channel-token", "webhook-signature", "X-Ingest-Secret")


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    import main
    # raise_server_exceptions=False: se alguma rota vazar a guarda e explodir no
    # banco, queremos ver o status, nao um traceback mascarando o resultado.
    return TestClient(main.app, raise_server_exceptions=False)


class TestSemCredencial:
    """O contrato: sem cookie e sem X-API-Key, 401. Nunca 200."""

    @pytest.mark.parametrize("metodo,path", ROTAS_FECHADAS)
    def test_401_sem_credencial(self, client, metodo, path):
        r = client.request(metodo, path)
        assert r.status_code == 401, (
            f"{metodo} {path} respondeu {r.status_code} sem credencial — "
            f"esperado 401. Corpo: {r.text[:200]}"
        )

    @pytest.mark.parametrize("metodo,path", ROTAS_FECHADAS)
    def test_nao_executou_o_efeito(self, client, metodo, path):
        """Guarda contra 'fecha mas depois de agir': a resposta 401 nao pode
        trazer payload de execucao (result/deleted_count/proposal_id/veiculo)."""
        r = client.request(metodo, path)
        corpo = r.text
        for vazamento in ("deleted_count", "proposal_id", "\"result\"", "\"veiculo\""):
            assert vazamento not in corpo, (
                f"{metodo} {path} vazou {vazamento} em resposta 401 — "
                "a guarda esta depois do efeito"
            )

    def test_cookie_de_sessao_invalido_tambem_401(self, client):
        """Cookie forjado nao passa (o token e assinado)."""
        # cookie via header: `cookies=` por request esta deprecado no httpx
        r = client.request("GET", "/api/tasks/sync/test",
                           headers={"Cookie": "session=forjado"})
        assert r.status_code == 401

    def test_x_api_key_errada_tambem_401(self, client):
        r = client.request("GET", "/api/tasks/sync/test",
                           headers={"X-API-Key": "chave-errada-de-proposito"})
        assert r.status_code == 401

    def test_vizinho_autenticado_continua_401(self, client):
        """`POST /api/tasks/sync` era a referencia do fix — nao pode ter
        regredido junto."""
        assert client.request("POST", "/api/tasks/sync").status_code == 401


class TestCaminhoDaMaquina:
    """Fechar nao pode matar o uso real: FEATURES.md documenta
    `/api/intel-chat/synthesize-now` como trigger manual via curl. O helper tem
    que aceitar X-API-Key == INTEL_API_KEY.

    Testado no helper, nao via HTTP: um request valido EXECUTARIA o sync/a
    sintese de verdade (Google Tasks, Anthropic) — teste nao dispara efeito.
    """

    def _request(self, headers=None):
        from starlette.requests import Request
        cabecalhos = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
        return Request({"type": "http", "method": "GET", "path": "/x",
                        "headers": cabecalhos, "query_string": b""})

    def test_api_key_correta_libera(self, monkeypatch):
        import main
        monkeypatch.setenv("INTEL_API_KEY", "chave-de-teste-123")
        assert main.require_scaffold_auth(self._request({"X-API-Key": "chave-de-teste-123"})) is None

    def test_api_key_com_newline_libera(self, monkeypatch):
        """A UI da Vercel cola \\n no fim de env var — o strip() precisa existir."""
        import main
        monkeypatch.setenv("INTEL_API_KEY", "chave-de-teste-123\n")
        assert main.require_scaffold_auth(self._request({"X-API-Key": " chave-de-teste-123 "})) is None

    def test_sem_header_levanta_401(self, monkeypatch):
        from fastapi import HTTPException
        import main
        monkeypatch.setenv("INTEL_API_KEY", "chave-de-teste-123")
        with pytest.raises(HTTPException) as exc:
            main.require_scaffold_auth(self._request({}))
        assert exc.value.status_code == 401

    def test_env_vazia_nao_vira_bypass(self, monkeypatch):
        """INTEL_API_KEY ausente + header vazio nao podem 'casar' em ''. Era
        exatamente a forma do bug do 26252d8: comparacao que passa por acidente."""
        from fastapi import HTTPException
        import main
        monkeypatch.delenv("INTEL_API_KEY", raising=False)
        with pytest.raises(HTTPException) as exc:
            main.require_scaffold_auth(self._request({"X-API-Key": ""}))
        assert exc.value.status_code == 401

    def test_sem_bypass_de_dev_local(self, monkeypatch):
        """`verify_cron_auth` libera tudo quando BASE_URL=localhost — foi assim
        que o 26252d8 abriu 40 rotas em prod. Este helper nao herda isso."""
        from fastapi import HTTPException
        import main
        monkeypatch.setenv("BASE_URL", "http://localhost:8000")
        monkeypatch.delenv("INTEL_API_KEY", raising=False)
        with pytest.raises(HTTPException):
            main.require_scaffold_auth(self._request({}))


# --------------------------------------------------------------------------
# Guarda da CLASSE (AST): nenhuma rota de andaime em main.py sem auth.
# --------------------------------------------------------------------------

# Path com pinta de andaime. `/fix-names`, `/cleanup` etc. entram; superficie de
# produto (CRUD de contatos, projetos, editorial...) esta FORA de proposito:
# neste single-tenant a API de produto e anonima por historico, e fechar aquilo
# e decisao de modulo, nao drive-by. Ver relatorio da varredura de 30/07/2026.
PINTA_DE_ANDAIME = re.compile(r"/(test|debug|seed|force|trigger|manual)\b|"
                              r"/(test|debug|seed)-|-(now|test)$|/synthesize-now")

# Sem efeito nenhum — anonimo aqui e inofensivo.
ANDAIME_ANONIMO_OK = {
    # Retorna {"status":"ok"} e nada mais. Zero efeito, zero dado.
    "/api/v1/test",
    # Read-only por design declarado no proprio docstring: pulse de timestamps
    # consumido por agente externo (claude.ai routines) pra checar se cron
    # dispara. Nao muta e nao expoe conteudo.
    "/api/cron/pulse",
    # Redirect 301 legado /rap/* -> /contatos/limpeza. Nao toca em nada.
    "/rap/contacts/cleanup",
}

# Vazamento de LEITURA conhecido e ainda aberto. Fica separado do
# ANDAIME_ANONIMO_OK de proposito: aquele diz "pode ser anonimo", este diz "nao
# deveria, mas ainda e" — e o guard nao pode dar OK calado pra nenhum dos dois.
#
# 31/07/2026: esvaziado. `/api/tasks/test-list` (unico morador, despejava 30
# tasks com titulo/status/vencimento pra qualquer um) foi fechado com
# require_scaffold_auth e entrou em ROTAS_FECHADAS. Manter a rota aqui depois
# de fechada faria a lista mentir na direcao mais perigosa: dizer que ha divida
# onde nao ha mais, e reservar uma isencao pronta pra proxima rota que alguem
# quisesse deixar aberta.
ANDAIME_LEITURA_EXPOSTA: set[str] = set()

# ─────────────────────────────────────────────────────────────────────────────
# TETO DE ROTAS MUTANTES SEM AUTH — o que impede a divida de crescer.
#
# 03/08/2026 (2a leva): 193 -> 64. Fechadas 129 rotas SEM CONSUMIDOR no
# repo, com `dependencies=[Depends(require_scaffold_auth)]` no decorator.
# As 64 restantes tem consumidor (majoritariamente frontend, que ja carrega
# o cookie de sessao) e serao fechadas bloco a bloco, validando a tela.
#
# Medicao original com `_rotas_de_main` (que tambem reconhece comparacao
# manual de segredo, entao e mais preciso que um grep): **193** rotas
# POST/PUT/PATCH/DELETE sem NENHUMA verificacao, de 368 mutantes. Em 30/07 o
# board registrava ~184 — cresceu em quatro dias. Fechar as 193 sem barrar as
# novas e enxugar gelo, e por isso este teto vem ANTES do fechamento por modulo.
#
# Confirmado exposto, nao teorico: `POST /api/contacts/99999999/snooze` responde
# `404 "Contato nao encontrado"` a curl anonimo — o handler e alcancado. O unico
# middleware e CORS, que nao protege contra curl.
#
# COMO BAIXAR ESTE NUMERO: ao fechar um modulo, rode o teste, veja o numero novo
# e atualize aqui. **Baixar e obrigatorio** — se o teto ficar folgado, ele para
# de proteger e vira decoracao (foi o que aconteceu com `ANDAIME_LEITURA_EXPOSTA`
# antes de ser esvaziada). Subir exige decisao explicita e justificativa aqui.
# 06/08/26: 53 → 46. Sete a menos, mas a conta tem uma virada no meio: a régua
# passou a exigir que `get_current_user` BARRE, e não só seja chamado — com isso
# apareceu uma rota que estava escondida (`POST /api/push/subscribe`, que gravava
# inscrição de push de anônimo). O número real era 54, não 53. Fechado o bloco
# `/api/contacts` (7 rotas, consumidor só na UI, que exige sessão) mais o push:
# 54 - 8 = 46.
TETO_MUTANTES_SEM_AUTH = 13
# ─────────────────────────────────────────────────────────────────────────────


def _barra_de_verdade(corpo: str) -> bool:
    """A rota REJEITA quem não passou, ou só consultou quem era?

    Sinal grosso de propósito: qualquer 401/403, `raise HTTPException` ou
    redirect pro login serve. Errar para o lado de "não barra" só faz a rota
    aparecer na lista de trabalho — errar para o outro lado a esconde, e
    esconder foi o que deixou `POST /api/push/subscribe` aberta contando como
    fechada.
    """
    return any(t in corpo for t in (
        "raise HTTPException", "status_code=401", "status_code=403",
        "RedirectResponse", "/login",
    ))


def _rotas_de_main():
    """[(lineno, metodo, path, tem_auth, nome_da_funcao)] de app/main.py."""
    with open(MAIN_PY, encoding="utf-8") as fh:
        fonte = fh.read()
    linhas = fonte.split("\n")
    arvore = ast.parse(fonte, filename=MAIN_PY)
    saida = []
    for node in ast.walk(arvore):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        rotas = []
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                continue
            if not (isinstance(dec.func.value, ast.Name) and dec.func.value.id == "app"):
                continue
            if dec.func.attr not in ("get", "post", "put", "patch", "delete"):
                continue
            if dec.args and isinstance(dec.args[0], ast.Constant):
                rotas.append((dec.func.attr.upper(), dec.args[0].value))
        if not rotas:
            continue
        tem_auth = False
        # `dependencies=[Depends(auth)]` NO DECORATOR  # noqa: E501 (ver _barra_de_verdade) — forma idiomatica do
        # FastAPI e a usada no fechamento em massa de 03/08 (129 rotas). Sem
        # este ramo o teste nao enxergaria nenhuma delas e o teto continuaria
        # marcando 193 com o trabalho feito: guarda que nao ve o progresso
        # tambem nao ve a regressao.
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and any(
                kw.arg == "dependencies" and any(n in ast.dump(kw.value) for n in AUTH_CALLS)
                for kw in dec.keywords
            ):
                tem_auth = True
        # Depends(require_admin) e cia na assinatura
        for default in list(node.args.defaults) + list(node.args.kw_defaults):
            if default is None:
                continue
            despejo = ast.dump(default)
            if any(nome in despejo for nome in AUTH_CALLS):
                tem_auth = True
        # chamada de auth no corpo
        corpo_fn = "\n".join(linhas[node.lineno - 1:node.end_lineno])
        barra = _barra_de_verdade(corpo_fn)
        for interno in ast.walk(node):
            if isinstance(interno, ast.Call):
                f = interno.func
                nome = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
                if nome in AUTH_CALLS:
                    tem_auth = True
                # Getter só protege se a rota realmente rejeitar depois.
                elif nome in GETTERS_QUE_NAO_BARRAM and barra:
                    tem_auth = True
        # comparacao manual de segredo (X-API-Key / CRON_SECRET / hmac) ou
        # carimbo de provedor externo (Google channel token, assinatura Svix).
        corpo = "\n".join(linhas[node.lineno - 1:node.end_lineno])
        if any(t in corpo for t in ("X-API-Key", "CRON_SECRET", "compare_digest", "hmac")):
            tem_auth = True
        if any(t in corpo for t in TOKENS_DE_PROVEDOR):
            tem_auth = True
        for metodo, path in rotas:
            saida.append((node.lineno, metodo, path, tem_auth, node.name))
    return saida


class TestClasseAndaime:
    def test_as_oito_rotas_fechadas_tem_auth_no_codigo(self):
        """Prova por inspecao (complementa o 401 do TestClient): a guarda esta
        no corpo, nao num middleware que alguem pode desligar."""
        por_rota = {(m, p): tem_auth for _, m, p, tem_auth, _ in _rotas_de_main()}
        faltando = [r for r in ROTAS_FECHADAS if not por_rota.get(r)]
        assert not faltando, f"rota fechada perdeu a verificacao de auth: {faltando}"

    def test_nenhum_andaime_novo_sem_auth(self):
        """A CLASSE. Rota nova com pinta de teste/seed/trigger sem auth reprova
        aqui — em vez de virar o proximo /api/tasks/sync/test."""
        expostas = sorted({
            f"{m} {p} (main.py:{ln})"
            for ln, m, p, tem_auth, _ in _rotas_de_main()
            if not tem_auth and PINTA_DE_ANDAIME.search(p)
            and p not in ANDAIME_ANONIMO_OK and p not in ANDAIME_LEITURA_EXPOSTA
        })
        assert not expostas, (
            "rota de andaime sem verificacao de auth (use require_scaffold_auth, "
            f"ou justifique em ANDAIME_ANONIMO_OK): {expostas}"
        )

    def test_o_detector_realmente_pega_o_padrao(self):
        """Guarda contra o teste virar decoracao: prova que a varredura acusa o
        caso ruim — o codigo original da rota, palavra por palavra."""
        import tempfile

        ruim = (
            '@app.get("/api/tasks/sync/test")\n'
            "async def test_tasks_sync():\n"
            '    """Endpoint para testar sync de tasks (sem autenticacao)."""\n'
            "    tasks_service = get_tasks_sync_service()\n"
            "    result = await tasks_service.full_sync()\n"
            "    return result\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
            fh.write(ruim)
            tmp = fh.name
        global MAIN_PY
        original = MAIN_PY
        try:
            MAIN_PY = tmp
            achados = [(m, p) for _, m, p, tem_auth, _ in _rotas_de_main() if not tem_auth]
            assert ("GET", "/api/tasks/sync/test") in achados
            assert PINTA_DE_ANDAIME.search("/api/tasks/sync/test")
        finally:
            MAIN_PY = original
            os.unlink(tmp)


class TestTetoDeRotasMutantes:
    """A CLASSE MAIOR — nao deixar a divida crescer enquanto ela nao e paga.

    As rotas de andaime (testes acima) sao a divida NOMEADA. Esta classe cuida da
    divida de MASSA: 193 rotas POST/PUT/PATCH/DELETE sem verificacao nenhuma.

    Por que teto e nao zero: exigir zero reprovaria o repo inteiro hoje e o teste
    viraria `skip` na primeira semana — que e como guarda morre. Teto congela o
    numero e obriga a decisao consciente pra cada rota nova.
    """

    def _mutantes_sem_auth(self):
        return [(m, p) for _, m, p, tem_auth, _ in _rotas_de_main()
                if m in ("POST", "PUT", "PATCH", "DELETE") and not tem_auth]

    def test_divida_nao_cresce(self):
        atuais = self._mutantes_sem_auth()
        assert len(atuais) <= TETO_MUTANTES_SEM_AUTH, (
            f"rotas mutantes sem auth: {len(atuais)} — o teto e "
            f"{TETO_MUTANTES_SEM_AUTH}. Uma rota nova que escreve sem verificar "
            f"credencial foi adicionada.\n"
            f"Ou voce poe auth nela (`require_scaffold_auth` aceita sessao OU "
            f"X-API-Key), ou sobe o teto AQUI com justificativa escrita.\n"
            f"Contexto: `POST /api/contacts/{{id}}/snooze` responde 404 a curl "
            f"anonimo em producao — estas rotas sao alcancaveis de verdade."
        )

    def test_teto_nao_fica_folgado(self):
        """Guarda contra o teto virar decoracao. Se o numero real caiu (modulo
        fechado) e ninguem baixou o teto, ele para de proteger: uma rota nova
        passaria despercebida usando a folga. Baixar e obrigatorio."""
        atuais = self._mutantes_sem_auth()
        folga = TETO_MUTANTES_SEM_AUTH - len(atuais)
        assert folga <= 5, (
            f"o teto ({TETO_MUTANTES_SEM_AUTH}) esta {folga} acima do real "
            f"({len(atuais)}). Baixe TETO_MUTANTES_SEM_AUTH para {len(atuais)} — "
            f"teto folgado deixa entrar rota nova sem ninguem ver."
        )
