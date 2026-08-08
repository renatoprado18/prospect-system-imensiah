"""Lê a planilha de torrefações do Jabô (#28) e grava o retrato do funil.

PEDIDO (CoS, 07/08/2026, task #999679): "a Andressa mantém à mão a planilha do
pipeline de torrefações, mas o INTEL não a lê — varredura semanal habilitaria o
sync automático do pipeline do Jabô".

O QUE A LEITURA MOSTROU, e que muda o que faz sentido construir:

  · São **49 linhas**, não centenas. O `rowCount` da API diz 906 porque é o
    tamanho da GRADE, não do conteúdo — a diferença é 95 por cento de vazio.
  · `Status` é **texto livre**, não categoria. 49 linhas produziram 12 grafias
    distintas, com três jeitos de escrever a mesma coisa ("Não responde", "Não
    responeu mais", "Não respondeu, posso tentar novamente") e erros de digitação
    ("Respindeu"). Normalizar isso por palavra-chave seria inventar um funil que
    ninguém digitou — a classe de defeito de [[feedback_filtro_vocabulario_errado_falha_calado]],
    em que o filtro erra o vocabulário e falha em silêncio.
  · A planilha **não é editada desde 15/07** e a última edição é do próprio
    Renato, não da Andressa. A premissa de "planilha viva" não se sustenta: o que
    o INTEL precisa saber sobre ela é justamente que ela parou.

Então este módulo NÃO inventa pipeline nem cria fichas. Ele responde três coisas
que hoje ninguém sabe sem abrir o Drive: quantos nomes existem, quantos nunca
foram trabalhados, e há quanto tempo ninguém toca. Status vai para a nota
LITERAL, agrupado por texto exato — quem lê vê o que a Andressa escreveu.

POR QUE NÃO CRIAR CONTATO PRA CADA TORREFAÇÃO: seriam 49 fichas comerciais num
CRM que passou a noite de 07/08 absorvendo 111 duplicatas. Importar lista de
prospecção pra dentro do CRM de relacionamento é decisão do Renato, não efeito
colateral de uma varredura semanal.
"""
from __future__ import annotations

import logging
from collections import Counter
from typing import Dict, List, Optional

import httpx

from database import get_db

logger = logging.getLogger(__name__)

PROJETO_JABO = 28
CONTA = "renato.almeida.prado@gmail.com"      # dona da planilha
PLANILHA_NOME = "roasters_contact_list"
ABA = "Torrefações"
TIPO_NOTA = "pipeline_sheet"

DRIVE_API = "https://www.googleapis.com/drive/v3/files"
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"


async def _token() -> Optional[str]:
    import integrations.google_contacts as gc
    return await gc.get_valid_token(CONTA)


async def ler(nome: str = PLANILHA_NOME, aba: str = ABA) -> Dict:
    """Linhas cruas da aba + metadados do arquivo.

    Busca pelo NOME e não por id fixo: id hardcoded morre calado no dia em que a
    planilha for recriada, e o cron seguiria reportando o retrato antigo como se
    fosse de hoje.
    """
    token = await _token()
    if not token:
        return {"erro": f"sem token para {CONTA}"}
    h = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=60) as cli:
        r = await cli.get(DRIVE_API, headers=h, params={
            "q": f"name='{nome}' and trashed=false",
            "fields": "files(id,name,modifiedTime,lastModifyingUser(emailAddress))"})
        arquivos = r.json().get("files", []) if r.status_code == 200 else []
        if not arquivos:
            return {"erro": f"planilha '{nome}' não encontrada no Drive de {CONTA}"}
        f = arquivos[0]

        r2 = await cli.get(f"{SHEETS_API}/{f['id']}/values/{aba}", headers=h)
        if r2.status_code != 200:
            return {"erro": f"aba '{aba}': HTTP {r2.status_code}"}
        vals = r2.json().get("values", [])

    if not vals:
        return {"erro": f"aba '{aba}' vazia"}
    cab = [c.strip() for c in vals[0]]
    linhas = [dict(zip(cab, r)) for r in vals[1:] if any(str(c).strip() for c in r)]
    return {"id": f["id"], "modificado_em": f["modifiedTime"],
            "ultimo_editor": (f.get("lastModifyingUser") or {}).get("emailAddress"),
            "colunas": cab, "linhas": linhas}


def retrato(dados: Dict) -> Dict:
    """Números que a planilha responde — sem normalizar o que é texto livre."""
    linhas = dados["linhas"]
    def campo(l, *nomes):
        for n in nomes:
            for k, v in l.items():
                if k.strip().lower() == n and str(v).strip():
                    return str(v).strip()
        return ""

    status = [campo(l, "status") for l in linhas]
    return {
        "total": len(linhas),
        "sem_status": sum(1 for s in status if not s),
        "com_telefone": sum(1 for l in linhas if campo(l, "phone/whatsapp", "phone", "telefone")),
        "com_email": sum(1 for l in linhas if campo(l, "email", "e-mail")),
        # Contagem por texto EXATO. Agrupar "Não responde" com "Não responeu
        # mais" exigiria decidir que são a mesma coisa — e são, mas quem decide
        # isso é quem escreveu, não um regex.
        "por_status": Counter(s for s in status if s).most_common(),
        "modificado_em": dados["modificado_em"],
        "ultimo_editor": dados["ultimo_editor"],
    }


def _texto(r: Dict, dias_parada: Optional[int]) -> str:
    linhas = [
        f"**{r['total']} torrefações** na planilha · "
        f"**{r['sem_status']} sem status** (nunca trabalhadas) · "
        f"{r['com_telefone']} com telefone · {r['com_email']} com e-mail",
        "",
    ]
    if dias_parada is not None:
        aviso = " ⚠️" if dias_parada > 14 else ""
        linhas.append(f"Planilha editada pela última vez há **{dias_parada} dias**"
                      f" (por {r['ultimo_editor'] or 'desconhecido'}).{aviso}")
        linhas.append("")
    linhas.append("Status como foram escritos (texto livre — não normalizo, "
                  "senão invento um funil que ninguém digitou):")
    for txt, n in r["por_status"]:
        linhas.append(f"- {n}× — {txt[:110]}")
    return "\n".join(linhas)


async def sincronizar(gravar: bool = True) -> Dict:
    """Lê a planilha e grava o retrato como nota do projeto #28.

    Uma nota por leitura, tipo `pipeline_sheet`: o histórico é o valor (dá pra
    ver o funil parado semana após semana). Se a leitura falhar, NÃO grava nada —
    nota de erro no projeto vira ruído, e o cron já registra a falha.
    """
    dados = await ler()
    if dados.get("erro"):
        logger.warning("jabo_pipeline_sheet: %s", dados["erro"])
        return {"status": "erro", "erro": dados["erro"]}

    r = retrato(dados)

    dias = None
    try:
        from datetime import datetime, timezone
        m = datetime.fromisoformat(dados["modificado_em"].replace("Z", "+00:00"))
        dias = (datetime.now(timezone.utc) - m).days
    except Exception:
        pass

    if not gravar:
        return {"status": "ok", "gravado": False, "retrato": r, "dias_parada": dias}

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO project_notes (project_id, tipo, titulo, conteudo, autor, metadata)
               VALUES (%s, %s, %s, %s, %s, %s::jsonb) RETURNING id""",
            (PROJETO_JABO, TIPO_NOTA,
             f"Pipeline de torrefações — {r['total']} nomes, {r['sem_status']} sem status",
             _texto(r, dias), "sistema",
             __import__("json").dumps({"planilha_id": dados["id"], "aba": ABA,
                                       "dias_parada": dias,
                                       "por_status": r["por_status"]}, ensure_ascii=False)))
        nota_id = cur.fetchone()["id"]
        conn.commit()

    logger.info("jabo_pipeline_sheet: nota %s — %s nomes, %s sem status, parada há %s dias",
                nota_id, r["total"], r["sem_status"], dias)
    return {"status": "ok", "gravado": True, "nota_id": nota_id,
            "retrato": r, "dias_parada": dias}
