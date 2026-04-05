"""
Veiculos Service - Controle de Manutencao de Veiculos
"""
from datetime import datetime, date, timedelta
from typing import List, Dict, Optional, Any
from dateutil.relativedelta import relativedelta
import json

from database import get_db


def get_veiculo(veiculo_id: int) -> Optional[Dict]:
    """Busca veiculo por ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM veiculos WHERE id = %s", (veiculo_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_veiculo_por_placa(placa: str) -> Optional[Dict]:
    """Busca veiculo por placa"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM veiculos WHERE placa = %s", (placa.upper(),))
        row = cursor.fetchone()
        return dict(row) if row else None


def listar_veiculos(apenas_ativos: bool = True) -> List[Dict]:
    """Lista todos os veiculos"""
    with get_db() as conn:
        cursor = conn.cursor()
        sql = "SELECT * FROM veiculos"
        if apenas_ativos:
            sql += " WHERE ativo = TRUE"
        sql += " ORDER BY apelido, marca, modelo"
        cursor.execute(sql)
        return [dict(row) for row in cursor.fetchall()]


def criar_veiculo(dados: Dict) -> Dict:
    """Cria um novo veiculo"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO veiculos (
                placa, apelido, marca, modelo, versao, ano_fabricacao, ano_modelo,
                cor, combustivel, renavam, chassi, motor, potencia, km_atual,
                km_atualizado_em, foto_url, proprietario, data_aquisicao, observacoes
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
        """, (
            dados.get('placa', '').upper(),
            dados.get('apelido'),
            dados.get('marca'),
            dados.get('modelo'),
            dados.get('versao'),
            dados.get('ano_fabricacao'),
            dados.get('ano_modelo'),
            dados.get('cor'),
            dados.get('combustivel'),
            dados.get('renavam'),
            dados.get('chassi'),
            dados.get('motor'),
            dados.get('potencia'),
            dados.get('km_atual', 0),
            datetime.now() if dados.get('km_atual') else None,
            dados.get('foto_url'),
            dados.get('proprietario'),
            dados.get('data_aquisicao'),
            dados.get('observacoes')
        ))
        veiculo = dict(cursor.fetchone())
        conn.commit()
        return veiculo


def atualizar_km(veiculo_id: int, km: int) -> Dict:
    """Atualiza quilometragem do veiculo"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE veiculos
            SET km_atual = %s, km_atualizado_em = NOW(), atualizado_em = NOW()
            WHERE id = %s
            RETURNING *
        """, (km, veiculo_id))
        veiculo = cursor.fetchone()
        conn.commit()
        return dict(veiculo) if veiculo else None


# ==================== ITENS DE MANUTENCAO ====================

def get_itens_manutencao(veiculo_id: int) -> List[Dict]:
    """Lista itens do plano de manutencao do veiculo"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM veiculo_itens_manutencao
            WHERE veiculo_id = %s AND ativo = TRUE
            ORDER BY ordem, categoria, item
        """, (veiculo_id,))
        return [dict(row) for row in cursor.fetchall()]


def criar_item_manutencao(veiculo_id: int, dados: Dict) -> Dict:
    """Cria um item no plano de manutencao"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO veiculo_itens_manutencao (
                veiculo_id, categoria, item, descricao, intervalo_km,
                intervalo_meses, tipo_acao, notas, ordem, notas_fabricante
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
        """, (
            veiculo_id,
            dados.get('categoria'),
            dados.get('item'),
            dados.get('descricao'),
            dados.get('intervalo_km'),
            dados.get('intervalo_meses'),
            dados.get('tipo_acao', 'substituir'),
            dados.get('notas'),
            dados.get('ordem', 0),
            dados.get('notas_fabricante')
        ))
        item = dict(cursor.fetchone())
        conn.commit()
        return item


def importar_plano_manutencao_prado(veiculo_id: int) -> int:
    """Importa plano de manutencao padrao do Land Cruiser Prado com especificacoes Toyota"""
    plano = [
        # COMPONENTES BASICOS DO MOTOR
        {
            "categoria": "Motor",
            "item": "Correia de Distribuicao",
            "intervalo_km": 150000,
            "tipo_acao": "substituir",
            "ordem": 1,
            "notas_fabricante": "OBRIGATORIO substituir a cada 150.000km. Falha pode causar danos graves ao motor. Trocar junto: tensor e polias."
        },
        {
            "categoria": "Motor",
            "item": "Folga das Valvulas",
            "intervalo_km": 40000,
            "intervalo_meses": 48,
            "tipo_acao": "inspecionar",
            "ordem": 2,
            "notas_fabricante": "Inspecionar folga e ajustar se necessario. Motor 1KZ-TE: Admissao 0.20-0.30mm / Escape 0.35-0.45mm (motor frio)."
        },
        {
            "categoria": "Motor",
            "item": "Correias de Acionamento",
            "intervalo_km": 20000,
            "intervalo_meses": 24,
            "tipo_acao": "inspecionar",
            "ordem": 3,
            "notas_fabricante": "Verificar rachaduras, desgaste e tensao. Substituir se apresentar danos visiveis ou a cada 100.000km."
        },
        {
            "categoria": "Motor",
            "item": "Oleo do Motor",
            "intervalo_km": 5000,
            "intervalo_meses": 6,
            "tipo_acao": "substituir",
            "ordem": 4,
            "notas_fabricante": "OLEO RECOMENDADO: Toyota Genuine Diesel Oil 15W-40 ou equivalente API CF-4/SG. CAPACIDADE: 7.0 litros (com filtro). Em uso severo (estradas de terra, reboque), trocar a cada 2.500km."
        },
        {
            "categoria": "Motor",
            "item": "Filtro de Oleo",
            "intervalo_km": 5000,
            "intervalo_meses": 6,
            "tipo_acao": "substituir",
            "ordem": 5,
            "notas_fabricante": "Substituir junto com o oleo. Filtro Toyota Genuine ou equivalente. Torque da tampa: 25 N.m."
        },
        {
            "categoria": "Motor",
            "item": "Sistema de Arrefecimento",
            "intervalo_km": 40000,
            "intervalo_meses": 24,
            "tipo_acao": "inspecionar",
            "ordem": 6,
            "notas_fabricante": "Verificar: nivel do reservatorio, vazamentos nas mangueiras, estado do radiador, funcionamento da ventoinha e termostato."
        },
        {
            "categoria": "Motor",
            "item": "Fluido de Arrefecimento",
            "intervalo_km": 80000,
            "tipo_acao": "substituir",
            "ordem": 7,
            "notas_fabricante": "FLUIDO: Toyota Super Long Life Coolant (vermelho/rosa) ou equivalente. CAPACIDADE: 10.4 litros. Mistura 50/50 com agua destilada. Drenar completamente antes de reabastecer."
        },
        {
            "categoria": "Motor",
            "item": "Tubos de Escapamento",
            "intervalo_km": 20000,
            "intervalo_meses": 12,
            "tipo_acao": "inspecionar",
            "ordem": 8,
            "notas_fabricante": "Verificar vazamentos, ferrugem, fixacao dos suportes e estado dos coxins de borracha."
        },

        # SISTEMA DE IGNICAO
        {
            "categoria": "Eletrica",
            "item": "Bateria",
            "intervalo_km": 10000,
            "intervalo_meses": 12,
            "tipo_acao": "inspecionar",
            "ordem": 9,
            "notas_fabricante": "Verificar nivel do eletrólito, terminais (limpar corrosao), tensao em repouso (12.4-12.7V). ESPECIFICACAO: 12V 90Ah (ex: Moura M90TD). Vida util media: 3-4 anos."
        },

        # SISTEMA DE COMBUSTIVEL
        {
            "categoria": "Combustivel",
            "item": "Filtro de Combustivel",
            "intervalo_km": 10000,
            "intervalo_meses": 12,
            "tipo_acao": "substituir",
            "ordem": 10,
            "notas_fabricante": "Filtro de combustivel diesel. Apos substituicao, sangrar sistema de combustivel para remover ar."
        },
        {
            "categoria": "Combustivel",
            "item": "Sedimentador de Agua",
            "intervalo_km": 10000,
            "intervalo_meses": 12,
            "tipo_acao": "inspecionar",
            "ordem": 11,
            "notas_fabricante": "Drenar agua acumulada pelo dreno na parte inferior. Se luz de advertencia acender, drenar imediatamente."
        },
        {
            "categoria": "Combustivel",
            "item": "Filtro de Ar",
            "intervalo_km": 20000,
            "intervalo_meses": 36,
            "tipo_acao": "substituir",
            "ordem": 12,
            "notas_fabricante": "Substituir elemento filtrante. Em condicoes de muita poeira, inspecionar a cada 5.000km e substituir se necessario."
        },
        {
            "categoria": "Combustivel",
            "item": "Fumaca do Motor",
            "intervalo_km": 40000,
            "intervalo_meses": 48,
            "tipo_acao": "inspecionar",
            "ordem": 13,
            "notas_fabricante": "Verificar coloracao da fumaca: preta (excesso combustivel), azul (queima de oleo), branca (vazamento de agua). Ajustar se necessario."
        },
        {
            "categoria": "Combustivel",
            "item": "Linhas de Combustivel",
            "intervalo_km": 20000,
            "intervalo_meses": 12,
            "tipo_acao": "inspecionar",
            "ordem": 14,
            "notas_fabricante": "Verificar vazamentos, rachaduras e conexoes das linhas de combustivel. Substituir se houver danos."
        },

        # CHASSI E CARROCARIA - FREIOS
        {
            "categoria": "Freios",
            "item": "Pedal e Freio de Estacionamento",
            "intervalo_km": 10000,
            "intervalo_meses": 6,
            "tipo_acao": "inspecionar",
            "ordem": 15,
            "notas_fabricante": "Verificar curso do pedal (folga livre e reserva), ajustar se necessario. Freio de mao: verificar numero de cliques (5-8 cliques normal)."
        },
        {
            "categoria": "Freios",
            "item": "Lonas e Tambor do Freio",
            "intervalo_km": 20000,
            "intervalo_meses": 12,
            "tipo_acao": "inspecionar",
            "ordem": 16,
            "notas_fabricante": "Espessura minima das lonas: 1.0mm. Verificar desgaste irregular, trincas nos tambores. Limite de desgaste do tambor: consultar especificacao."
        },
        {
            "categoria": "Freios",
            "item": "Discos e Pastilhas do Freio",
            "intervalo_km": 10000,
            "intervalo_meses": 6,
            "tipo_acao": "inspecionar",
            "ordem": 17,
            "notas_fabricante": "Espessura minima pastilhas: 1.0mm. Verificar espessura e empenamento dos discos. Espessura minima disco dianteiro: 28mm."
        },
        {
            "categoria": "Freios",
            "item": "Fluido de Freio",
            "intervalo_km": 40000,
            "intervalo_meses": 24,
            "tipo_acao": "substituir",
            "ordem": 18,
            "notas_fabricante": "FLUIDO: DOT 3 ou DOT 4. Substituir completamente (sangria). Verificar nivel no reservatorio: entre MIN e MAX."
        },
        {
            "categoria": "Freios",
            "item": "Fluido de Embreagem",
            "intervalo_km": 10000,
            "intervalo_meses": 6,
            "tipo_acao": "inspecionar",
            "ordem": 19,
            "notas_fabricante": "FLUIDO: DOT 3 ou DOT 4 (mesmo do freio). Verificar nivel e vazamentos no cilindro mestre e auxiliar."
        },
        {
            "categoria": "Freios",
            "item": "Mangueiras de Freio",
            "intervalo_km": 20000,
            "intervalo_meses": 12,
            "tipo_acao": "inspecionar",
            "ordem": 20,
            "notas_fabricante": "Verificar rachaduras, vazamentos, bolhas e desgaste por atrito. Substituir se houver qualquer dano visivel."
        },
        {
            "categoria": "Freios",
            "item": "Bomba de Vacuo Servo-freio",
            "intervalo_km": 200000,
            "tipo_acao": "substituir",
            "ordem": 21,
            "notas_fabricante": "Verificar funcionamento do servo-freio (pedal deve ficar mais leve com motor ligado). Substituir bomba de vacuo se apresentar ruido ou perda de eficiencia."
        },

        # DIRECAO
        {
            "categoria": "Direcao",
            "item": "Fluido Direcao Hidraulica",
            "intervalo_km": 10000,
            "intervalo_meses": 6,
            "tipo_acao": "inspecionar",
            "ordem": 22,
            "notas_fabricante": "FLUIDO: ATF Dexron II ou III. Verificar nivel com motor quente. Verificar vazamentos na bomba, mangueiras e caixa."
        },
        {
            "categoria": "Direcao",
            "item": "Volante e Caixa de Direcao",
            "intervalo_km": 10000,
            "intervalo_meses": 6,
            "tipo_acao": "inspecionar",
            "ordem": 23,
            "notas_fabricante": "Verificar folga no volante (max 30mm), ruidos na caixa de direcao, vazamentos. Verificar alinhamento da direcao."
        },

        # TRANSMISSAO
        {
            "categoria": "Transmissao",
            "item": "Lubrificacao Arvore Transmissao",
            "intervalo_km": 10000,
            "intervalo_meses": 6,
            "tipo_acao": "lubrificar",
            "ordem": 24,
            "notas_fabricante": "Engraxar cruzetas e juntas deslizantes com graxa a base de litio. Verificar folgas e vibracoes. Apertar parafusos."
        },
        {
            "categoria": "Transmissao",
            "item": "Coifas do Semi-eixo",
            "intervalo_km": 20000,
            "intervalo_meses": 24,
            "tipo_acao": "inspecionar",
            "ordem": 25,
            "notas_fabricante": "Verificar rachaduras, vazamento de graxa, fixacao das abracadeiras. Substituir imediatamente se houver dano."
        },
        {
            "categoria": "Transmissao",
            "item": "Juntas Esfericas e Coifas",
            "intervalo_km": 10000,
            "intervalo_meses": 6,
            "tipo_acao": "inspecionar",
            "ordem": 26,
            "notas_fabricante": "Verificar folgas nas juntas esfericas (balanco com veiculo suspenso), estado das coifas de protecao."
        },
        {
            "categoria": "Transmissao",
            "item": "Oleo do Diferencial",
            "intervalo_km": 40000,
            "intervalo_meses": 24,
            "tipo_acao": "substituir",
            "ordem": 27,
            "notas_fabricante": "OLEO: API GL-5 SAE 90 (dianteiro e traseiro). CAPACIDADE: Dianteiro 1.35L / Traseiro 2.8L. Em uso severo, trocar a cada 20.000km."
        },
        {
            "categoria": "Transmissao",
            "item": "Oleo Transmissao/Caixa Transferencia",
            "intervalo_km": 40000,
            "intervalo_meses": 48,
            "tipo_acao": "inspecionar",
            "ordem": 28,
            "notas_fabricante": "OLEO: API GL-4/GL-5 SAE 75W-90. Caixa manual: 2.6L / Caixa transferencia: 1.4L. Verificar nivel e vazamentos."
        },
        {
            "categoria": "Transmissao",
            "item": "Fluido Transmissao Automatica",
            "intervalo_km": 40000,
            "intervalo_meses": 24,
            "tipo_acao": "substituir",
            "ordem": 29,
            "notas_fabricante": "FLUIDO: Toyota ATF Type T-IV ou Dexron III. CAPACIDADE: 9.9L (troca total). Verificar nivel com motor quente em P."
        },

        # SUSPENSAO
        {
            "categoria": "Suspensao",
            "item": "Suspensoes Dianteira e Traseira",
            "intervalo_km": 10000,
            "intervalo_meses": 6,
            "tipo_acao": "inspecionar",
            "ordem": 30,
            "notas_fabricante": "Verificar vazamentos nos amortecedores, estado das molas, buchas, batentes, bieletas da barra estabilizadora. Verificar altura do veiculo."
        },

        # OUTROS
        {
            "categoria": "Outros",
            "item": "Pneus e Pressao de Calibragem",
            "intervalo_km": 10000,
            "intervalo_meses": 6,
            "tipo_acao": "inspecionar",
            "ordem": 31,
            "notas_fabricante": "PRESSAO RECOMENDADA: Dianteiros 29 psi / Traseiros 32 psi (pneus frios). Verificar desgaste, bolhas, cortes. Profundidade minima dos sulcos: 1.6mm. Rodizio a cada 10.000km."
        },
        {
            "categoria": "Outros",
            "item": "Lampadas, Buzinas, Limpador",
            "intervalo_km": 10000,
            "intervalo_meses": 6,
            "tipo_acao": "inspecionar",
            "ordem": 32,
            "notas_fabricante": "Verificar funcionamento de todos os farois, lanternas, luzes de freio, setas, luz de re. Testar buzina. Verificar palhetas e esguicho do limpador."
        },
        {
            "categoria": "Outros",
            "item": "Filtro do Ar Condicionado",
            "intervalo_km": 20000,
            "tipo_acao": "substituir",
            "ordem": 33,
            "notas_fabricante": "Substituir filtro de cabine/polen. Em ambientes com muita poeira ou poluicao, trocar com mais frequencia."
        },
        {
            "categoria": "Outros",
            "item": "Ar Condicionado/Refrigerante",
            "intervalo_km": 20000,
            "intervalo_meses": 12,
            "tipo_acao": "inspecionar",
            "ordem": 34,
            "notas_fabricante": "GAS: R134a. Verificar funcionamento, temperatura de saida, ruidos no compressor. Higienizar evaporador anualmente."
        },
    ]

    count = 0
    for item in plano:
        criar_item_manutencao(veiculo_id, item)
        count += 1

    return count


# ==================== MANUTENCOES REALIZADAS ====================

def get_ultima_manutencao_item(veiculo_id: int, item_id: int) -> Optional[Dict]:
    """Busca ultima manutencao realizada para um item"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM veiculo_manutencoes
            WHERE veiculo_id = %s AND item_id = %s
            ORDER BY km_manutencao DESC
            LIMIT 1
        """, (veiculo_id, item_id))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_historico_manutencoes(veiculo_id: int, limit: int = 50) -> List[Dict]:
    """Lista historico de manutencoes do veiculo"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT m.*, i.item as item_nome, i.categoria
            FROM veiculo_manutencoes m
            LEFT JOIN veiculo_itens_manutencao i ON i.id = m.item_id
            WHERE m.veiculo_id = %s
            ORDER BY m.data_manutencao DESC, m.km_manutencao DESC
            LIMIT %s
        """, (veiculo_id, limit))
        return [dict(row) for row in cursor.fetchall()]


def registrar_manutencao(veiculo_id: int, dados: Dict) -> Dict:
    """Registra uma manutencao realizada"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO veiculo_manutencoes (
                veiculo_id, item_id, data_manutencao, km_manutencao,
                tipo_acao, descricao, fornecedor, valor, nota_fiscal_url,
                relatorio_url, observacoes, ordem_servico_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
        """, (
            veiculo_id,
            dados.get('item_id'),
            dados.get('data_manutencao'),
            dados.get('km_manutencao'),
            dados.get('tipo_acao'),
            dados.get('descricao'),
            dados.get('fornecedor'),
            dados.get('valor'),
            dados.get('nota_fiscal_url'),
            dados.get('relatorio_url'),
            dados.get('observacoes'),
            dados.get('ordem_servico_id')
        ))
        manutencao = dict(cursor.fetchone())

        # Atualiza km do veiculo se maior que atual
        cursor.execute("""
            UPDATE veiculos SET km_atual = %s, km_atualizado_em = NOW()
            WHERE id = %s AND (km_atual IS NULL OR km_atual < %s)
        """, (dados.get('km_manutencao'), veiculo_id, dados.get('km_manutencao')))

        conn.commit()
        return manutencao


# ==================== STATUS E CALCULOS ====================

def get_item_manutencao(item_id: int) -> Optional[Dict]:
    """Busca um item de manutencao por ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM veiculo_itens_manutencao WHERE id = %s", (item_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def calcular_status_item(item: Dict, ultima_manutencao: Optional[Dict], km_atual: int, data_atual: date = None) -> Dict:
    """
    Calcula status de um item de manutencao.
    Retorna: status (ok/atencao/vencido), proxima_km, proxima_data, dias_restantes, km_restante
    """
    if data_atual is None:
        data_atual = date.today()

    intervalo_km = item.get('intervalo_km')
    intervalo_meses = item.get('intervalo_meses')

    # Se nunca foi feita manutencao, considerar desde 0km / data de hoje
    if ultima_manutencao:
        ultima_km = ultima_manutencao.get('km_manutencao', 0)
        ultima_data = ultima_manutencao.get('data_manutencao')
        if isinstance(ultima_data, str):
            ultima_data = datetime.strptime(ultima_data, '%Y-%m-%d').date()
    else:
        ultima_km = 0
        ultima_data = None

    status = 'ok'
    proxima_km = None
    proxima_data = None
    km_restante = None
    dias_restantes = None
    vencido_por_km = False
    vencido_por_tempo = False

    # Calculo por KM
    if intervalo_km:
        proxima_km = ultima_km + intervalo_km
        km_restante = proxima_km - km_atual

        if km_restante <= 0:
            vencido_por_km = True
        elif km_restante <= intervalo_km * 0.1:  # 10% restante = atencao
            status = 'atencao'

    # Calculo por TEMPO
    if intervalo_meses and ultima_data:
        proxima_data = ultima_data + relativedelta(months=intervalo_meses)
        dias_restantes = (proxima_data - data_atual).days

        if dias_restantes <= 0:
            vencido_por_tempo = True
        elif dias_restantes <= 30:  # Menos de 30 dias = atencao
            if status != 'vencido':
                status = 'atencao'

    # Define status final
    if vencido_por_km or vencido_por_tempo:
        status = 'vencido'

    return {
        'item_id': item.get('id'),
        'item': item.get('item'),
        'categoria': item.get('categoria'),
        'tipo_acao': item.get('tipo_acao'),
        'status': status,
        'proxima_km': proxima_km,
        'proxima_data': proxima_data.isoformat() if proxima_data else None,
        'km_restante': km_restante,
        'dias_restantes': dias_restantes,
        'ultima_km': ultima_km,
        'ultima_data': ultima_data.isoformat() if ultima_data else None,
        'intervalo_km': intervalo_km,
        'intervalo_meses': intervalo_meses,
        'vencido_por_km': vencido_por_km,
        'vencido_por_tempo': vencido_por_tempo,
        'notas_fabricante': item.get('notas_fabricante')
    }


def get_dashboard_veiculo(veiculo_id: int) -> Dict:
    """
    Retorna dados do dashboard de manutencao do veiculo:
    - Resumo por status (ok, atencao, vencido)
    - Lista de itens por categoria com status
    - Proximas manutencoes
    """
    veiculo = get_veiculo(veiculo_id)
    if not veiculo:
        return None

    km_atual = veiculo.get('km_atual', 0)
    itens = get_itens_manutencao(veiculo_id)

    # Calcula status de cada item
    itens_status = []
    for item in itens:
        ultima = get_ultima_manutencao_item(veiculo_id, item['id'])
        status = calcular_status_item(item, ultima, km_atual)
        itens_status.append(status)

    # Agrupa por status
    resumo = {'ok': 0, 'atencao': 0, 'vencido': 0}
    for item in itens_status:
        resumo[item['status']] = resumo.get(item['status'], 0) + 1

    # Agrupa por categoria
    por_categoria = {}
    for item in itens_status:
        cat = item['categoria']
        if cat not in por_categoria:
            por_categoria[cat] = []
        por_categoria[cat].append(item)

    # Ordena itens vencidos/atencao primeiro
    status_order = {'vencido': 0, 'atencao': 1, 'ok': 2}
    proximas = sorted(itens_status, key=lambda x: (status_order.get(x['status'], 9), x.get('km_restante') or 999999))

    return {
        'veiculo': veiculo,
        'km_atual': km_atual,
        'resumo': resumo,
        'total_itens': len(itens_status),
        'itens_por_categoria': por_categoria,
        'proximas_manutencoes': proximas[:10],  # Top 10 mais urgentes
        'itens_vencidos': [i for i in itens_status if i['status'] == 'vencido'],
        'itens_atencao': [i for i in itens_status if i['status'] == 'atencao']
    }


# ==================== ORDENS DE SERVICO ====================

def gerar_numero_os() -> str:
    """Gera numero unico para ordem de servico"""
    return f"OS-{datetime.now().strftime('%Y%m%d%H%M%S')}"


def criar_ordem_servico(veiculo_id: int, km_atual: int, itens_ids: List[int] = None, observacoes: str = None) -> Dict:
    """
    Cria uma ordem de servico com os itens que precisam de manutencao.
    Se itens_ids nao for passado, inclui automaticamente itens vencidos/atencao.
    """
    dashboard = get_dashboard_veiculo(veiculo_id)
    if not dashboard:
        return None

    # Se nao passou itens especificos, pega vencidos + atencao
    if itens_ids is None:
        itens_os = dashboard['itens_vencidos'] + dashboard['itens_atencao']
    else:
        itens_os = [i for i in dashboard['proximas_manutencoes'] if i['item_id'] in itens_ids]

    if not itens_os:
        return {'error': 'Nenhum item para incluir na OS'}

    # Formata itens para JSONB
    itens_json = []
    for item in itens_os:
        itens_json.append({
            'item_id': item['item_id'],
            'item': item['item'],
            'categoria': item['categoria'],
            'tipo_acao': item['tipo_acao'],
            'status': item['status'],
            'km_restante': item.get('km_restante'),
            'dias_restantes': item.get('dias_restantes'),
            'notas_fabricante': item.get('notas_fabricante'),
            'realizado': False,
            'valor': None,
            'observacao': None
        })

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO veiculo_ordens_servico (
                veiculo_id, numero, km_criacao, itens, observacoes
            ) VALUES (%s, %s, %s, %s, %s)
            RETURNING *
        """, (
            veiculo_id,
            gerar_numero_os(),
            km_atual,
            json.dumps(itens_json),
            observacoes
        ))
        os = dict(cursor.fetchone())
        conn.commit()
        return os


def get_ordem_servico(os_id: int) -> Optional[Dict]:
    """Busca ordem de servico por ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT o.*, v.placa, v.apelido, v.marca, v.modelo
            FROM veiculo_ordens_servico o
            JOIN veiculos v ON v.id = o.veiculo_id
            WHERE o.id = %s
        """, (os_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def listar_ordens_servico(veiculo_id: int = None, status: str = None) -> List[Dict]:
    """Lista ordens de servico"""
    with get_db() as conn:
        cursor = conn.cursor()
        sql = """
            SELECT o.*, v.placa, v.apelido, v.marca, v.modelo
            FROM veiculo_ordens_servico o
            JOIN veiculos v ON v.id = o.veiculo_id
            WHERE 1=1
        """
        params = []

        if veiculo_id:
            sql += " AND o.veiculo_id = %s"
            params.append(veiculo_id)

        if status:
            sql += " AND o.status = %s"
            params.append(status)

        sql += " ORDER BY o.data_criacao DESC"
        cursor.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]


def finalizar_ordem_servico(os_id: int, dados: Dict) -> Dict:
    """
    Finaliza uma ordem de servico e registra as manutencoes realizadas.
    dados deve conter:
    - data_execucao
    - km_execucao
    - valor_final
    - nota_fiscal_url (opcional)
    - relatorio_url (opcional)
    - oficina (opcional)
    - itens: lista com item_id e se foi realizado
    """
    os = get_ordem_servico(os_id)
    if not os:
        return {'error': 'Ordem de servico nao encontrada'}

    veiculo_id = os['veiculo_id']
    data_execucao = dados.get('data_execucao', date.today())
    km_execucao = dados.get('km_execucao', os['km_criacao'])

    with get_db() as conn:
        cursor = conn.cursor()

        # Atualiza OS
        cursor.execute("""
            UPDATE veiculo_ordens_servico SET
                status = 'concluida',
                data_execucao = %s,
                km_execucao = %s,
                valor_final = %s,
                nota_fiscal_url = %s,
                relatorio_url = %s,
                oficina = %s,
                atualizado_em = NOW()
            WHERE id = %s
        """, (
            data_execucao,
            km_execucao,
            dados.get('valor_final'),
            dados.get('nota_fiscal_url'),
            dados.get('relatorio_url'),
            dados.get('oficina'),
            os_id
        ))

        # Registra cada item realizado como manutencao
        itens_os = os.get('itens', [])
        if isinstance(itens_os, str):
            itens_os = json.loads(itens_os)

        itens_realizados = dados.get('itens', {})

        for item in itens_os:
            item_id = item.get('item_id')
            # Verifica se foi marcado como realizado
            if itens_realizados.get(str(item_id), True):  # Default True se nao especificado
                registrar_manutencao(veiculo_id, {
                    'item_id': item_id,
                    'data_manutencao': data_execucao,
                    'km_manutencao': km_execucao,
                    'tipo_acao': item.get('tipo_acao'),
                    'descricao': item.get('item'),
                    'fornecedor': dados.get('oficina'),
                    'valor': itens_realizados.get(f'{item_id}_valor'),
                    'nota_fiscal_url': dados.get('nota_fiscal_url'),
                    'relatorio_url': dados.get('relatorio_url'),
                    'ordem_servico_id': os_id
                })

        # Atualiza km do veiculo
        atualizar_km(veiculo_id, km_execucao)

        conn.commit()
        return get_ordem_servico(os_id)


# ==================== SEED DO PRADO ====================

def atualizar_notas_fabricante_prado(veiculo_id: int) -> int:
    """Atualiza os itens existentes do Prado com as notas do fabricante Toyota"""
    notas = {
        "Correia de Distribuicao": "OBRIGATORIO substituir a cada 150.000km. Falha pode causar danos graves ao motor. Trocar junto: tensor e polias.",
        "Folga das Valvulas": "Inspecionar folga e ajustar se necessario. Motor 1KZ-TE: Admissao 0.20-0.30mm / Escape 0.35-0.45mm (motor frio).",
        "Correias de Acionamento": "Verificar rachaduras, desgaste e tensao. Substituir se apresentar danos visiveis ou a cada 100.000km.",
        "Oleo do Motor": "OLEO RECOMENDADO: Toyota Genuine Diesel Oil 15W-40 ou equivalente API CF-4/SG. CAPACIDADE: 7.0 litros (com filtro). Em uso severo (estradas de terra, reboque), trocar a cada 2.500km.",
        "Filtro de Oleo": "Substituir junto com o oleo. Filtro Toyota Genuine ou equivalente. Torque da tampa: 25 N.m.",
        "Sistema de Arrefecimento": "Verificar: nivel do reservatorio, vazamentos nas mangueiras, estado do radiador, funcionamento da ventoinha e termostato.",
        "Fluido de Arrefecimento": "FLUIDO: Toyota Super Long Life Coolant (vermelho/rosa) ou equivalente. CAPACIDADE: 10.4 litros. Mistura 50/50 com agua destilada.",
        "Tubos de Escapamento": "Verificar vazamentos, ferrugem, fixacao dos suportes e estado dos coxins de borracha.",
        "Bateria": "Verificar nivel do eletrólito, terminais (limpar corrosao), tensao em repouso (12.4-12.7V). ESPECIFICACAO: 12V 90Ah (ex: Moura M90TD). Vida util media: 3-4 anos.",
        "Filtro de Combustivel": "Filtro de combustivel diesel. Apos substituicao, sangrar sistema de combustivel para remover ar.",
        "Sedimentador de Agua": "Drenar agua acumulada pelo dreno na parte inferior. Se luz de advertencia acender, drenar imediatamente.",
        "Filtro de Ar": "Substituir elemento filtrante. Em condicoes de muita poeira, inspecionar a cada 5.000km e substituir se necessario.",
        "Fumaca do Motor": "Verificar coloracao da fumaca: preta (excesso combustivel), azul (queima de oleo), branca (vazamento de agua).",
        "Linhas de Combustivel": "Verificar vazamentos, rachaduras e conexoes das linhas de combustivel. Substituir se houver danos.",
        "Pedal e Freio de Estacionamento": "Verificar curso do pedal (folga livre e reserva). Freio de mao: verificar numero de cliques (5-8 cliques normal).",
        "Lonas e Tambor do Freio": "Espessura minima das lonas: 1.0mm. Verificar desgaste irregular, trincas nos tambores.",
        "Discos e Pastilhas do Freio": "Espessura minima pastilhas: 1.0mm. Espessura minima disco dianteiro: 28mm.",
        "Fluido de Freio": "FLUIDO: DOT 3 ou DOT 4. Substituir completamente (sangria). Nivel: entre MIN e MAX.",
        "Fluido de Embreagem": "FLUIDO: DOT 3 ou DOT 4. Verificar nivel e vazamentos no cilindro mestre e auxiliar.",
        "Mangueiras de Freio": "Verificar rachaduras, vazamentos, bolhas e desgaste por atrito. Substituir se houver dano.",
        "Bomba de Vacuo Servo-freio": "Verificar funcionamento do servo-freio (pedal deve ficar mais leve com motor ligado).",
        "Fluido Direcao Hidraulica": "FLUIDO: ATF Dexron II ou III. Verificar nivel com motor quente. Verificar vazamentos.",
        "Volante e Caixa de Direcao": "Verificar folga no volante (max 30mm), ruidos na caixa de direcao, vazamentos, alinhamento.",
        "Lubrificacao Arvore Transmissao": "Engraxar cruzetas e juntas deslizantes com graxa a base de litio. Verificar folgas e vibracoes.",
        "Coifas do Semi-eixo": "Verificar rachaduras, vazamento de graxa, fixacao das abracadeiras. Substituir se houver dano.",
        "Juntas Esfericas e Coifas": "Verificar folgas nas juntas esfericas (balanco com veiculo suspenso), estado das coifas.",
        "Oleo do Diferencial": "OLEO: API GL-5 SAE 90. CAPACIDADE: Dianteiro 1.35L / Traseiro 2.8L. Em uso severo, trocar a cada 20.000km.",
        "Oleo Transmissao/Caixa Transferencia": "OLEO: API GL-4/GL-5 SAE 75W-90. Caixa manual: 2.6L / Caixa transferencia: 1.4L.",
        "Fluido Transmissao Automatica": "FLUIDO: Toyota ATF Type T-IV ou Dexron III. CAPACIDADE: 9.9L (troca total). Verificar nivel com motor quente em P.",
        "Suspensoes Dianteira e Traseira": "Verificar vazamentos nos amortecedores, estado das molas, buchas, batentes, bieletas.",
        "Pneus e Pressao de Calibragem": "PRESSAO: Dianteiros 29 psi / Traseiros 32 psi (pneus frios). Profundidade minima: 1.6mm. Rodizio a cada 10.000km.",
        "Lampadas, Buzinas, Limpador": "Verificar funcionamento de todos os farois, lanternas, luzes de freio, setas, buzina, palhetas.",
        "Filtro do Ar Condicionado": "Substituir filtro de cabine/polen. Em ambientes com poeira/poluicao, trocar com mais frequencia.",
        "Ar Condicionado/Refrigerante": "GAS: R134a. Verificar funcionamento, temperatura de saida, ruidos no compressor. Higienizar anualmente.",
    }

    count = 0
    with get_db() as conn:
        cursor = conn.cursor()
        for item_nome, nota in notas.items():
            cursor.execute("""
                UPDATE veiculo_itens_manutencao
                SET notas_fabricante = %s
                WHERE veiculo_id = %s AND item = %s
            """, (nota, veiculo_id, item_nome))
            count += cursor.rowcount
        conn.commit()

    return count


def criar_prado_jrw5025() -> Dict:
    """Cria o veiculo Prado JRW5025 com dados completos"""

    # Verifica se ja existe
    existente = get_veiculo_por_placa('JRW5025')
    if existente:
        return existente

    # Cria o veiculo
    veiculo = criar_veiculo({
        'placa': 'JRW5025',
        'apelido': 'Prado',
        'marca': 'Toyota',
        'modelo': 'Land Cruiser Prado',
        'versao': '3.0 Diesel Turbo',
        'ano_fabricacao': 2008,
        'ano_modelo': 2009,
        'cor': 'Preta',
        'combustivel': 'Diesel',
        'renavam': '00115396993',
        'chassi': 'JTEBY25J490068650',
        'motor': '1KZ1860491',
        'potencia': '131CV/2982cc',
        'km_atual': 208000,
        'proprietario': 'Orestes Alves de Almeida Prado',
        'observacoes': 'Land Cruiser Prado 120 Series. Motor 1KZ-TE 3.0 Turbo Diesel.'
    })

    veiculo_id = veiculo['id']

    # Importa plano de manutencao
    importar_plano_manutencao_prado(veiculo_id)

    # Registra historico de manutencoes conhecidas
    historico = [
        # Revisao 82.717km - Fluido arrefecimento
        {'data': '2015-10-16', 'km': 82717, 'itens': ['Fluido de Arrefecimento'], 'fornecedor': 'Collection'},
        # Revisao 150.000km - Correia distribuicao
        {'data': '2019-10-17', 'km': 150000, 'itens': ['Correia de Distribuicao'], 'fornecedor': None},
        # Bateria nova
        {'data': '2020-07-23', 'km': 151000, 'itens': ['Bateria'], 'fornecedor': 'Emporio das Baterias'},
        # Revisao 155.000km completa
        {'data': '2020-11-05', 'km': 155000, 'itens': [
            'Oleo do Motor', 'Filtro de Oleo', 'Filtro de Combustivel',
            'Fluido Transmissao Automatica', 'Ar Condicionado/Refrigerante'
        ], 'fornecedor': 'Ananias - Dracena'},
        # Ultima troca oleo estimada
        {'data': '2025-01-01', 'km': 200000, 'itens': ['Oleo do Motor', 'Filtro de Oleo'], 'fornecedor': None},
    ]

    # Busca itens pelo nome para pegar IDs
    itens = get_itens_manutencao(veiculo_id)
    itens_por_nome = {i['item']: i['id'] for i in itens}

    with get_db() as conn:
        cursor = conn.cursor()

        for h in historico:
            for item_nome in h['itens']:
                item_id = itens_por_nome.get(item_nome)
                if item_id:
                    cursor.execute("""
                        INSERT INTO veiculo_manutencoes (
                            veiculo_id, item_id, data_manutencao, km_manutencao,
                            descricao, fornecedor
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                    """, (
                        veiculo_id, item_id, h['data'], h['km'],
                        item_nome, h.get('fornecedor')
                    ))

        conn.commit()

    return get_veiculo(veiculo_id)
