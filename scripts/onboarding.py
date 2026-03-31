#!/usr/bin/env python3
"""
INTEL Onboarding Script
Execute ao iniciar uma sessao Claude Code para sincronizar conhecimento.

Uso: python scripts/onboarding.py [--full]
"""

import os
import subprocess
import re
from datetime import datetime
from pathlib import Path

# Colors for terminal
class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'

def print_header(text):
    print(f"\n{Colors.BOLD}{Colors.HEADER}{'='*60}{Colors.ENDC}")
    print(f"{Colors.BOLD}{Colors.HEADER}{text.center(60)}{Colors.ENDC}")
    print(f"{Colors.BOLD}{Colors.HEADER}{'='*60}{Colors.ENDC}\n")

def print_section(text):
    print(f"\n{Colors.BOLD}{Colors.CYAN}## {text}{Colors.ENDC}")
    print(f"{Colors.CYAN}{'-'*40}{Colors.ENDC}")

def print_item(label, value, color=Colors.ENDC):
    print(f"  {Colors.BOLD}{label}:{Colors.ENDC} {color}{value}{Colors.ENDC}")

def print_warning(text):
    print(f"  {Colors.YELLOW}! {text}{Colors.ENDC}")

def print_success(text):
    print(f"  {Colors.GREEN}+ {text}{Colors.ENDC}")

def print_error(text):
    print(f"  {Colors.RED}x {text}{Colors.ENDC}")

def get_project_root():
    """Find project root by looking for .git directory"""
    current = Path(__file__).resolve().parent.parent
    return current

def read_file_section(filepath, start_marker, end_marker=None, max_lines=30):
    """Read a section from a markdown file"""
    try:
        with open(filepath, 'r') as f:
            content = f.read()

        if start_marker in content:
            start_idx = content.index(start_marker)
            section = content[start_idx:]

            if end_marker and end_marker in section[len(start_marker):]:
                end_idx = section.index(end_marker, len(start_marker))
                section = section[:end_idx]

            lines = section.split('\n')[:max_lines]
            return '\n'.join(lines)
    except Exception as e:
        return f"Error reading file: {e}"
    return ""

def get_git_info():
    """Get git status and recent commits"""
    info = {}

    try:
        # Current branch
        result = subprocess.run(['git', 'branch', '--show-current'],
                              capture_output=True, text=True)
        info['branch'] = result.stdout.strip()

        # Status
        result = subprocess.run(['git', 'status', '--short'],
                              capture_output=True, text=True)
        info['status'] = result.stdout.strip() or "(clean)"

        # Recent commits
        result = subprocess.run(['git', 'log', '--oneline', '-10'],
                              capture_output=True, text=True)
        info['commits'] = result.stdout.strip()

        # Last modified files
        result = subprocess.run(['git', 'diff', '--name-only', 'HEAD~5', 'HEAD'],
                              capture_output=True, text=True)
        info['recent_files'] = result.stdout.strip()

    except Exception as e:
        info['error'] = str(e)

    return info

def extract_gotchas(filepath):
    """Extract gotchas from ARCHITECTURE.md"""
    gotchas = []
    try:
        with open(filepath, 'r') as f:
            content = f.read()

        # Find Gotchas section
        if "## Gotchas" in content:
            start = content.index("## Gotchas")
            end = content.find("\n## ", start + 10)
            section = content[start:end] if end > 0 else content[start:]

            # Extract items
            lines = section.split('\n')
            current_gotcha = None
            for line in lines:
                if line.startswith('### '):
                    if current_gotcha:
                        gotchas.append(current_gotcha)
                    current_gotcha = {'title': line[4:], 'detail': ''}
                elif current_gotcha and line.strip().startswith('-'):
                    current_gotcha['detail'] += line.strip()[2:] + ' '

            if current_gotcha:
                gotchas.append(current_gotcha)

    except Exception:
        pass

    return gotchas

def extract_status(filepath):
    """Extract status from COORDINATION.md"""
    status = {}
    try:
        with open(filepath, 'r') as f:
            content = f.read()

        # Extract key-value pairs from Status Atual section
        lines = content.split('\n')
        in_status = False
        for line in lines:
            if '## Status Atual' in line:
                in_status = True
                continue
            if in_status:
                if line.startswith('## '):
                    break
                if '**' in line and ':' in line:
                    match = re.search(r'\*\*(.+?)\*\*:\s*(.+)', line)
                    if match:
                        status[match.group(1)] = match.group(2)

    except Exception:
        pass

    return status

def extract_recent_session(filepath):
    """Extract most recent session info"""
    try:
        with open(filepath, 'r') as f:
            content = f.read()

        # Find most recent session
        pattern = r'## Sessao (\d{4}-\d{2}-\d{2}) \((\w+)\)'
        matches = list(re.finditer(pattern, content))

        if matches:
            last_match = matches[-1]
            date = last_match.group(1)
            instance = last_match.group(2)

            # Get session content
            start = last_match.start()
            next_section = content.find('\n## ', start + 10)
            section = content[start:next_section] if next_section > 0 else content[start:start+2000]

            return {'date': date, 'instance': instance, 'content': section}

    except Exception:
        pass

    return None

def main(full=False):
    root = get_project_root()
    os.chdir(root)

    print_header("INTEL - Onboarding")
    print(f"  {Colors.CYAN}Data:{Colors.ENDC} {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  {Colors.CYAN}Diretorio:{Colors.ENDC} {root}")

    # === STATUS GERAL ===
    print_section("Status do Sistema")

    coord_file = root / 'docs' / 'COORDINATION.md'
    status = extract_status(coord_file)

    for key, value in status.items():
        color = Colors.GREEN if 'ATIVO' in value or 'AUTOMATICO' in value else Colors.ENDC
        print_item(key, value, color)

    # === GIT INFO ===
    print_section("Git Status")

    git_info = get_git_info()
    print_item("Branch", git_info.get('branch', 'unknown'), Colors.BLUE)
    print_item("Status", git_info.get('status', 'unknown'))

    print(f"\n  {Colors.BOLD}Ultimos commits:{Colors.ENDC}")
    for line in git_info.get('commits', '').split('\n')[:5]:
        if line:
            print(f"    {Colors.GREEN}{line}{Colors.ENDC}")

    # === GOTCHAS ===
    print_section("Gotchas Importantes")

    arch_file = root / 'docs' / 'ARCHITECTURE.md'
    gotchas = extract_gotchas(arch_file)

    if gotchas:
        for g in gotchas[:5]:
            print_warning(f"{g['title']}")
            if g['detail']:
                print(f"      {g['detail'][:80]}...")
    else:
        # Fallback - show key gotchas manually
        print_warning("FastAPI Route Ordering: Rotas especificas ANTES de parametrizadas")
        print_warning("Deploy: AUTOMATICO via Vercel ao fazer git push origin main")
        print_warning("PostgreSQL: similarity() NAO disponivel - usar ILIKE")
        print_warning("Google OAuth: Tasks sync requer scope 'tasks' (nao 'tasks.readonly')")
        print_warning("Claude API 529: Implementar retry com backoff exponencial")

    # === ULTIMA SESSAO ===
    print_section("Ultima Sessao")

    session = extract_recent_session(coord_file)
    if session:
        print_item("Data", session['date'])
        print_item("Instancia", session['instance'], Colors.BLUE)

        # Extract work done
        if 'Trabalho Realizado' in session['content']:
            print(f"\n  {Colors.BOLD}Trabalho realizado:{Colors.ENDC}")
            lines = session['content'].split('\n')
            for line in lines:
                if '| CONCLUIDO |' in line or '| EM ANDAMENTO |' in line:
                    # Extract task name
                    parts = line.split('|')
                    if len(parts) >= 2:
                        task = parts[1].strip()
                        status_txt = 'CONCLUIDO' if 'CONCLUIDO' in line else 'EM ANDAMENTO'
                        color = Colors.GREEN if status_txt == 'CONCLUIDO' else Colors.YELLOW
                        print(f"    {color}[{status_txt}]{Colors.ENDC} {task}")

    # === ARQUIVOS RECENTES ===
    print_section("Arquivos Modificados Recentemente")

    recent = git_info.get('recent_files', '').split('\n')[:10]
    for f in recent:
        if f:
            color = Colors.YELLOW if 'main.py' in f else Colors.ENDC
            print(f"    {color}{f}{Colors.ENDC}")

    # === FILAS DE TAREFAS ===
    print_section("Filas de Tarefas")

    task_files = [
        ('2INTEL', 'docs/INTEL_TASK_QUEUE.md'),
        ('3FLOW', 'docs/FLOW_TASK_QUEUE.md'),
    ]

    for name, filepath in task_files:
        full_path = root / filepath
        if full_path.exists():
            with open(full_path, 'r') as f:
                content = f.read()

            # Count pending tasks
            pending = content.count('[ ]') + content.count('PENDENTE')
            done = content.count('[x]') + content.count('CONCLUIDO')

            status_color = Colors.GREEN if pending == 0 else Colors.YELLOW
            print_item(name, f"{pending} pendentes, {done} concluidas", status_color)
        else:
            print_item(name, "Arquivo nao encontrado", Colors.RED)

    # === COMANDOS UTEIS ===
    print_section("Comandos Uteis")
    print(f"  {Colors.CYAN}Ler coordenacao:{Colors.ENDC}  cat docs/COORDINATION.md")
    print(f"  {Colors.CYAN}Ler arquitetura:{Colors.ENDC}  cat docs/ARCHITECTURE.md")
    print(f"  {Colors.CYAN}Ver commits:{Colors.ENDC}      git log --oneline -20")
    print(f"  {Colors.CYAN}Testar local:{Colors.ENDC}     cd app && uvicorn main:app --reload")

    # === FULL MODE ===
    if full:
        print_section("Conteudo Completo - COORDINATION.md")
        with open(coord_file, 'r') as f:
            print(f.read()[:3000])

    print(f"\n{Colors.GREEN}{'='*60}{Colors.ENDC}")
    print(f"{Colors.GREEN}Onboarding completo. Bom trabalho!{Colors.ENDC}")
    print(f"{Colors.GREEN}{'='*60}{Colors.ENDC}\n")

if __name__ == '__main__':
    import sys
    full_mode = '--full' in sys.argv
    main(full=full_mode)
