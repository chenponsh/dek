map $http_upgrade $connection_upgrade {
    default upgrade;
    '' close;
}

limit_req_zone $binary_remote_addr zone=hermes_login:10m rate=5r/m;

include /etc/nginx/snippets/cloudflare-realip.conf;

server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    return 444;
}

server {
    listen 80;
    listen [::]:80;
    server_name regkb.chenponai.com;

    location ^~ /.well-known/acme-challenge/ {
        root /var/www/letsencrypt;
        default_type text/plain;
        try_files $uri =404;
    }

    location = /review {
        if ($request_method != GET) { return 405; }
        return 301 https://regkb.chenponai.com/review;
    }

    location ^~ /review/ {
        if ($request_method != GET) { return 405; }
        return 301 https://regkb.chenponai.com$request_uri;
    }

    location / {
        return 301 https://regkb.chenponai.com$request_uri;
    }
}

server {
    listen 443 ssl default_server;
    listen [::]:443 ssl default_server;
    ssl_reject_handshake on;
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name regkb.chenponai.com;

    ssl_certificate /etc/letsencrypt/live/regkb.chenponai.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/regkb.chenponai.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:HermesTLS:10m;
    ssl_session_timeout 1d;
    ssl_session_tickets off;

    client_max_body_size 100m;

    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "same-origin" always;
    add_header X-Frame-Options "SAMEORIGIN" always;

    include /etc/nginx/snippets/dek-review-location.conf;

    location = /login {
        return 302 /hermes/login?next=%2Fhermes%2F;
    }

    location = /auth/password-login {
        limit_req zone=hermes_login burst=5 nodelay;
        include /etc/nginx/snippets/hermes-proxy.conf;
    }

    location = /hermes {
        return 302 /hermes/;
    }

    location ^~ /hermes/ {
        proxy_pass http://127.0.0.1:9119/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-Port 443;
        proxy_set_header X-Forwarded-Prefix /hermes;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_connect_timeout 10s;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_buffering off;
        proxy_request_buffering off;
    }

    location = /kb {
        return 302 /login;
    }

    location = /kb/ {
        return 302 /login;
    }

    location ~ ^/kb/(wiki|source)/(.*)$ {
        return 301 /$1/$2;
    }

    location / {
        limit_except GET { deny all; }
        proxy_pass http://127.0.0.1:9120;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 5s;
        proxy_read_timeout 30s;
        proxy_send_timeout 30s;
        proxy_buffering on;
        proxy_hide_header Server;
    }
}
