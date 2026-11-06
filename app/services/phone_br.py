"""Normalização de telefone para o DESTINATÁRIO de um envio (WhatsApp/Evolution).

Isto NÃO é o mesmo problema de `services.contact_identity.canonical_br_phone`,
que normaliza para COMPARAR duas fichas. Aqui o resultado vira o `number` que
vai no fio para a Evolution API, e o custo do erro é uma mensagem que não chega
sem ninguém ser avisado — a Evolution aceita um número malformado e devolve
sucesso.

──────────────────────────────────────────────────────────────────────────────
**POR QUE ESTE MÓDULO EXISTE (06/11/2026).** Havia duas normalizações no repo,
as duas erradas de maneiras diferentes:

1. `integrations/whatsapp.py::_normalize_phone` só acertava DDD de São Paulo:

       if len(digits) == 11 and digits.startswith('11'): digits = '55' + digits
       elif len(digits) == 10: digits = '5511' + digits
       elif len(digits) == 9:  digits = '55119' + digits

   O ramo de 11 dígitos exigia `startswith('11')`, então `41991064663`
   (Curitiba) não casava com `if` nenhum e saía **sem o `55`**. Medido na base:
   dos 713 telefones de 11 dígitos, 600 têm o primeiro par diferente de `11`.
   Os outros dois ramos são piores que inúteis: o de 10 dígitos prefixa `5511`
   sobre um número que **já tem DDD** (`5137782929` de Porto Alegre virava
   `55115137782929`, 14 dígitos), e o de 9 dígitos prefixa `55119` sobre um
   celular que já começa em 9 (nono dígito duplicado).

2. `integrations/evolution_api.py` repetia, em 7 lugares, a regra

       if not phone_clean.startswith('55') and len(phone_clean) <= 11:
           phone_clean = '55' + phone_clean

   que acerta o DDD brasileiro mas **estrangeiriza ao contrário**: a base tem
   número internacional de 11 dígitos (`+33624553002` França,
   `+14157588419` EUA, `+12122706000`), e prefixar `55` neles manda a
   mensagem para um brasileiro diferente. Hoje esses números funcionam porque
   `len <= 11` casa e… não, casa sim — eles viram `5533624553002`. O defeito
   existe nas duas implementações, em sentidos opostos.

**A REGRA AQUI DECIDE POR FORMA, NÃO POR TAMANHO.** Um número de 11 dígitos só
ganha `55` se for reconhecível como celular brasileiro: DDD na lista da Anatel
**e** nono dígito `9` na terceira posição. É isso que separa `41991064663` (vira
`5541991064663`) de `12122706000` (DDD 12 existe, mas o terceiro dígito é `1` —
celular brasileiro nenhum começa assim; fica intocado). Quando a forma não
permite afirmar, **devolve os dígitos como entraram** — nunca inventa DDI nem
DDD. Número que não dá para normalizar é número que o chamador precisa ver como
está, não um palpite que viaja silencioso até a Evolution.
"""
from __future__ import annotations

import re
from typing import Any

# DDDs em uso no Brasil (Plano Geral de Códigos Nacionais / Anatel). A lista
# existe porque é ela que distingue "11 dígitos brasileiros" de "11 dígitos
# internacionais": sem ela, +33 (França) e +1 (EUA) ganham um `55` na frente.
DDD_VALIDOS = frozenset({
    11, 12, 13, 14, 15, 16, 17, 18, 19,              # SP
    21, 22, 24,                                      # RJ
    27, 28,                                          # ES
    31, 32, 33, 34, 35, 37, 38,                      # MG
    41, 42, 43, 44, 45, 46,                          # PR
    47, 48, 49,                                      # SC
    51, 53, 54, 55,                                  # RS
    61,                                              # DF/GO
    62, 64,                                          # GO
    63,                                              # TO
    65, 66,                                          # MT
    67,                                              # MS
    68,                                              # AC
    69,                                              # RO
    71, 73, 74, 75, 77,                              # BA
    79,                                              # SE
    81, 87,                                          # PE
    82,                                              # AL
    83,                                              # PB
    84,                                              # RN
    85, 88,                                          # CE
    86, 89,                                          # PI
    91, 93, 94,                                      # PA
    92, 97,                                          # AM
    95,                                              # RR
    96,                                              # AP
    98, 99,                                          # MA
})

# DDD padrão para número LOCAL (sem DDD nenhum). Não há como inferir: 8 ou 9
# dígitos não carregam a informação. Mantém o default histórico do
# `_normalize_phone` (São Paulo) em vez de montar um número curto que a
# Evolution aceita e não entrega.
DDD_PADRAO = "11"

# Assinante fixo brasileiro começa em 2-5; celular, em 9 (pós-Anatel) ou 6-9
# (pré-nono-dígito). Usado só como teste de FORMA.
_PREFIXO_FIXO = frozenset("2345")


def _ddd_valido(par: str) -> bool:
    return par.isdigit() and int(par) in DDD_VALIDOS


def normalize_br_phone(phone: Any) -> str:
    """Devolve o `number` pronto para a Evolution API (só dígitos, com DDI).

        '41991064663'          -> '5541991064663'   (era '41991064663', sem DDI)
        '(41) 99106-4663'      -> '5541991064663'
        '+55 41 99106-4663'    -> '5541991064663'   (já tinha DDI)
        '11984153337'          -> '5511984153337'   (o caso que já funcionava)
        '(011) 3258-8699'      -> '551132588699'    (prefixo de tronco 0)
        '1133334444'           -> '551133334444'    (fixo com DDD)
        '981818611'            -> '5511981818611'   (local, assume DDD 11)
        '33334444'            ->  '551133334444'    (fixo local, assume DDD 11)
        '+33624553002'         -> '33624553002'     (França — NÃO leva 55)
        '+1 (415) 758-8419'    -> '14157588419'     (EUA — NÃO leva 55)
        ''                     -> ''
    """
    digits = re.sub(r"\D", "", str(phone or ""))
    if not digits:
        return ""

    candidatos = [digits]
    # Prefixo de tronco interurbano ("(011) 3258-8699", 346 ocorrências na
    # base). Entra como CANDIDATO, não como substituição: se o que sobra não
    # for um número brasileiro inteiro, o 0 era parte de outra coisa e tirá-lo
    # inventa dado.
    if len(digits) in (11, 12) and digits[0] == "0" and _ddd_valido(digits[1:3]):
        candidatos.append(digits[1:])

    for d in candidatos:
        # Já em E.164 brasileiro: 55 + DDD(2) + assinante(9 celular | 8 fixo).
        if len(d) in (12, 13) and d.startswith("55") and _ddd_valido(d[2:4]):
            return d

        # Celular nacional sem DDI: DDD(2) + 9 + 8 dígitos.
        if len(d) == 11 and _ddd_valido(d[:2]) and d[2] == "9":
            return "55" + d

        # Fixo nacional sem DDI: DDD(2) + assinante(8) começando em 2-5.
        if len(d) == 10 and _ddd_valido(d[:2]) and d[2] in _PREFIXO_FIXO:
            return "55" + d

        # Celular LOCAL, sem DDD: 9 dígitos começando em 9. Único caso em que
        # o DDD é assumido, e só porque o `_normalize_phone` original já o
        # assumia (de forma quebrada: `'55119' + digits` duplicava o nono
        # dígito e produzia 14 dígitos). Fixo de 8 dígitos NÃO entra aqui de
        # propósito: o código antigo não tinha esse ramo, e a base tem campo
        # corrompido por notação científica do Excel (`5.51139E+16` → oito
        # dígitos `55113916`) que ganharia um DDD e viraria um número
        # plausível e errado.
        if len(d) == 9 and d[0] == "9":
            return "55" + DDD_PADRAO + d

    # Não dá para afirmar o que é: internacional, ramal, celular de SP no
    # formato pré-2012 (`(011) 8396-0550` — inserir o nono dígito aqui é
    # fabricar um número DIFERENTE, e a mensagem iria para um estranho), dado
    # sujo. Devolve como entrou — o chamador precisa ver o número, não um
    # palpite que viaja silencioso até a Evolution.
    return digits
