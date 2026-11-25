# ---- Stage 1: build the Vite SPA ----
FROM node:20-alpine AS build
WORKDIR /app

# Install deps first (cached unless lockfile changes)
COPY package.json package-lock.json ./
RUN npm ci

# Build
COPY . .
RUN npm run build

# ---- Stage 2: serve with nginx ----
FROM nginx:1.27-alpine AS runtime

# SPA + /api proxy config (see frontend/nginx.conf)
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Static build output
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
