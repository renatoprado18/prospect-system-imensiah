"""
Servico de Oficinas (Workshops)
Gerenciamento de oficinas para manutencao de veiculos
"""
from typing import Dict, List, Optional
import json
from database import get_db


def criar_oficina(dados: Dict) -> Dict:
    """Cria uma nova oficina"""
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO oficinas (
                nome, apelido, endereco, cidade, estado, cep,
                telefone, whatsapp, email, website,
                contato_nome, contato_id,
                especialidades, servicos, notas, google_maps_url
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s,
                %s, %s, %s, %s
            )
            RETURNING id
        """, (
            dados.get('nome'),
            dados.get('apelido'),
            dados.get('endereco'),
            dados.get('cidade'),
            dados.get('estado'),
            dados.get('cep'),
            dados.get('telefone'),
            dados.get('whatsapp'),
            dados.get('email'),
            dados.get('website'),
            dados.get('contato_nome'),
            dados.get('contato_id'),
            json.dumps(dados.get('especialidades', [])),
            json.dumps(dados.get('servicos', [])),
            dados.get('notas'),
            dados.get('google_maps_url')
        ))

        oficina_id = cursor.fetchone()['id']
        conn.commit()

        return get_oficina(oficina_id)


def get_oficina(oficina_id: int) -> Optional[Dict]:
    """Busca oficina por ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM oficinas WHERE id = %s", (oficina_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_oficina_by_nome(nome: str) -> Optional[Dict]:
    """Busca oficina pelo nome"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM oficinas WHERE nome ILIKE %s", (f'%{nome}%',))
        row = cursor.fetchone()
        return dict(row) if row else None


def listar_oficinas(ativo: bool = True) -> List[Dict]:
    """Lista todas as oficinas"""
    with get_db() as conn:
        cursor = conn.cursor()
        if ativo:
            cursor.execute("SELECT * FROM oficinas WHERE ativo = TRUE ORDER BY nome")
        else:
            cursor.execute("SELECT * FROM oficinas ORDER BY nome")
        return [dict(row) for row in cursor.fetchall()]


def atualizar_oficina(oficina_id: int, dados: Dict) -> Dict:
    """Atualiza uma oficina"""
    with get_db() as conn:
        cursor = conn.cursor()

        updates = []
        values = []
        for key in ['nome', 'apelido', 'endereco', 'cidade', 'estado', 'cep',
                    'telefone', 'whatsapp', 'email', 'website',
                    'contato_nome', 'contato_id', 'notas', 'google_maps_url', 'ativo']:
            if key in dados:
                updates.append(f"{key} = %s")
                values.append(dados[key])

        # Handle JSONB fields
        if 'especialidades' in dados:
            updates.append("especialidades = %s")
            values.append(json.dumps(dados['especialidades']))
        if 'servicos' in dados:
            updates.append("servicos = %s")
            values.append(json.dumps(dados['servicos']))

        if updates:
            values.append(oficina_id)
            cursor.execute(f"""
                UPDATE oficinas SET {', '.join(updates)}
                WHERE id = %s
            """, values)
            conn.commit()

        return get_oficina(oficina_id)


def deletar_oficina(oficina_id: int) -> Dict:
    """Desativa uma oficina (soft delete)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE oficinas SET ativo = FALSE WHERE id = %s", (oficina_id,))
        conn.commit()
    return {'success': True}


def seed_oficinas() -> List[Dict]:
    """Registra as oficinas pre-definidas"""
    oficinas_data = [
        {
            'nome': 'Sollo 4WD',
            'contato_nome': 'Sr. Joao',
            'whatsapp': '+5511940375674',
            'endereco': 'R. Andrequice, 18 - Vila Leopoldina, Sao Paulo - SP, 05307-030',
            'cidade': 'Sao Paulo',
            'estado': 'SP',
            'cep': '05307-030',
            'especialidades': ['Toyota 4x4', 'Land Cruiser', 'Prado', 'Hilux'],
            'servicos': [
                'Manutencao preventiva',
                'Manutencao corretiva',
                'Suspensao',
                'Transmissao 4x4',
                'Motor Diesel'
            ],
            'notas': 'Especialista em Toyota 4x4. Altamente recomendado para Land Cruiser Prado.'
        },
        {
            'nome': 'Bela Vista Servicos Automotivos',
            'contato_nome': 'Sr. Jardel e Sra. Nice',
            'telefone': '+551195369-4230',
            'whatsapp': '+5511953694230',
            'endereco': 'R. Andrequice, 18 - Vila Leopoldina, Sao Paulo - SP, 05307-030',
            'cidade': 'Sao Paulo',
            'estado': 'SP',
            'cep': '05307-030',
            'especialidades': ['Servicos Gerais', 'Lava-Rapido'],
            'servicos': [
                'Alinhamento de pneus',
                'Ar-condicionado',
                'Bateria',
                'Conserto e manutencao de freio',
                'Eletrico',
                'Freios',
                'Martelinho de ouro',
                'Pneus',
                'Reparo de suspensao e direcao',
                'Transmissao',
                'Troca de filtro de cabine e de ar',
                'Troca de oleo',
                'Lava-rapido',
                'Aspiracao interna de veiculos',
                'Lavagem de motores',
                'Limpeza de carpetes',
                'Limpeza geral de veiculos',
                'Polimento',
                'Restauracao de farol'
            ],
            'notas': 'Servicos gerais automotivos e lava-rapido completo.'
        },
        {
            'nome': 'Fall Car',
            'apelido': 'Fall Car Tapecaria',
            'contato_nome': 'Luiz',
            'whatsapp': '+5511947292623',
            'endereco': 'Alameda Nothmann, 1027 - Campos Eliseos, Sao Paulo - SP, 01216-001',
            'cidade': 'Sao Paulo',
            'estado': 'SP',
            'cep': '01216-001',
            'website': 'https://www.fallcar.com.br',
            'especialidades': ['Tapecaria', 'Teto Solar'],
            'servicos': [
                'Tapecaria automotiva',
                'Teto solar instalacao',
                'Teto solar manutencao',
                'Estofamento',
                'Forros',
                'Carpetes'
            ],
            'notas': 'Especialista em Tapecaria e Teto Solar.'
        }
    ]

    criadas = []
    for dados in oficinas_data:
        # Verifica se ja existe
        existente = get_oficina_by_nome(dados['nome'])
        if not existente:
            oficina = criar_oficina(dados)
            criadas.append(oficina)
        else:
            criadas.append(existente)

    return criadas
