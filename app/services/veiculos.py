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


def get_alertas_manutencao() -> Dict:
    """
    Retorna alertas de manutencao de todos os veiculos ativos.
    Usado para widget no dashboard principal.
    """
    veiculos = listar_veiculos(apenas_ativos=True)

    alertas = {
        'vencidos': [],
        'atencao': [],
        'total_vencidos': 0,
        'total_atencao': 0,
        'veiculos': []
    }

    for veiculo in veiculos:
        dashboard = get_dashboard_veiculo(veiculo['id'])
        if not dashboard:
            continue

        veiculo_info = {
            'id': veiculo['id'],
            'apelido': veiculo.get('apelido') or f"{veiculo['marca']} {veiculo['modelo']}",
            'placa': veiculo['placa'],
            'km_atual': veiculo.get('km_atual', 0),
            'vencidos': len(dashboard.get('itens_vencidos', [])),
            'atencao': len(dashboard.get('itens_atencao', []))
        }

        # Adiciona itens vencidos
        for item in dashboard.get('itens_vencidos', []):
            alertas['vencidos'].append({
                'veiculo_id': veiculo['id'],
                'veiculo': veiculo_info['apelido'],
                'placa': veiculo['placa'],
                'item': item['item'],
                'categoria': item['categoria'],
                'km_restante': item.get('km_restante'),
                'status': 'vencido'
            })

        # Adiciona itens em atencao
        for item in dashboard.get('itens_atencao', []):
            alertas['atencao'].append({
                'veiculo_id': veiculo['id'],
                'veiculo': veiculo_info['apelido'],
                'placa': veiculo['placa'],
                'item': item['item'],
                'categoria': item['categoria'],
                'km_restante': item.get('km_restante'),
                'status': 'atencao'
            })

        if veiculo_info['vencidos'] > 0 or veiculo_info['atencao'] > 0:
            alertas['veiculos'].append(veiculo_info)

    alertas['total_vencidos'] = len(alertas['vencidos'])
    alertas['total_atencao'] = len(alertas['atencao'])

    # Ordena por urgencia (vencidos primeiro, depois por km_restante)
    alertas['vencidos'].sort(key=lambda x: x.get('km_restante') or 0)
    alertas['atencao'].sort(key=lambda x: x.get('km_restante') or 0)

    return alertas


def get_timeline_manutencao(veiculo_id: int) -> Dict:
    """
    Retorna timeline de manutencao do veiculo no formato matriz.
    Compara o plano do fabricante com o historico real de manutencoes.
    """
    veiculo = get_veiculo(veiculo_id)
    if not veiculo:
        return None

    km_atual = veiculo.get('km_atual', 0)

    # Busca itens de manutencao do veiculo
    with get_db() as conn:
        cursor = conn.cursor()

        # Itens de manutencao (plano do fabricante)
        cursor.execute("""
            SELECT * FROM veiculo_itens_manutencao
            WHERE veiculo_id = %s
            ORDER BY categoria, item
        """, (veiculo_id,))
        itens = [dict(row) for row in cursor.fetchall()]

        # Historico de manutencoes
        cursor.execute("""
            SELECT id, veiculo_id, item_id, data_manutencao as data_execucao,
                   km_manutencao as km_execucao, tipo_acao, descricao,
                   fornecedor, valor, observacoes, ordem_servico_id
            FROM veiculo_manutencoes
            WHERE veiculo_id = %s
            ORDER BY km_manutencao
        """, (veiculo_id,))
        historico = [dict(row) for row in cursor.fetchall()]

    if not itens:
        return {'veiculo': veiculo, 'itens': [], 'intervalos': [], 'matriz': {}}

    # Determina intervalo base (menor intervalo_km entre os itens)
    intervalos_km = [i['intervalo_km'] for i in itens if i.get('intervalo_km')]
    intervalo_base = min(intervalos_km) if intervalos_km else 5000

    # Gera colunas de intervalos (de 0 ate km_atual + 20000, arredondado)
    km_max = ((km_atual // intervalo_base) + 3) * intervalo_base
    intervalos = list(range(intervalo_base, km_max + 1, intervalo_base))

    # Cria indice de historico por item_id e km
    historico_por_item = {}
    for h in historico:
        item_id = h.get('item_id')
        if item_id:
            if item_id not in historico_por_item:
                historico_por_item[item_id] = []
            historico_por_item[item_id].append(h)

    # Agrupa itens por categoria
    categorias = {}
    for item in itens:
        cat = item.get('categoria', 'Outros')
        if cat not in categorias:
            categorias[cat] = []
        categorias[cat].append(item)

    # Determina o primeiro km registrado globalmente (ponto de partida dos dados)
    primeiro_km_global = None
    if historico:
        primeiro_km_global = min(h['km_execucao'] for h in historico)

    # Constroi matriz de status
    matriz = {}
    itens_precisam_revisao = []  # Itens que precisam ser feitos agora

    for item in itens:
        item_id = item['id']
        intervalo_item = item.get('intervalo_km', 0)
        hist_item = historico_por_item.get(item_id, [])

        # KMs onde foi executado
        kms_executados = {h['km_execucao'] for h in hist_item}

        # Primeiro km registrado para este item especifico
        primeiro_km_item = min(kms_executados) if kms_executados else None

        linha = {}
        ultimo_km_feito = None

        for km_col in intervalos:
            # Verifica se este item deveria ser feito neste km
            if intervalo_item and intervalo_item > 0:
                deveria_fazer = (km_col % intervalo_item == 0)
            else:
                deveria_fazer = False

            if not deveria_fazer:
                # Item nao aplicavel neste intervalo
                linha[km_col] = {'status': 'na', 'class': 'na'}
            else:
                # Verifica se foi feito (com tolerancia de 20%)
                tolerancia = intervalo_item * 0.2
                feito = False
                km_execucao = None
                data_execucao = None

                for km_exec in kms_executados:
                    if abs(km_exec - km_col) <= tolerancia:
                        feito = True
                        km_execucao = km_exec
                        ultimo_km_feito = km_exec
                        # Busca data
                        for h in hist_item:
                            if h['km_execucao'] == km_exec:
                                data_execucao = h.get('data_execucao')
                                break
                        break

                if feito:
                    linha[km_col] = {
                        'status': 'done',
                        'class': 'done',
                        'km_execucao': km_execucao,
                        'data': str(data_execucao) if data_execucao else None
                    }
                elif km_col > km_atual:
                    # Futuro
                    linha[km_col] = {'status': 'future', 'class': 'future'}
                elif primeiro_km_item and km_col < primeiro_km_item - tolerancia:
                    # Antes do primeiro registro - sem dados historicos
                    linha[km_col] = {'status': 'no_data', 'class': 'no-data'}
                elif km_col <= km_atual - intervalo_item:
                    # Perdido (deveria ter feito e passou muito)
                    linha[km_col] = {'status': 'missed', 'class': 'missed'}
                elif km_col <= km_atual:
                    # Pendente/Atrasado
                    linha[km_col] = {'status': 'pending', 'class': 'pending'}
                else:
                    linha[km_col] = {'status': 'na', 'class': 'na'}

        matriz[item_id] = linha

        # Verifica se este item precisa de revisao agora
        if intervalo_item and intervalo_item > 0:
            if ultimo_km_feito:
                km_desde_ultima = km_atual - ultimo_km_feito
                if km_desde_ultima >= intervalo_item:
                    itens_precisam_revisao.append({
                        'item': item,
                        'ultimo_km': ultimo_km_feito,
                        'km_desde_ultima': km_desde_ultima,
                        'atrasado_por': km_desde_ultima - intervalo_item
                    })
            elif primeiro_km_item is None:
                # Nunca foi feito e deveria
                itens_precisam_revisao.append({
                    'item': item,
                    'ultimo_km': None,
                    'km_desde_ultima': None,
                    'atrasado_por': None,
                    'nunca_feito': True
                })

    # Calcula estatisticas (apenas a partir do primeiro registro)
    total_pontos = 0
    total_feitos = 0
    total_perdidos = 0
    total_sem_dados = 0

    for item_id, linha in matriz.items():
        for km_col, cell in linha.items():
            if km_col <= km_atual:
                if cell['status'] == 'done':
                    total_pontos += 1
                    total_feitos += 1
                elif cell['status'] == 'missed':
                    total_pontos += 1
                    total_perdidos += 1
                elif cell['status'] == 'pending':
                    total_pontos += 1
                elif cell['status'] == 'no_data':
                    total_sem_dados += 1

    # Ordena itens que precisam revisao por urgencia
    itens_precisam_revisao.sort(
        key=lambda x: (x.get('nunca_feito', False), -(x.get('atrasado_por') or 0)),
        reverse=True
    )

    return {
        'veiculo': veiculo,
        'itens': itens,
        'categorias': categorias,
        'intervalos': intervalos,
        'intervalo_base': intervalo_base,
        'matriz': matriz,
        'km_atual': km_atual,
        'primeiro_km_registrado': primeiro_km_global,
        'historico': historico,
        'precisam_revisao': itens_precisam_revisao,
        'stats': {
            'total_pontos': total_pontos,
            'total_feitos': total_feitos,
            'total_perdidos': total_perdidos,
            'total_sem_dados': total_sem_dados,
            'percentual': round((total_feitos / total_pontos * 100) if total_pontos > 0 else 100, 1)
        }
    }


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


def get_todas_ultimas_manutencoes(veiculo_id: int) -> Dict[int, Dict]:
    """
    Busca a ultima manutencao de TODOS os itens do veiculo em uma unica query.
    Retorna dict com item_id -> manutencao
    """
    with get_db() as conn:
        cursor = conn.cursor()
        # Usa DISTINCT ON para pegar apenas a ultima manutencao de cada item
        cursor.execute("""
            SELECT DISTINCT ON (item_id) *
            FROM veiculo_manutencoes
            WHERE veiculo_id = %s AND item_id IS NOT NULL
            ORDER BY item_id, km_manutencao DESC
        """, (veiculo_id,))
        rows = cursor.fetchall()
        return {row['item_id']: dict(row) for row in rows}


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

    OTIMIZADO: Usa apenas 3 queries em vez de N+1
    """
    veiculo = get_veiculo(veiculo_id)
    if not veiculo:
        return None

    km_atual = veiculo.get('km_atual', 0)
    itens = get_itens_manutencao(veiculo_id)

    # OTIMIZACAO: Busca todas as ultimas manutencoes em uma unica query
    ultimas_manutencoes = get_todas_ultimas_manutencoes(veiculo_id)

    # Calcula status de cada item (sem queries adicionais)
    itens_status = []
    for item in itens:
        ultima = ultimas_manutencoes.get(item['id'])
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


def criar_ordem_servico(veiculo_id: int, km_atual: int, itens_ids: List[int] = None, itens_extras: List[str] = None, observacoes: str = None, oficina: str = None) -> Dict:
    """
    Cria uma ordem de servico com os itens que precisam de manutencao.
    Se itens_ids nao for passado, inclui automaticamente itens vencidos/atencao.
    itens_extras: lista de strings com descricoes de itens adicionais
    oficina: nome da oficina onde sera realizado o servico
    """
    dashboard = get_dashboard_veiculo(veiculo_id)
    if not dashboard:
        return None

    # Se nao passou itens especificos, pega vencidos + atencao
    if itens_ids is None:
        itens_os = dashboard['itens_vencidos'] + dashboard['itens_atencao']
    else:
        itens_os = [i for i in dashboard['proximas_manutencoes'] if i['item_id'] in itens_ids]

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

    # Adiciona itens extras (servicos adicionais sem vinculo com item de manutencao)
    if itens_extras:
        for extra in itens_extras:
            if extra and extra.strip():
                itens_json.append({
                    'item_id': None,
                    'item': extra.strip(),
                    'categoria': 'Extras',
                    'tipo_acao': 'verificar',
                    'status': 'extra',
                    'km_restante': None,
                    'dias_restantes': None,
                    'notas_fabricante': None,
                    'realizado': False,
                    'valor': None,
                    'observacao': None
                })

    if not itens_json:
        return {'error': 'Nenhum item para incluir na OS'}

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO veiculo_ordens_servico (
                veiculo_id, numero, km_criacao, itens, observacoes, oficina
            ) VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING *
        """, (
            veiculo_id,
            gerar_numero_os(),
            km_atual,
            json.dumps(itens_json),
            observacoes,
            oficina
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


def deletar_ordem_servico(os_id: int) -> Dict:
    """Deleta uma ordem de servico (apenas se estiver pendente)"""
    os = get_ordem_servico(os_id)
    if not os:
        return {'error': 'Ordem de servico nao encontrada'}

    if os['status'] != 'pendente':
        return {'error': 'Apenas ordens de servico pendentes podem ser excluidas'}

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM veiculo_ordens_servico WHERE id = %s", (os_id,))
        conn.commit()

    return {'success': True, 'message': f'Ordem {os["numero"]} excluida com sucesso'}


def atualizar_ordem_servico(os_id: int, itens_ids: List[int] = None, itens_extras: List[str] = None, remover_ids: List[int] = None, remover_extras: List[str] = None, oficina: str = None) -> Dict:
    """
    Atualiza os itens de uma ordem de servico pendente.

    Parametros:
    - os_id: ID da OS
    - itens_ids: Lista de IDs de itens de manutencao a ADICIONAR
    - itens_extras: Lista de strings de itens extras a ADICIONAR
    - remover_ids: Lista de IDs de itens de manutencao a REMOVER
    - remover_extras: Lista de strings de itens extras a REMOVER
    - oficina: Nome da oficina
    """
    os_data = get_ordem_servico(os_id)
    if not os_data:
        return {'error': 'Ordem de servico nao encontrada'}

    if os_data['status'] != 'pendente':
        return {'error': 'Apenas ordens de servico pendentes podem ser editadas'}

    veiculo_id = os_data['veiculo_id']
    dashboard = get_dashboard_veiculo(veiculo_id)

    # Pega itens atuais da OS
    itens_atuais = os_data.get('itens', [])
    if isinstance(itens_atuais, str):
        itens_atuais = json.loads(itens_atuais)

    # Remove itens se solicitado
    if remover_ids:
        itens_atuais = [i for i in itens_atuais if i.get('item_id') not in remover_ids]

    if remover_extras:
        remover_extras_lower = [e.lower().strip() for e in remover_extras]
        itens_atuais = [i for i in itens_atuais if not (i.get('status') == 'extra' and i.get('item', '').lower().strip() in remover_extras_lower)]

    # Adiciona novos itens de manutencao
    if itens_ids:
        # IDs ja presentes na OS
        ids_existentes = {i.get('item_id') for i in itens_atuais if i.get('item_id')}

        # Busca dados dos itens do dashboard
        todos_itens = dashboard.get('itens_vencidos', []) + dashboard.get('itens_atencao', [])
        # Adiciona tambem itens OK para poder incluir itens de proximas revisoes
        for cat_itens in dashboard.get('itens_por_categoria', {}).values():
            todos_itens.extend(cat_itens)

        # Remove duplicatas
        itens_por_id = {i['item_id']: i for i in todos_itens}

        for item_id in itens_ids:
            if item_id not in ids_existentes and item_id in itens_por_id:
                item = itens_por_id[item_id]
                itens_atuais.append({
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

    # Adiciona novos itens extras
    if itens_extras:
        extras_existentes = {i.get('item', '').lower().strip() for i in itens_atuais if i.get('status') == 'extra'}

        for extra in itens_extras:
            if extra and extra.strip() and extra.lower().strip() not in extras_existentes:
                itens_atuais.append({
                    'item_id': None,
                    'item': extra.strip(),
                    'categoria': 'Extras',
                    'tipo_acao': 'verificar',
                    'status': 'extra',
                    'km_restante': None,
                    'dias_restantes': None,
                    'notas_fabricante': None,
                    'realizado': False,
                    'valor': None,
                    'observacao': None
                })

    if not itens_atuais:
        return {'error': 'A OS deve ter pelo menos um item'}

    # Atualiza no banco
    with get_db() as conn:
        cursor = conn.cursor()
        if oficina is not None:
            cursor.execute("""
                UPDATE veiculo_ordens_servico
                SET itens = %s, oficina = %s, atualizado_em = NOW()
                WHERE id = %s
            """, (json.dumps(itens_atuais), oficina if oficina else None, os_id))
        else:
            cursor.execute("""
                UPDATE veiculo_ordens_servico
                SET itens = %s, atualizado_em = NOW()
                WHERE id = %s
            """, (json.dumps(itens_atuais), os_id))
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


def registrar_revisao_completa(veiculo_id: int, km: int, data_revisao: str, fornecedor: str = None, itens_ids: List[int] = None) -> Dict:
    """
    Registra uma revisao completa para o veiculo.
    Se itens_ids nao for passado, registra todos os itens que deveriam ser feitos ate essa km.

    Parametros:
    - veiculo_id: ID do veiculo
    - km: Km em que a revisao foi feita
    - data_revisao: Data no formato YYYY-MM-DD
    - fornecedor: Nome da oficina (opcional)
    - itens_ids: Lista de IDs dos itens a registrar (opcional, se nao passar registra todos aplicaveis)

    Retorna:
    - Dict com quantidade de itens registrados e detalhes
    """
    veiculo = get_veiculo(veiculo_id)
    if not veiculo:
        return {'error': 'Veiculo nao encontrado'}

    itens = get_itens_manutencao(veiculo_id)
    ultimas = get_todas_ultimas_manutencoes(veiculo_id)

    registrados = []
    ignorados = []

    with get_db() as conn:
        cursor = conn.cursor()

        for item in itens:
            # Se passou lista de IDs, usa apenas esses
            if itens_ids and item['id'] not in itens_ids:
                continue

            # Verifica se ja tem registro nessa km ou superior
            ultima = ultimas.get(item['id'])
            if ultima and ultima['km_manutencao'] >= km:
                ignorados.append({
                    'item': item['item'],
                    'motivo': f"Ja tem registro em {ultima['km_manutencao']:,}km"
                })
                continue

            # Se nao passou lista, verifica se o item deveria ser feito ate essa km
            if not itens_ids:
                intervalo = item.get('intervalo_km', 0)
                if intervalo == 0:
                    continue
                # Verifica se esse item deveria ter sido feito ate essa km
                ultima_km = ultima['km_manutencao'] if ultima else 0
                proxima_km = ultima_km + intervalo
                # Se a proxima revisao era antes ou igual ao km sendo registrado, registra
                if proxima_km > km:
                    ignorados.append({
                        'item': item['item'],
                        'motivo': f"Proxima so em {proxima_km:,}km (ultima: {ultima_km:,}km)"
                    })
                    continue

            # Registra a manutencao
            cursor.execute("""
                INSERT INTO veiculo_manutencoes (
                    veiculo_id, item_id, data_manutencao, km_manutencao,
                    tipo_acao, descricao, fornecedor
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (
                veiculo_id, item['id'], data_revisao, km,
                item.get('tipo_acao'), item['item'], fornecedor
            ))
            registrados.append(item['item'])

        conn.commit()

    return {
        'success': True,
        'km': km,
        'data': data_revisao,
        'fornecedor': fornecedor,
        'registrados': registrados,
        'total_registrados': len(registrados),
        'ignorados': ignorados,
        'total_ignorados': len(ignorados)
    }


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
