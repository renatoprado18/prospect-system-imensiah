/**
 * Article Match Widget
 *
 * Lazy-load de artigos do estoque editorial relacionados a uma noticia.
 * Usa GET /api/news/{news_id}/details (insights validados por Claude).
 *
 * API:
 *   ArticleMatch.toggle(newsId, buttonEl, containerEl)
 *   ArticleMatch.load(newsId, containerEl)
 *
 * Cache em memoria evita rebuscar a mesma noticia na mesma sessao.
 */
(function () {
    const cache = new Map();

    function escape(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function renderLoading(container) {
        container.innerHTML = `
            <div style="padding:14px 16px;color:#64748b;font-size:0.85rem;display:flex;align-items:center;gap:10px;">
                <div style="width:14px;height:14px;border:2px solid #e2e8f0;border-top-color:#8b5cf6;border-radius:50%;animation:amSpin 0.7s linear infinite;"></div>
                Avaliando estoque editorial com IA...
            </div>`;
    }

    function renderEmpty(container, hasCandidates) {
        const msg = hasCandidates
            ? 'Existem artigos candidatos mas a IA nao encontrou conexao forte com esta noticia.'
            : 'Nenhum artigo do seu estoque encontrou conexao com esta noticia.';
        container.innerHTML = `
            <div style="padding:14px 16px;background:#f8fafc;border-radius:8px;color:#64748b;font-size:0.85rem;line-height:1.5;">
                <i class="bi bi-emoji-neutral" style="margin-right:6px;"></i>${escape(msg)}
            </div>`;
    }

    function renderError(container) {
        container.innerHTML = `
            <div style="padding:12px 14px;background:#fef2f2;border-radius:8px;color:#991b1b;font-size:0.85rem;">
                <i class="bi bi-exclamation-circle" style="margin-right:6px;"></i>Erro ao carregar artigos relacionados. Tente de novo.
            </div>`;
    }

    function renderInsights(container, insights) {
        const cards = insights.map(ins => {
            const title = escape(ins.article_title || 'Artigo');
            const connection = escape(ins.connection || '');
            const editorialUrl = ins.article_id ? `/editorial?post_id=${ins.article_id}` : null;
            const externalUrl = ins.article_url || null;
            const actions = [];
            if (editorialUrl) {
                actions.push(`<a href="${editorialUrl}" style="display:inline-flex;align-items:center;gap:4px;padding:4px 10px;border-radius:5px;background:#8b5cf6;color:#fff;text-decoration:none;font-size:0.75rem;font-weight:600;"><i class="bi bi-pencil-square"></i> Abrir no editorial</a>`);
            }
            if (externalUrl) {
                actions.push(`<a href="${escape(externalUrl)}" target="_blank" rel="noopener" style="display:inline-flex;align-items:center;gap:4px;padding:4px 10px;border-radius:5px;background:#f1f5f9;color:#334155;text-decoration:none;font-size:0.75rem;font-weight:500;"><i class="bi bi-box-arrow-up-right"></i> Ver artigo</a>`);
            }
            return `
                <div style="background:#fff;border:1px solid #e2e8f0;border-left:3px solid #8b5cf6;border-radius:8px;padding:12px 14px;margin-bottom:10px;">
                    <div style="font-size:0.9rem;font-weight:600;color:#0f172a;line-height:1.35;margin-bottom:8px;">${title}</div>
                    <div style="background:linear-gradient(135deg,#faf5ff 0%,#f3e8ff 100%);border-radius:6px;padding:9px 12px;color:#5b21b6;font-size:0.82rem;line-height:1.5;margin-bottom:10px;">
                        <strong style="font-size:0.7rem;text-transform:uppercase;letter-spacing:0.5px;color:#8b5cf6;display:block;margin-bottom:3px;">Conexao</strong>${connection}
                    </div>
                    <div style="display:flex;gap:6px;flex-wrap:wrap;">${actions.join('')}</div>
                </div>`;
        }).join('');
        container.innerHTML = `
            <div style="padding:4px 0;">
                <div style="font-size:0.7rem;text-transform:uppercase;letter-spacing:0.5px;color:#8b5cf6;font-weight:700;margin-bottom:8px;">
                    <i class="bi bi-lightbulb-fill"></i> ${insights.length} artigo${insights.length !== 1 ? 's' : ''} do estoque
                </div>
                ${cards}
            </div>`;
    }

    async function load(newsId, container) {
        if (!container) return;
        if (cache.has(newsId)) {
            const data = cache.get(newsId);
            const insights = data.insights || [];
            if (insights.length > 0) renderInsights(container, insights);
            else renderEmpty(container, (data.related_articles || []).length > 0);
            return;
        }
        renderLoading(container);
        try {
            const r = await fetch(`/api/news/${newsId}/details`);
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            const data = await r.json();
            cache.set(newsId, data);
            const insights = data.insights || [];
            if (insights.length > 0) renderInsights(container, insights);
            else renderEmpty(container, (data.related_articles || []).length > 0);
        } catch (e) {
            console.error('ArticleMatch.load failed', e);
            renderError(container);
        }
    }

    function toggle(newsId, button, container) {
        if (!container) return;
        const isOpen = container.dataset.amOpen === '1';
        if (isOpen) {
            container.style.display = 'none';
            container.dataset.amOpen = '0';
            if (button) button.classList.remove('am-active');
            return;
        }
        container.style.display = 'block';
        container.dataset.amOpen = '1';
        if (button) button.classList.add('am-active');
        if (!container.dataset.amLoaded) {
            load(newsId, container);
            container.dataset.amLoaded = '1';
        }
    }

    if (!document.getElementById('article-match-style')) {
        const s = document.createElement('style');
        s.id = 'article-match-style';
        s.textContent = `
            @keyframes amSpin { to { transform: rotate(360deg); } }
            .am-related-pane { margin-top: 10px; }
            .am-trigger { background:#faf5ff;color:#7c3aed;border:1px solid #ddd6fe;border-radius:6px;padding:5px 11px;font-size:0.78rem;font-weight:600;cursor:pointer;display:inline-flex;align-items:center;gap:5px;transition:all 0.15s;}
            .am-trigger:hover { background:#ede9fe; }
            .am-trigger.am-active { background:#7c3aed;color:#fff;border-color:#7c3aed; }
        `;
        document.head.appendChild(s);
    }

    window.ArticleMatch = { load, toggle };
})();
