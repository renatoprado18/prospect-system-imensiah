#!/usr/bin/env python3
"""Quadro de frentes em curso — quem está com qual frente, agora.

Por que existe (09/11/2026): o protocolo de sessões paralelas separava CoS de
Dev por NATUREZA do trabalho. Com **duas sessões do mesmo papel** — naquele dia
"Flamarpar" e "Vallen-RACI", as duas CoS — o marcador de papel não separa nada:
as duas são donas de escrita do MESMO `project_cos_status.md`. O board foi de
12,2 a 15,5 KB sem nenhuma das duas ver a outra somando. Ninguém perdeu
conteúdo, e foi sorte: as escritas saíram em sequência, não interleaved.

**Tabela viva, não log.** O `session_locks.md` tem teto de 9 KB e já chegou a
228 KB sendo 97% log. Uma linha por abertura de sessão o estouraria em dias.
Aqui a linha se SOBRESCREVE, e some quando a sessão morre.

  frentes.py claim <papel> [frente]   — registra/atualiza a frente desta sessão
  frentes.py release                  — solta a frente desta sessão
  frentes.py list                     — mostra o quadro

Poda automática: linha de sessão cujo processo não existe mais é removida em
qualquer invocação. Sessão que morre sem `/fim` não deixa lock fantasma.
"""
import json, glob, os, re, subprocess, sys, time

LOCKS = os.path.expanduser(
    '~/.claude/projects/-Users-rap-prospect-system/memory/session_locks.md')
CWD_ALVO = '/Users/rap/prospect-system'
INI, FIM = '<!-- FRENTES:INICIO -->', '<!-- FRENTES:FIM -->'


def sessoes_vivas():
    """{sessionId: (nome, pid)} das sessões deste projeto com processo de pé."""
    try:
        pids = set(subprocess.run(['ps', '-eo', 'pid'], capture_output=True,
                                  text=True, timeout=5).stdout.split()[1:])
    except Exception:
        pids = set()
    viva = {}
    for f in glob.glob(os.path.expanduser('~/.claude/sessions/*.json')):
        try:
            d = json.load(open(f))
        except Exception:
            continue
        if str(d.get('pid')) in pids and d.get('cwd') == CWD_ALVO:
            sid = d.get('sessionId', '')
            if d.get('updatedAt', 0) >= viva.get(sid, (0,))[0]:
                viva[sid] = (d.get('updatedAt', 0), d.get('name') or '', d.get('pid'))
    return {k: (v[1], v[2]) for k, v in viva.items()}


def eu():
    sid = os.environ.get('CLAUDE_CODE_SESSION_ID', '')
    nome, pid = sessoes_vivas().get(sid, ('', os.getppid()))
    return sid, nome, pid


# ------------------------------------------------------------------ bloco

def le_linhas(txt):
    """Linhas do quadro como lista de dicts, já podadas das sessões mortas."""
    i, j = txt.find(INI), txt.find(FIM)
    if i < 0 or j < 0:
        return []
    vivas = sessoes_vivas()
    out = []
    for l in txt[i + len(INI):j].splitlines():
        l = l.strip()
        if not l.startswith('|') or l.startswith('|---') or 'sessão' in l:
            continue
        c = [x.strip() for x in l.strip('|').split('|')]
        if len(c) < 5:
            continue
        sid = c[4].strip('`')
        if sid not in vivas:          # poda: processo sumiu
            continue
        out.append({'nome': c[0], 'papel': c[1], 'frente': c[2],
                    'desde': c[3], 'sid': sid})
    return out


def monta(linhas):
    if not linhas:
        corpo = '_nenhuma sessão registrada agora._'
    else:
        corpo = ('| sessão | papel | frente | desde | id |\n'
                 '|---|---|---|---|---|\n' +
                 '\n'.join(f"| {d['nome']} | {d['papel']} | {d['frente']} | "
                           f"{d['desde']} | `{d['sid']}` |" for d in linhas))
    return (f"{INI}\n## 🔒 Frentes em curso agora\n"
            f"> Tabela viva, mantida por `scripts/frentes.py` — não editar à mão. "
            f"Linha some quando a sessão morre.\n\n"
            f"{corpo}\n{FIM}")


def grava(txt_novo_bloco):
    """Substitui só o bloco; cria depois do frontmatter se não existir.

    Lock por O_EXCL porque duas sessões podem reivindicar no mesmo segundo —
    sem ele, o read-modify-write de uma apaga o da outra, que é exatamente o
    defeito que este arquivo existe para evitar.
    """
    trava = LOCKS + '.lock'
    for _ in range(50):
        try:
            fd = os.open(trava, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            time.sleep(0.1)
    else:
        print('frentes: lock ocupado há 5s, desisto sem escrever', file=sys.stderr)
        return False
    try:
        t = open(LOCKS).read()
        i, j = t.find(INI), t.find(FIM)
        if i >= 0 and j >= 0:
            t = t[:i] + txt_novo_bloco + t[j + len(FIM):]
        else:
            m = re.match(r'^---\n.*?\n---\n', t, re.S)   # depois do frontmatter
            p = m.end() if m else 0
            t = t[:p] + txt_novo_bloco + '\n' + t[p:]
        tmp = LOCKS + '.tmp'
        with open(tmp, 'w') as fh:
            fh.write(t)
        os.replace(tmp, LOCKS)
        return True
    finally:
        try:
            os.unlink(trava)
        except OSError:
            pass


# ------------------------------------------------------------------ cli

def main(argv):
    cmd = argv[0] if argv else 'list'
    sid, nome, _pid = eu()

    if cmd == 'list':
        linhas = le_linhas(open(LOCKS).read())
        if not linhas:
            print('nenhuma frente registrada')
        for d in linhas:
            marca = '▸' if d['sid'] == sid else ' '
            print(f"{marca} {d['nome']:22} {d['papel']:4} {d['frente']:28} desde {d['desde']}")
        return 0

    if not sid:
        print('frentes: sem CLAUDE_CODE_SESSION_ID — nada a fazer', file=sys.stderr)
        return 0                      # nunca travar a abertura da sessão

    t = open(LOCKS).read()
    linhas = [d for d in le_linhas(t) if d['sid'] != sid]

    if cmd == 'claim':
        papel = (argv[1] if len(argv) > 1 else '?').lower()
        frente = ' '.join(argv[2:]).strip() or nome or '(a definir)'
        linhas.append({'nome': nome or '(sem nome)', 'papel': papel,
                       'frente': frente[:40],
                       'desde': time.strftime('%d/%m %H:%M'), 'sid': sid})
        linhas.sort(key=lambda d: (d['papel'], d['nome']))
        if grava(monta(linhas)):
            iguais = [d for d in linhas
                      if d['papel'] == papel and d['sid'] != sid]
            print(f"frente registrada: {frente} ({papel})")
            if iguais:
                print(f"⚠️  OUTRA(S) SESSÃO(ÕES) NO MESMO PAPEL '{papel}': "
                      + ', '.join(f"{d['nome']} → {d['frente']}" for d in iguais))
                print("   Vocês dividem o MESMO board. Escreva só as linhas da sua "
                      "frente e confira o mtime antes de gravar.")
        return 0

    if cmd == 'release':
        if grava(monta(linhas)):
            print('frente solta')
        return 0

    print(f"uso: frentes.py claim <papel> [frente] | release | list", file=sys.stderr)
    return 1


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as e:                 # nunca derrubar abertura nem /fim
        print(f'frentes: {type(e).__name__}: {e}', file=sys.stderr)
        sys.exit(0)
