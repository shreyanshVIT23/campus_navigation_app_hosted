# Hosting behind SWAG

[SWAG](https://docs.linuxserver.io/general/swag/) (linuxserver.io) is nginx plus Let's Encrypt certificates. Here it terminates TLS for `campnav.shreyanshpande.work` and proxies to the two containers.

## Layout
- `https://campnav.shreyanshpande.work/` → `campus-navigation-frontend:3000`
- `https://campnav.shreyanshpande.work/api/<route>` → `campus-navigation-backend:8000/<route>` (SWAG strips `/api`)
- The containers publish no ports. They share SWAG's external `web` network, and SWAG reaches them by container name.

## Files
- `docker-compose.yml` (repo root)
- `deploy/swag/campnav.subdomain.conf` → copy to SWAG's `/config/nginx/proxy-confs/`. It follows SWAG's `_template.subdomain.conf.sample` (2025/07/18).

## Why the settings are what they are
- **`--root-path /api`** (uvicorn, in `backend/Dockerfile`): FastAPI's "Behind a Proxy" guide for a prefix-stripping proxy. Routes match without the prefix, and `/api/docs` loads `/api/openapi.json`. Map URLs in responses stay relative (`/load_shortest_path_svg?...`); `frontend/lib/api.js` adds the base URL.
- **`--forwarded-allow-ips *`**: only SWAG can reach the container, so its `X-Forwarded-*` headers are trusted.
- **One uvicorn worker**: route maps are kept in process memory (`Utils/route_maps`).
- **`NEXT_PUBLIC_API_BASE_URL` is a build arg**: Next.js inlines `NEXT_PUBLIC_*` at `next build`. Changing the domain means rebuilding the frontend image.
- **`.dockerignore` uses `**/`**: Docker anchors patterns at the context root, so a plain `.env` would not exclude `server/.env`. `Test/test_behind_proxy.py` checks what is shipped.
- **`campnav-db` volume**: the five runtime DBs. Docker seeds a named volume from the image only when the volume is empty, so later DB changes in the image need `docker cp` or a new volume.
