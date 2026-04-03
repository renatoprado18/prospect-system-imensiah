/**
 * Service Worker for INTEL Push Notifications
 */

const CACHE_NAME = 'intel-v1';

// Install event
self.addEventListener('install', (event) => {
    console.log('Service Worker installed');
    self.skipWaiting();
});

// Activate event
self.addEventListener('activate', (event) => {
    console.log('Service Worker activated');
    event.waitUntil(clients.claim());
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
