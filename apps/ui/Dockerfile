FROM node:20-alpine AS build
WORKDIR /app
COPY package.json ./
RUN npm install
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
# The official image runs envsubst over /etc/nginx/templates/*.template at startup,
# substituting only variables present in the environment (e.g. API_URL). nginx's own
# $host / $scheme variables are left intact.
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
