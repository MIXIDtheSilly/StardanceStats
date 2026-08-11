FROM node:24-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM node:24-alpine
WORKDIR /app
ENV NODE_ENV=production
ENV PORT=3000
# adapter-node bundles its dependencies, so the runtime image needs no node_modules.
COPY --from=build /app/build ./build
COPY --from=build /app/package.json ./package.json
EXPOSE 3000
CMD ["node", "build/index.js"]
