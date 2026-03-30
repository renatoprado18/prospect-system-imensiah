(function() {
    // INTEL LinkedIn Extractor v4
    console.log('INTEL: Iniciando extração...');

    if (!location.href.includes('linkedin.com/in/')) {
        alert('Abra um perfil LinkedIn primeiro');
        return;
    }

    var d = { linkedin_url: location.href };

    // Nome - múltiplas estratégias
    var nameEl = document.querySelector('h1');
    if (!nameEl) nameEl = document.querySelector('[class*="text-heading-xlarge"]');
    if (!nameEl) nameEl = document.querySelector('[class*="top-card"] h1');
    if (!nameEl) {
        // Tentar pelo título da página
        var titleMatch = document.title.match(/^(.+?)\s*[-|–]/);
        if (titleMatch) d.full_name = titleMatch[1].trim();
    } else {
        d.full_name = nameEl.innerText.trim().split('\n')[0];
    }

    console.log('INTEL: Nome encontrado:', d.full_name);

    // Headline - buscar div logo após o nome
    var headlineEl = document.querySelector('[class*="text-body-medium"][class*="break-words"]');
    if (!headlineEl) headlineEl = document.querySelector('[data-generated-suggestion-target]');
    if (headlineEl) {
        d.headline = headlineEl.innerText.trim();
    }

    // Se não encontrou, tentar pelo texto da página
    if (!d.headline && d.full_name) {
        var mainEl = document.querySelector('main') || document.body;
        var text = mainEl.innerText;
        var nameIdx = text.indexOf(d.full_name);
        if (nameIdx >= 0) {
            var afterName = text.substring(nameIdx + d.full_name.length);
            var lines = afterName.split('\n').map(l => l.trim()).filter(l => l.length > 0);
            for (var i = 0; i < lines.length && i < 10; i++) {
                var line = lines[i];
                if (line.length < 10 || line.length > 250) continue;
                if (/^(Conectar|Mensagem|Mais|Seguir|Enviar|Message|Connect|More|Follow|1º|2º|3º|\d+\s*(conexão|connection))/i.test(line)) continue;
                d.headline = line;
                break;
            }
        }
    }

    console.log('INTEL: Headline:', d.headline);

    // Location
    var locEl = document.querySelector('[class*="text-body-small"][class*="t-black--light"]');
    if (locEl) {
        d.location = locEl.innerText.trim();
    }

    // Fallback location - buscar padrão de cidade
    if (!d.location) {
        var bodyText = document.body.innerText;
        var locMatch = bodyText.match(/(São Paulo|Rio de Janeiro|Belo Horizonte|Curitiba|Porto Alegre|Brasília|Salvador|Fortaleza|Recife|[A-Z][a-zà-ú]+),\s*(São Paulo|SP|RJ|MG|PR|RS|BA|DF|CE|PE|Brasil|Brazil)/);
        if (locMatch) d.location = locMatch[0];
    }

    console.log('INTEL: Location:', d.location);

    // Conexões
    var connMatch = document.body.innerText.match(/(\d[\d.,]+)\+?\s*(connections|conexões)/i);
    if (connMatch) {
        d.connections = parseInt(connMatch[1].replace(/[.,]/g, ''));
    }

    // Foto de perfil
    var photoEl = document.querySelector('img.pv-top-card-profile-picture__image');
    if (!photoEl) photoEl = document.querySelector('[class*="profile-picture"] img');
    if (!photoEl) photoEl = document.querySelector('button[class*="profile-picture"] img');
    if (!photoEl) {
        // Fallback: buscar imagem quadrada do LinkedIn
        var imgs = document.querySelectorAll('img[src*="media.licdn.com"]');
        for (var i = 0; i < imgs.length; i++) {
            var img = imgs[i];
            if (img.src.includes('ghost')) continue;
            if (img.width >= 100 && img.width <= 300 && Math.abs(img.width - img.height) < 50) {
                photoEl = img;
                break;
            }
        }
    }
    if (photoEl && photoEl.src) {
        d.profile_picture = photoEl.src;
    }

    console.log('INTEL: Foto:', d.profile_picture ? 'Sim' : 'Não');

    // Título do cargo (extrair da headline)
    if (d.headline) {
        var parts = d.headline.split(/\s+at\s+|\s*@\s*|\s*\|\s*/i);
        if (parts.length >= 1) d.title = parts[0].trim();
        if (parts.length >= 2) d.company = parts[1].trim();
    }

    // Debug info
    d._version = 4;
    d._timestamp = new Date().toISOString();

    console.log('INTEL: Dados extraídos:', d);

    if (!d.full_name) {
        alert('Nome não encontrado. Verifique se está em um perfil LinkedIn.');
        return;
    }

    // Enviar para o servidor
    var url = 'https://intel.almeida-prado.com/api/linkedin/bookmarklet-receive?data=' + encodeURIComponent(JSON.stringify(d));
    window.open(url, '_blank', 'width=500,height=500');
})();
