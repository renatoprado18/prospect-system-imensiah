"""Integracoes externas do Cost Tracker (Bloco B).

Cada funcao busca o custo de um mes completo de um provedor especifico.
Retorna dict {amount_usd, metrics, notes} ou levanta excecao se falhar.

Providers cobertos:
- anthropic: /v1/organizations/cost_report (Admin API key, lag de ~1 dia)
- railway: GraphQL usage query + precos Hobby aproximados
- vercel: tentado mas Hobby plan retorna costs_not_found (mantido $0)
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime
from decimal import Decimal
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

ANTHROPIC_ADMIN_URL = "https://api.anthropic.com/v1/organizations/cost_report"
RAILWAY_GRAPHQL_URL = "https://backboard.railway.app/graphql/v2"
VERCEL_API_BASE = "https://api.vercel.com"
HETZNER_API_BASE = "https://api.hetzner.cloud/v1"

# EUR->USD: Hetzner cobra em EUR pra locations EU. Taxa fixa aproximada
# (recalibrar anualmente, drift de ~5%/ano e aceitavel pro cost audit).
EUR_TO_USD = Decimal("1.08")

# Railway Hobby pricing aproximado (valores Anthropic $/unit-time)
# Source: dashboard Railway docs (docs.railway.com/reference/pricing). Pode estar
# desatualizado — refina via POST manual se nao bater com invoice.
RAILWAY_PRICES = {
    "CPU_USAGE_2": Decimal("0.000231"),       # $/vCPU-min
    "MEMORY_USAGE_GB": Decimal("0.000463"),   # $/GB-min
    "NETWORK_TX_GB": Decimal("0.05"),         # $/GB egress
    "DISK_USAGE_GB": Decimal("0.000231"),     # $/GB-min
    "BACKUP_USAGE_GB": Decimal("0.000231"),   # $/GB-min
    "EPHEMERAL_DISK_USAGE_GB": Decimal("0"),  # incluido
}
RAILWAY_BASE_FEE = Decimal("5.00")  # Hobby plan base/mes


def _iso_z(d: date) -> str:
    return f"{d.isoformat()}T00:00:00Z"


# ============================================================================
# Anthropic
# ============================================================================

def fetch_anthropic_cost(period_start: date, period_end: date) -> Dict:
    """Busca custo total do periodo via Admin API. Lag de ~24h apos fim do dia.

    Retorna {amount_usd, metrics, notes}. Levanta RuntimeError se sem token
    ou httpx.HTTPStatusError em falha de API.
    """
    api_key = (os.getenv("ANTHROPIC_ADMIN_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_ADMIN_API_KEY nao configurada")

    # Anthropic API quer ending_at exclusivo (primeiro dia do mes seguinte)
    end_exclusive = (date(period_end.year, period_end.month + 1, 1)
                     if period_end.month < 12
                     else date(period_end.year + 1, 1, 1))

    params = {
        "starting_at": _iso_z(period_start),
        "ending_at": _iso_z(end_exclusive),
        "limit": 31,
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(ANTHROPIC_ADMIN_URL, params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    total_cents = Decimal("0")
    days = 0
    for day in data.get("data", []):
        days += 1
        for r in day.get("results", []):
            total_cents += Decimal(str(r.get("amount", "0")))

    # Anthropic cost_report retorna minor units (centavos) apesar do
    # currency:USD — observado empirically via reconciliacao com Console
    # (07/05/2026: API somava $1850 mas Console mostrava $18.51 = 100x menos).
    total_usd = (total_cents / Decimal("100")).quantize(Decimal("0.01"))

    return {
        "amount_usd": float(total_usd),
        "metrics": {
            "days_returned": days,
            "currency": "USD",
            "raw_cents": float(total_cents),
            "source": "anthropic_admin_api",
        },
        "notes": f"Auto via Anthropic Admin API ({days} dias de cost_report, /100 cents->USD)",
    }


def fetch_anthropic_daily(target_date: date) -> float:
    """Custo Anthropic do dia UTC `target_date` em USD. Cents do cost_report /100.
    Lag de ~24h — chamar pra D-1 ou anterior. Devolve 0.0 se sem token ou sem dados."""
    api_key = (os.getenv("ANTHROPIC_ADMIN_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_ADMIN_API_KEY nao configurada")

    from datetime import timedelta
    next_day = target_date + timedelta(days=1)
    params = {
        "starting_at": _iso_z(target_date),
        "ending_at": _iso_z(next_day),
        "limit": 2,
    }
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(ANTHROPIC_ADMIN_URL, params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    total_cents = Decimal("0")
    for day in data.get("data", []):
        for r in day.get("results", []):
            total_cents += Decimal(str(r.get("amount", "0")))
    return float((total_cents / Decimal("100")).quantize(Decimal("0.01")))


# ============================================================================
# Railway
# ============================================================================

def _railway_query(query: str, variables: Optional[Dict] = None) -> Dict:
    """GraphQL POST com auth Bearer. Levanta em erro."""
    api_key = (os.getenv("RAILWAY_API_TOKEN") or "").strip()
    if not api_key:
        raise RuntimeError("RAILWAY_API_TOKEN nao configurada")

    body = {"query": query}
    if variables:
        body["variables"] = variables
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(RAILWAY_GRAPHQL_URL, json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    if data.get("errors"):
        raise RuntimeError(f"Railway GraphQL: {data['errors']}")
    return data.get("data", {})


def fetch_railway_cost(period_start: date, period_end: date) -> Dict:
    """Busca usage Railway do periodo + multiplica por precos Hobby.

    Limitacao conhecida: projetos deletados desde o periodo nao aparecem
    no historico — pode subestimar bills passadas. Manual POST overrides.
    """
    # Lista projetos pra mapear id -> name (best-effort, falha nao bloqueia)
    projects_map: Dict[str, str] = {}
    try:
        d = _railway_query("{ projects { edges { node { id name } } } }")
        for e in (d.get("projects") or {}).get("edges") or []:
            n = e.get("node") or {}
            if n.get("id"):
                projects_map[n["id"]] = n.get("name") or n["id"][:8]
    except Exception as e:
        logger.warning(f"Railway projects map falhou: {e}")

    # Historic usage do periodo
    end_exclusive = (date(period_end.year, period_end.month + 1, 1)
                     if period_end.month < 12
                     else date(period_end.year + 1, 1, 1))
    measurements = list(RAILWAY_PRICES.keys())
    measurements_str = ", ".join(measurements)
    query = f"""
    {{
      usage(
        measurements: [{measurements_str}],
        startDate: "{_iso_z(period_start)}",
        endDate: "{_iso_z(end_exclusive)}"
      ) {{
        measurement
        value
        tags {{ projectId }}
      }}
    }}
    """
    d = _railway_query(query)
    items: List[Dict] = d.get("usage") or []

    # Agrega por projeto + total
    per_project: Dict[str, Dict[str, float]] = {}
    for it in items:
        pid = (it.get("tags") or {}).get("projectId") or "unknown"
        m = it.get("measurement")
        v = float(it.get("value") or 0)
        if not m:
            continue
        per_project.setdefault(pid, {})[m] = v

    # Calcula custo
    project_costs: Dict[str, float] = {}
    total_usage = Decimal("0")
    metric_totals: Dict[str, float] = {m: 0.0 for m in measurements}
    for pid, ms in per_project.items():
        proj_cost = Decimal("0")
        for m, v in ms.items():
            price = RAILWAY_PRICES.get(m, Decimal("0"))
            proj_cost += Decimal(str(v)) * price
            metric_totals[m] = metric_totals.get(m, 0.0) + v
        pname = projects_map.get(pid, pid[:8])
        project_costs[pname] = float(proj_cost.quantize(Decimal("0.01")))
        total_usage += proj_cost

    total = (total_usage + RAILWAY_BASE_FEE).quantize(Decimal("0.01"))

    return {
        "amount_usd": float(total),
        "metrics": {
            "base_fee_usd": float(RAILWAY_BASE_FEE),
            "usage_cost_usd": float(total_usage.quantize(Decimal("0.01"))),
            "per_project_usd": project_costs,
            "raw_totals": metric_totals,
            "source": "railway_graphql",
            "warning": (
                "Projetos deletados nao aparecem no historico — pode subestimar."
            ),
        },
        "notes": (
            f"Auto via Railway GraphQL (base ${RAILWAY_BASE_FEE} + "
            f"${total_usage:.2f} usage em {len(per_project)} projetos)"
        ),
    }


# ============================================================================
# Hetzner Cloud
# ============================================================================

def _hetzner_price_to_usd(price_block: Dict) -> float:
    """price_block = {"net": "4.20", "gross": "4.99", "currency": "EUR"}.
    Retorna gross em USD (converte EUR se necessario)."""
    try:
        v = Decimal(str(price_block.get("gross") or 0))
    except Exception:
        return 0.0
    currency = (price_block.get("currency") or "EUR").upper()
    if currency == "EUR":
        v = v * EUR_TO_USD
    return float(v.quantize(Decimal("0.01")))


def fetch_hetzner_cost(period_start: date, period_end: date) -> Dict:
    """Hetzner Cloud: lista servers ativos + multiplica por price_monthly + IPv4.

    Hetzner cobra mensalidade fixa (independente de uso). period_start/end nao
    sao usados — sempre retorna custo mensal corrente. EUR convertido pra USD.
    """
    api_token = (os.getenv("HETZNER_API_TOKEN") or "").strip()
    if not api_token:
        raise RuntimeError("HETZNER_API_TOKEN nao configurada")

    headers = {"Authorization": f"Bearer {api_token}"}
    with httpx.Client(timeout=30.0) as client:
        srv_resp = client.get(f"{HETZNER_API_BASE}/servers", headers=headers)
        srv_resp.raise_for_status()
        servers = srv_resp.json().get("servers") or []

        if not servers:
            return {
                "amount_usd": 0.0,
                "metrics": {"servers": [], "source": "hetzner_api"},
                "notes": "Hetzner sem servidores ativos.",
            }

        pr_resp = client.get(f"{HETZNER_API_BASE}/pricing", headers=headers)
        pr_resp.raise_for_status()
        pricing = pr_resp.json().get("pricing") or {}

    # Mapa (server_type_name, location_name) -> monthly USD
    type_loc_price: Dict[tuple, float] = {}
    for st in pricing.get("server_types") or []:
        name = st.get("name")
        for p in st.get("prices") or []:
            loc = p.get("location")
            usd = _hetzner_price_to_usd(p.get("price_monthly") or {})
            if name and loc and usd > 0:
                type_loc_price[(name, loc)] = usd

    # Preco IPv4 mensal (assumindo igual em todas locations — pega o primeiro)
    ipv4_monthly_usd = 0.60
    for ip in pricing.get("primary_ips") or []:
        if (ip.get("type") or "").lower() == "ipv4":
            for p in ip.get("prices") or []:
                v = _hetzner_price_to_usd(p.get("price_monthly") or {})
                if v > 0:
                    ipv4_monthly_usd = v
                    break
            break

    servers_detail: List[Dict] = []
    total = Decimal("0")
    for s in servers:
        st_name = (s.get("server_type") or {}).get("name", "unknown")
        loc_name = ((s.get("datacenter") or {}).get("location") or {}).get("name", "")
        srv_price = type_loc_price.get((st_name, loc_name), 0.0)
        primary_ipv4 = (s.get("public_net") or {}).get("ipv4")
        has_ipv4 = bool(primary_ipv4 and primary_ipv4.get("ip"))
        ipv4_cost = ipv4_monthly_usd if has_ipv4 else 0.0
        srv_total = srv_price + ipv4_cost
        servers_detail.append({
            "name": s.get("name"),
            "type": st_name,
            "location": loc_name,
            "server_usd": round(srv_price, 2),
            "ipv4_usd": round(ipv4_cost, 2),
            "monthly_usd": round(srv_total, 2),
        })
        total += Decimal(str(srv_total))

    total_usd = float(total.quantize(Decimal("0.01")))
    return {
        "amount_usd": total_usd,
        "metrics": {
            "servers": servers_detail,
            "ipv4_price_monthly_usd": ipv4_monthly_usd,
            "eur_to_usd_rate": float(EUR_TO_USD),
            "source": "hetzner_api",
        },
        "notes": f"Auto via Hetzner Cloud API ({len(servers)} server(s), EUR->USD@{EUR_TO_USD})",
    }


# ============================================================================
# Vercel (no-op por enquanto)
# ============================================================================

def fetch_vercel_cost(period_start: date, period_end: date) -> Dict:
    """Hobby plan retorna costs_not_found — sempre $0. Quando upgrade pra Pro,
    /v1/billing/charges retorna FOCUS-format JSONL com cobranças reais."""
    api_token = (os.getenv("VERCEL_API_TOKEN") or "").strip()
    if not api_token:
        return {"amount_usd": 0.0, "metrics": {"plan": "no_token"}, "notes": "Sem token"}

    end_exclusive = (date(period_end.year, period_end.month + 1, 1)
                     if period_end.month < 12
                     else date(period_end.year + 1, 1, 1))
    params = {
        "from": _iso_z(period_start),
        "to": _iso_z(end_exclusive),
    }
    headers = {"Authorization": f"Bearer {api_token}"}
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(f"{VERCEL_API_BASE}/v1/billing/charges",
                              params=params, headers=headers)
            if resp.status_code == 404 or "costs_not_found" in resp.text:
                return {
                    "amount_usd": 0.0,
                    "metrics": {"plan": "hobby_no_charges", "source": "vercel_api"},
                    "notes": "Hobby plan free tier (Vercel API confirmou no charges)",
                }
            resp.raise_for_status()
            # JSONL response (uma linha por charge)
            charges = [
                __import__("json").loads(line) for line in resp.text.splitlines() if line.strip()
            ]
    except Exception as e:
        logger.warning(f"Vercel API falhou: {e} — assumindo $0")
        return {
            "amount_usd": 0.0,
            "metrics": {"plan": "unknown", "error": str(e)[:200]},
            "notes": "Vercel API indisponivel — assumindo $0",
        }

    total = sum(float(c.get("BilledCost") or 0) for c in charges)
    return {
        "amount_usd": round(total, 2),
        "metrics": {"charges_count": len(charges), "source": "vercel_api"},
        "notes": f"Auto via Vercel /v1/billing/charges ({len(charges)} charges)",
    }
