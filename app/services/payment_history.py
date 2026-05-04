"""
Payment History Service

Registra despesas do ciclo de pagamento mensal numa planilha Google Sheets
pra historico permanente. Complementa payment_cycle.py (que so envia email).

Planilha alvo (config fixa):
- Spreadsheet: 1PikAs8tPAp5KNZyHyyfdw9sOGOATNuBa-fks8f85Du4
- Aba: 'Historico' (criada automaticamente se nao existir)

Schema da aba (1 row por forma de pagamento — se despesa tem PIX e boleto vira 2 rows):
| Mes | Despesa | Valor | Forma | Codigo | Vencimento | Data Registro |
"""
import logging
from datetime import datetime
from typing import Dict, List, Any

logger = logging.getLogger(__name__)


SPREADSHEET_ID = "1PikAs8tPAp5KNZyHyyfdw9sOGOATNuBa-fks8f85Du4"
SHEET_NAME = "Historico"
HEADERS = [
    "Mes",
    "Despesa",
    "Valor",
    "Forma",
    "Codigo",
    "Vencimento",
    "Data Registro",
]

MESES = [
    "",
    "Janeiro",
    "Fevereiro",
    "Marco",
    "Abril",
    "Maio",
    "Junho",
    "Julho",
    "Agosto",
    "Setembro",
    "Outubro",
    "Novembro",
    "Dezembro",
]


def _build_rows(month: int, year: int, expenses: List[Dict]) -> List[List[Any]]:
    """
    Compoe as rows pra append. 1 row por (despesa, forma de pagamento).
    Se despesa tem PIX e boleto, vira 2 rows.
    Se nao tem nenhum, vira 1 row com forma '-'.
    """
    if not (1 <= month <= 12):
        raise ValueError(f"Mes invalido: {month}")

    mes_label = f"{MESES[month]}/{year}"
    data_reg = datetime.now().strftime("%d/%m/%Y")

    rows: List[List[Any]] = []
    for e in expenses:
        nome = e.get("nome", "")
        valor = e.get("valor", 0)
        venc_dia = e.get("vencimento_dia", "-")
        venc_label = f"Dia {venc_dia}" if venc_dia not in (None, "", "-") else "-"
        pix = e.get("pix")
        boleto = e.get("boleto")

        if pix:
            rows.append([mes_label, nome, valor, "PIX", pix, venc_label, data_reg])
        if boleto:
            rows.append([mes_label, nome, valor, "Boleto", boleto, venc_label, data_reg])
        if not pix and not boleto:
            rows.append([mes_label, nome, valor, "-", "-", venc_label, data_reg])

    return rows


async def register_to_sheet(
    month: int, year: int, expenses: List[Dict]
) -> Dict[str, Any]:
    """
    Registra despesas do mes na planilha Historico do Google Sheets.

    Args:
        month: 1-12
        year: ano (ex: 2026)
        expenses: [{nome, valor, vencimento_dia, pix?, boleto?, nota?}, ...]

    Returns:
        {success, rows_added, sheet_url, headers_written}
    """
    from integrations.google_sheets import GoogleSheets

    sheets = GoogleSheets()

    # Garantir que a aba existe
    sheet_info = await sheets.get_or_create_sheet(SPREADSHEET_ID, SHEET_NAME)
    headers_written = False

    # Se aba foi recem-criada OU primeira linha esta vazia, escreve headers
    if sheet_info.get("created"):
        await sheets.append_rows(
            SPREADSHEET_ID,
            SHEET_NAME,
            [HEADERS],
            value_input_option="RAW",
        )
        headers_written = True
    else:
        try:
            first_row = await sheets.read_range(
                SPREADSHEET_ID, f"{SHEET_NAME}!A1:G1"
            )
            if not first_row or not first_row[0] or not first_row[0][0]:
                await sheets.append_rows(
                    SPREADSHEET_ID,
                    SHEET_NAME,
                    [HEADERS],
                    value_input_option="RAW",
                )
                headers_written = True
        except Exception as e:
            logger.warning(f"Erro ao ler primeira linha (segue assim mesmo): {e}")

    # Compor rows e append
    rows = _build_rows(month, year, expenses)
    if not rows:
        return {
            "success": True,
            "rows_added": 0,
            "headers_written": headers_written,
            "sheet_url": f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit",
            "message": "Nenhuma despesa para registrar",
        }

    result = await sheets.append_rows(SPREADSHEET_ID, SHEET_NAME, rows)
    updates = result.get("updates", {})
    rows_added = updates.get("updatedRows", len(rows))

    logger.info(
        f"payment_history: {rows_added} rows adicionadas em {SHEET_NAME} "
        f"({MESES[month]}/{year})"
    )

    return {
        "success": True,
        "rows_added": rows_added,
        "headers_written": headers_written,
        "sheet_url": f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit",
        "month": month,
        "year": year,
        "month_name": MESES[month],
    }
