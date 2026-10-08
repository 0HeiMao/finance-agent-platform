# 前端镜像：Node 构建静态包 -> nginx 托管。
#
# 两段式是必须的：构建需要 node_modules（上万文件），运行时只需要 dist 里那几个文件。
# 最终镜像因此只有 nginx + 静态资源（几十 MB），不含 node/npm。
FROM node:22-alpine AS build

WORKDIR /app

# 先只拷清单：用 `npm ci` 而不是 `npm install`，锁文件说装哪个版本就装哪个版本
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

FROM nginx:alpine

# 站点配置放在 conf.d 而不是替换 nginx.conf：基础镜像里的 nginx.conf 已经 include 了 conf.d
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
