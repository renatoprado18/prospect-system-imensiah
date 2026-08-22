#!/usr/bin/env python3
"""
ENSAIO do mutirão de merge — roda o caminho REAL e NÃO escreve no Google.

POR QUE EXISTE (22/08/2026). Em 22/08 o mutirão parou no primeiro grupo porque o
merge **fabricava no Google a duplicata que vinha limpar**: ao fundir as três
"Bettina Berman", 2 fichas foram apagadas e 4 criadas. O conserto (`962a389`)
adicionou `buscar_ficha_existente` — procurar antes de criar. O gate que ficou no
board é este: *provar o caminho num teste que não seja a base de produção* antes
de soltar os 111 grupos restantes.

O QUE ESTE ENSAIO FAZ. Roda `merge_par` — a função real, o mesmo caminho do
mutirão — contra o banco LOCAL, com o módulo do Google trocado por um ESPIÃO que
deixa passar toda LEITURA e intercepta toda ESCRITA. O placar responde a única
pergunta que importa:

    quantos CREATE aconteceriam?

CREATE > 0 = o gerador de duplicata continua ligado e o mutirão não pode rodar.
CREATE = 0 = todo merge encontra a ficha que já existe e só atualiza/apaga.

POR QUE NÃO REIMPLEMENTEI A LÓGICA. Um simulador que "calcula o que aconteceria"
mede a si mesmo, não o mutirão. Aqui o código exercitado é `merge_par` →
`propagate_merge_to_google` → `_achar_ou_criar`, linha por linha, com as buscas
reais na People API. Só as três escritas são interceptadas.
[[feedback_medir_o_consumidor_certo]]

FIDELIDADE DO `update`. O espião não devolve `True` cego: `update_google_contact`
faz um GET antes do PATCH, e o GET é leitura. O espião repete esse GET — se a
ficha responde 200 com etag, o update REAL teria sucesso; se não, o caminho real
cairia no create, e é isso que o placar precisa registrar. Devolver `True` de
graça esconderia justamente os creates que este ensaio veio contar.

⚠️ O BANCO LOCAL É ALTERADO. O merge local acontece de verdade (é o que faz o
caminho seguir até a propagação). O local é cópia descartável: rode
`./dev.sh sync` depois. A guarda abaixo recusa rodar em prod.

Uso:
    DB_TARGET=local python3 scripts/ensaio_merge_google.py [--limit N]
"""
import argparse
import asyncio
import os
import sys
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).parent.parent
env_path = _ROOT / '.env'
if env_path.exists():
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key, value.strip('"').strip("'"))

sys.path.insert(0, str(_ROOT / 'app'))
sys.stdout.reconfigure(line_buffering=True)

import httpx  # noqa: E402

import integrations.google_contacts as google_real  # noqa: E402
from services.duplicados import (  # noqa: E402
    encontrar_duplicados, escolher_sobrevivente, merge_par,
)

GOOGLE_PEOPLE_API = "https://people.googleapis.com/v1"


# ─────────────────────────── guarda de alvo ───────────────────────────
# Sem isto, um `DB_TARGET=prod` distraído funde de verdade os 111 grupos que o
# ensaio existe para NÃO tocar. O alvo é declarado, nunca deduzido
# ([[reference_db_target_protocol]]).
def _exigir_banco_local() -> None:
    alvo = (os.getenv("DB_TARGET") or "").strip().lower()
    if alvo != "local":
        sys.exit(
            f"[ensaio] DB_TARGET={alvo!r} — este script SÓ roda em local.\n"
            f"         O merge apaga linhas de verdade; em prod isso seria o\n"
            f"         mutirão, não o ensaio. Use: DB_TARGET=local"
        )


class EspiaoGoogle:
    """Módulo do Google com as escritas interceptadas e as leituras intactas.

    `__getattr__` delega ao módulo real tudo que não está sobrescrito — inclusive
    `buscar_ficha_existente`, que é a função sob julgamento e precisa rodar de
    verdade contra a People API.
    """

    def __init__(self, real):
        self._real = real
        self.registro = []          # (acao, conta, rid_ou_nome)
        self.placar = Counter()

    def __getattr__(self, nome):
        return getattr(self._real, nome)

    async def _ficha_responde(self, access_token: str, resource_name: str) -> bool:
        """O mesmo GET que `update_google_contact` faz antes do PATCH."""
        rid = (resource_name or "").split("people/")[-1]
        if not rid:
            return False
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(
                    f"{GOOGLE_PEOPLE_API}/people/{rid}",
                    headers={"Authorization": f"Bearer {access_token}"},
                    params={"personFields": "metadata"},
                )
                return r.status_code == 200 and bool(r.json().get("etag"))
        except Exception:
            return False

    async def create_google_contact(self, access_token, contact_data, *a, **kw):
        nome = (contact_data or {}).get("nome") or "?"
        self.registro.append(("CREATE", nome, ""))
        self.placar["create"] += 1
        return "people/ENSAIO_NAO_CRIADO"

    async def update_google_contact(self, access_token, resource_name, contact_data,
                                    *a, **kw):
        existe = await self._ficha_responde(access_token, resource_name)
        nome = (contact_data or {}).get("nome") or "?"
        self.registro.append(("UPDATE" if existe else "UPDATE_FALHARIA",
                              nome, resource_name))
        self.placar["update" if existe else "update_falharia"] += 1
        return existe

    async def delete_google_contact(self, access_token, resource_name, *a, **kw):
        self.registro.append(("DELETE", "", resource_name))
        self.placar["delete"] += 1
        return True


async def executar(limite: int) -> int:
    espiao = EspiaoGoogle(google_real)

    resultado = encontrar_duplicados(threshold=0.9, limit=limite)
    pares = [d for d in resultado.get("duplicates", []) if d.get("score", 0) >= 0.9]

    print("=" * 68)
    print("ENSAIO DO MUTIRÃO DE MERGE — nada é escrito no Google")
    print("=" * 68)
    print(f"pares com score ≥ 0.9: {len(pares)} (de {resultado.get('total')} no total)")
    if not pares:
        print("\nNada a ensaiar.")
        return 0

    stats = Counter()
    por_par = []

    for i, dup in enumerate(pares, 1):
        c1, c2 = dup.get("contact1", {}), dup.get("contact2", {})
        keep_id, merge_id = escolher_sobrevivente(c1, c2)
        antes = Counter(espiao.placar)

        rotulo = f"{c1.get('nome')} ⟷ {c2.get('nome')}"
        try:
            r = await merge_par(keep_id, merge_id, google_contacts_module=espiao)
            if r.get("error"):
                stats["pulado"] += 1
                print(f"[{i:3d}/{len(pares)}] {rotulo[:44]:44s} PULADO: {r['error'][:40]}")
                continue
            prop = r.get("google_propagation")
            if prop and prop.get("error"):
                stats["propagacao_falhou"] += 1
        except Exception as e:                                   # noqa: BLE001
            stats["erro"] += 1
            print(f"[{i:3d}/{len(pares)}] {rotulo[:44]:44s} ERRO: {type(e).__name__}: {e}")
            continue

        stats["fundido"] += 1
        delta = Counter(espiao.placar)
        delta.subtract(antes)
        delta = {k: v for k, v in delta.items() if v}
        por_par.append((rotulo, delta))
        marca = "🔴" if delta.get("create") else "  "
        print(f"[{i:3d}/{len(pares)}] {marca} {rotulo[:44]:44s} {delta}")

    print("\n" + "=" * 68)
    print("PLACAR")
    print("=" * 68)
    print(f"  pares fundidos (local):     {stats['fundido']}")
    print(f"  pulados:                    {stats['pulado']}")
    print(f"  erros:                      {stats['erro']}")
    print(f"  propagação falhou:          {stats['propagacao_falhou']}")
    print()
    print(f"  🔴 CREATE (ficha nova):      {espiao.placar['create']}")
    print(f"     UPDATE (ficha achada):    {espiao.placar['update']}")
    print(f"     UPDATE que falharia:      {espiao.placar['update_falharia']}")
    print(f"     DELETE (absorvida):       {espiao.placar['delete']}")

    criadores = [(r, d) for r, d in por_par if d.get("create")]
    if criadores:
        print(f"\n🔴 {len(criadores)} par(es) ainda CRIARIAM ficha no Google:")
        for rotulo, delta in criadores[:15]:
            print(f"     {rotulo[:52]:52s} {delta}")
        if len(criadores) > 15:
            print(f"     … e mais {len(criadores) - 15}")
        print("\n   VEREDITO: o gerador de duplicata continua ligado.")
        print("   NÃO soltar o mutirão nos 111 grupos.")
        return 1

    print("\n✅ VEREDITO: zero CREATE — todo merge encontrou a ficha que já existia.")
    print("   O caminho está provado fora de produção.")
    print("\n⚠️  O banco LOCAL foi alterado pelo ensaio. Rode: ./dev.sh sync")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200,
                    help="teto de pares a ensaiar (default 200)")
    args = ap.parse_args()
    _exigir_banco_local()
    raise SystemExit(asyncio.run(executar(args.limit)))
