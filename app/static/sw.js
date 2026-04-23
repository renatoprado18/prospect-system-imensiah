/**
 * Service Worker for INTEL Push Notifications
 */

const CACHE_NAME = 'intel-v2';
const STATIC_ASSETS = [
    '/static/img/icon-192.svg',
    '/static/img/icon-512.svg',
    '/static/manifest.json',
    'https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css',
    'https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.1/font/bootstrap-icons.css',
    'https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js',
];

// Install event - pre-cache static assets
self.addEventListener('install', (event) => {
    console.log('Service Worker installed');
    event.waitUntil(
        caches.open(CACHE_NAME)
            .then(cache => cache.addAll(STATIC_ASSETS))
            .then(() => self.skipWaiting())
    );
});

// Activate event - clean old caches
self.addEventListener('activate', (event) => {
    console.log('Service Worker activated');
    event.waitUntil(
        caches.keys().then(keys =>
            Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
        ).then(() => clients.claim())
    );
});

// Fetch event - network-first for pages, cache-first for static assets
self.addEventListener('fetch', (event) => {
    const url = new URL(event.request.url);

    // Only handle GET requests
    if (event.request.method !== 'GET') return;

    // Skip API calls
    if (url.pathname.startsWith('/api/')) return;

    // Cache-first for static assets (CDN, /static/)
    if (url.pathname.startsWith('/static/') || url.hostname.includes('cdn.jsdelivr.net') || url.hostname.includes('fonts.googleapis.com') || url.hostname.includes('fonts.gstatic.com')) {
        event.respondWith(
            caches.match(event.request).then(cached => {
                return cached || fetch(event.request).then(response => {
                    const clone = response.clone();
                    caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
                    return response;
                });
            })
        );
        return;
    }

    // Network-first for pages (HTML)
    if (event.request.headers.get('accept')?.includes('text/html')) {
        event.respondWith(
            fetch(event.request)
                .then(response => {
                    const clone = response.clone();
                    caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
                    return response;
                })
                .catch(() => caches.match(event.request))
        );
    }
});

// Push event - triggered when push notification is received
self.addEventListener('push', (event) => {
    console.log('Push notification received');

    let data = {
        title: 'INTEL',
        body: 'Nova notificacao',
        icon: '/static/img/icon-192.png',
        badge: '/static/img/badge-72.png',
        tag: 'intel-notification',
        data: {}
    };

    if (event.data) {
        try {
            const payload = event.data.json();
            data = {
                title: payload.title || data.title,
                body: payload.body || data.body,
                icon: payload.icon || data.icon,
                badge: payload.badge || data.badge,
                tag: payload.tag || `intel-${Date.now()}`,
                data: payload.data || {},
                actions: payload.actions || []
            };
        } catch (e) {
            data.body = event.data.text();
        }
    }

    const options = {
        body: data.body,
        icon: data.icon,
        badge: data.badge,
        tag: data.tag,
        data: data.data,
        requireInteraction: data.data?.urgent || false,
        actions: data.actions
    };

    event.waitUntil(
        self.registration.showNotification(data.title, options)
    );
});

// Notification click event
self.addEventListener('notificationclick', (event) => {
    console.log('Notification clicked:', event.action);
    event.notification.close();

    const data = event.notification.data || {};
    let url = '/rap';

    // Handle action buttons
    if (event.action) {
        if (event.action === 'open' && data.url) {
            url = data.url;
        } else if (event.action === 'execute' && data.proposal_id && data.option_id) {
            url = `/api/action-proposals/${data.proposal_id}/quick-action?option=${data.option_id}&confirm=true`;
        } else if (event.action === 'dismiss' && data.proposal_id) {
            // Just close notification, optionally dismiss proposal
            fetch(`/api/action-proposals/${data.proposal_id}/dismiss`, { method: 'POST' });
            return;
        }
    } else {
        // Default click - open dashboard or specific URL
        if (data.url) {
            url = data.url;
        } else if (data.contact_id) {
            url = `/contatos/${data.contact_id}`;
        } else if (data.proposal_id) {
            url = '/rap#actionProposalsWidget';
        }
    }

    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true })
            .then((windowClients) => {
                // Check if there's already a window open
                for (const client of windowClients) {
                    if (client.url.includes('/rap') && 'focus' in client) {
                        client.focus();
                        if (url !== '/rap') {
                            client.navigate(url);
                        }
                        return;
                    }
                }
                // Open new window
                if (clients.openWindow) {
                    return clients.openWindow(url);
                }
            })
    );
});

// Notification close event
self.addEventListener('notificationclose', (event) => {
    console.log('Notification closed');
});
