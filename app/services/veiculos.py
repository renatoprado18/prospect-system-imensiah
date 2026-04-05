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
                intervalo_meses, tipo_acao, notas, ordem
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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
            dados.get('ordem', 0)
        ))
        item = dict(cursor.fetchone())
        conn.commit()
        return item


def importar_plano_manutencao_prado(veiculo_id: int) -> int:
    """Importa plano de manutencao padrao do Land Cruiser Prado"""
    plano = [
        # COMPONENTES BASICOS DO MOTOR
        {"categoria": "Motor", "item": "Correia de Distribuicao", "intervalo_km": 150000, "tipo_acao": "substituir", "ordem": 1},
        {"categoria": "Motor", "item": "Folga das Valvulas", "intervalo_km": 40000, "intervalo_meses": 48, "tipo_acao": "inspecionar", "ordem": 2},
        {"categoria": "Motor", "item": "Correias de Acionamento", "intervalo_km": 20000, "intervalo_meses": 24, "tipo_acao": "inspecionar", "ordem": 3},
        {"categoria": "Motor", "item": "Oleo do Motor", "intervalo_km": 5000, "intervalo_meses": 6, "tipo_acao": "substituir", "ordem": 4},
        {"categoria": "Motor", "item": "Filtro de Oleo", "intervalo_km": 5000, "intervalo_meses": 6, "tipo_acao": "substituir", "ordem": 5},
        {"categoria": "Motor", "item": "Sistema de Arrefecimento", "intervalo_km": 40000, "intervalo_meses": 24, "tipo_acao": "inspecionar", "ordem": 6},
        {"categoria": "Motor", "item": "Fluido de Arrefecimento", "intervalo_km": 80000, "tipo_acao": "substituir", "ordem": 7},
        {"categoria": "Motor", "item": "Tubos de Escapamento", "intervalo_km": 20000, "intervalo_meses": 12, "tipo_acao": "inspecionar", "ordem": 8},

        # SISTEMA DE IGNICAO
        {"categoria": "Eletrica", "item": "Bateria", "intervalo_km": 10000, "intervalo_meses": 12, "tipo_acao": "inspecionar", "ordem": 9},

        # SISTEMA DE COMBUSTIVEL
        {"categoria": "Combustivel", "item": "Filtro de Combustivel", "intervalo_km": 10000, "intervalo_meses": 12, "tipo_acao": "substituir", "ordem": 10},
        {"categoria": "Combustivel", "item": "Sedimentador de Agua", "intervalo_km": 10000, "intervalo_meses": 12, "tipo_acao": "inspecionar", "ordem": 11},
        {"categoria": "Combustivel", "item": "Filtro de Ar", "intervalo_km": 20000, "intervalo_meses": 36, "tipo_acao": "substituir", "ordem": 12},
        {"categoria": "Combustivel", "item": "Fumaca do Motor", "intervalo_km": 40000, "intervalo_meses": 48, "tipo_acao": "inspecionar", "ordem": 13},
        {"categoria": "Combustivel", "item": "Linhas de Combustivel", "intervalo_km": 20000, "intervalo_meses": 12, "tipo_acao": "inspecionar", "ordem": 14},

        # CHASSI E CARROCARIA - FREIOS
        {"categoria": "Freios", "item": "Pedal e Freio de Estacionamento", "intervalo_km": 10000, "intervalo_meses": 6, "tipo_acao": "inspecionar", "ordem": 15},
        {"categoria": "Freios", "item": "Lonas e Tambor do Freio", "intervalo_km": 20000, "intervalo_meses": 12, "tipo_acao": "inspecionar", "ordem": 16},
        {"categoria": "Freios", "item": "Discos e Pastilhas do Freio", "intervalo_km": 10000, "intervalo_meses": 6, "tipo_acao": "inspecionar", "ordem": 17},
        {"categoria": "Freios", "item": "Fluido de Freio", "intervalo_km": 40000, "intervalo_meses": 24, "tipo_acao": "substituir", "ordem": 18},
        {"categoria": "Freios", "item": "Fluido de Embreagem", "intervalo_km": 10000, "intervalo_meses": 6, "tipo_acao": "inspecionar", "ordem": 19},
        {"categoria": "Freios", "item": "Mangueiras de Freio", "intervalo_km": 20000, "intervalo_meses": 12, "tipo_acao": "inspecionar", "ordem": 20},
        {"categoria": "Freios", "item": "Bomba de Vacuo Servo-freio", "intervalo_km": 200000, "tipo_acao": "substituir", "ordem": 21},

        # DIRECAO
        {"categoria": "Direcao", "item": "Fluido Direcao Hidraulica", "intervalo_km": 10000, "intervalo_meses": 6, "tipo_acao": "inspecionar", "ordem": 22},
        {"categoria": "Direcao", "item": "Volante e Caixa de Direcao", "intervalo_km": 10000, "intervalo_meses": 6, "tipo_acao": "inspecionar", "ordem": 23},

        # TRANSMISSAO
        {"categoria": "Transmissao", "item": "Lubrificacao Arvore Transmissao", "intervalo_km": 10000, "intervalo_meses": 6, "tipo_acao": "lubrificar", "ordem": 24},
        {"categoria": "Transmissao", "item": "Coifas do Semi-eixo", "intervalo_km": 20000, "intervalo_meses": 24, "tipo_acao": "inspecionar", "ordem": 25},
        {"categoria": "Transmissao", "item": "Juntas Esfericas e Coifas", "intervalo_km": 10000, "intervalo_meses": 6, "tipo_acao": "inspecionar", "ordem": 26},
        {"categoria": "Transmissao", "item": "Oleo do Diferencial", "intervalo_km": 40000, "intervalo_meses": 24, "tipo_acao": "substituir", "ordem": 27},
        {"categoria": "Transmissao", "item": "Oleo Transmissao/Caixa Transferencia", "intervalo_km": 40000, "intervalo_meses": 48, "tipo_acao": "inspecionar", "ordem": 28},
        {"categoria": "Transmissao", "item": "Fluido Transmissao Automatica", "intervalo_km": 40000, "intervalo_meses": 24, "tipo_acao": "substituir", "ordem": 29},

        # SUSPENSAO
        {"categoria": "Suspensao", "item": "Suspensoes Dianteira e Traseira", "intervalo_km": 10000, "intervalo_meses": 6, "tipo_acao": "inspecionar", "ordem": 30},

        # OUTROS
        {"categoria": "Outros", "item": "Pneus e Pressao de Calibragem", "intervalo_km": 10000, "intervalo_meses": 6, "tipo_acao": "inspecionar", "ordem": 31},
        {"categoria": "Outros", "item": "Lampadas, Buzinas, Limpador", "intervalo_km": 10000, "intervalo_meses": 6, "tipo_acao": "inspecionar", "ordem": 32},
        {"categoria": "Outros", "item": "Filtro do Ar Condicionado", "intervalo_km": 20000, "tipo_acao": "substituir", "ordem": 33},
        {"categoria": "Outros", "item": "Ar Condicionado/Refrigerante", "intervalo_km": 20000, "intervalo_meses": 12, "tipo_acao": "inspecionar", "ordem": 34},
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
        'vencido_por_tempo': vencido_por_tempo
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
