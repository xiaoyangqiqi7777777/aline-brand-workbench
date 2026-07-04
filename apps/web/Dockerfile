FROM node:22-alpine

WORKDIR /workspace

ENV NEXT_TELEMETRY_DISABLED=1

COPY package.json package-lock.json ./
COPY apps/web/package.json apps/web/package.json
RUN npm ci

COPY apps/web apps/web

EXPOSE 3000

CMD ["npm", "run", "dev", "--workspace", "@brand-studio/web", "--", "--hostname", "0.0.0.0"]
