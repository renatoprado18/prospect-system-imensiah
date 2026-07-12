"""Policy central de modelos LLM por tier de tarefa (F-E).

v0 comportamento-preservador: cada tier = o modelo que os call sites JÁ usam
hoje. Centralizar aqui permite (a) trocar o modelo de um tier inteiro em 1
linha e (b) medir custo por tier (PDCA). NÃO migrar modelo no v0 — só mover o
literal pra cá. Ver skill claude-api pra migração futura (Opus 4.8/Sonnet 5).
"""
FAST = "claude-haiku-4-5-20251001"     # classificação, triagem, OCR, extração barata
BALANCED = "claude-sonnet-4-6"          # geração, draft, análise média (default)
DEEP = "claude-opus-4-7"                # análise profunda (raro)
