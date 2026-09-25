# 受限网络环境部署

部署机连不上 Telegram 时用这一套。已在 UGREEN DXP4800（Debian 12 / x86_64 /
Docker 29.4.3）上跑通。

## 症状

容器起来后反复崩溃重启，日志里是：

```
ConnectionError: Connection to Telegram failed 5 time(s)
```

先确认确实是网络问题，而不是配置问题：

```bash
# 三个 Telegram DC，全 FAIL 就是被阻断了
for dc in 149.154.167.51 149.154.175.53 91.108.56.130; do
  timeout 6 bash -c "</dev/tcp/$dc/443" 2>/dev/null \
    && echo "$dc:443 OK" || echo "$dc:443 FAIL"
done

# 解析结果如果落在一个和 Telegram 毫不相干的段里，就是 DNS 被污染了
getent hosts api.telegram.org
```

## 为什么是这个方案

排除掉的几条路：

| 方案 | 为什么不行 |
|---|---|
| 设 `HTTP_PROXY` / `HTTPS_PROXY` | Telethon 走 MTProto，不认这两个变量 |
| mihomo 只开 socks5（proxy 模式） | 应用层不会主动去用，等于没配 |
| 改代码加 `TG_PROXY` | 可行（约 25 行，给两个 `TelegramClient` 传 `proxy=`），但要动代码 |

剩下的就是网络层接管：**mihomo 开 TUN，bot 用 `network_mode: "service:proxy"`
共享它的 netns**。bot 一行代码不用改，它发出的所有流量（包括 MTProto 的裸 TCP）
都落进 mihomo 的规则引擎。

## 前置检查

```bash
ls -l /dev/net/tun          # 必须存在
docker pull metacubex/mihomo:latest
```

## 验证链路

起 proxy 之后、拉 bot 之前先验一遍：

```bash
# 节点加载了几个
docker exec tg-bot-proxy wget -qO- http://127.0.0.1:9090/providers/proxies/main \
  | grep -o '"type":"[A-Za-z]*"' | sort | uniq -c

# 三个 DC 的裸 TCP 通不通（这才是 MTProto 真正要用的）
docker exec tg-bot-proxy sh -c '
  for p in 149.154.167.51 149.154.175.53 91.108.56.130; do
    nc -z -w 8 $p 443 && echo "$p TCP_OK" || echo "$p TCP_FAIL"
  done'

# 出口 IP 是不是节点的
docker exec tg-bot-proxy wget -qO- https://api.ipify.org
```

三个都 `TCP_OK` 再 `docker compose up -d bot`。成功的标志是日志里出现：

```
tgmd.clients  bot client started as @your_bot (id ...)
tgmd.app      ready — mode telegram, N worker(s)
```

## 常见坑

**`PermissionError: /data/sessions/user.session`** —— 镜像里跑的是 uid `10001`，
而 `./data` 是 root 建的：

```bash
sudo chown -R 10001:10001 data
```

**`http.enabled is set but http.public_base_url is empty`** —— 从阶段 1 起这
只是一条启动警告，不再退出（以前会以退出码 2 退出，容器无限重启）。HTTP 服务
照常监听、`/healthz` 可用，只是「Telegram 媒体转存 PikPak」这一项关闭；磁力、
直链、分享链接转存不受影响。有了公网 HTTPS 地址后配上 `PUBLIC_BASE_URL` 即可
恢复。

**改完 mihomo 配置只重启了 proxy** —— bot 共享 proxy 的 netns，proxy 一重启
bot 的连接就断了。两个一起重启。

## Telegram 固定一个出口

`mihomo/config.example.yaml` 里 Telegram 的全部 IP 段都走 `TG` 组（`type: fallback`），
其余流量走 `PROXY`（url-test）。原因：url-test 会在节点仍然可用时自动切换，同一个
会话就可能同时出现在两个出口 IP 上；fallback 只在当前节点失效时才切。

## 直连媒体线路 v2（`TG_DIRECT_MEDIA=v2`）

DC2、DC4 各有一个媒体专用端点能从 NAS 直连（2026-09-26 实测）。v2 用这些端点下载
**非本 DC** 的文件，在直连连接上单独协商一把 auth key，这把 key 只在直连出口上用，
所以不会像 v1（`auto`，已拒绝）那样让同一把 key 出现在两个 IP 上。细节见
`tgmd/direct.py` 和 `docs/wms/M7.1-revisions.md` §B。

打开之前：

1. mihomo 规则里，三条 DIRECT 必须在 Telegram 段之前（示例配置已经这样排）：
   ```
   - IP-CIDR,149.154.166.111/32,DIRECT,no-resolve
   - IP-CIDR6,2001:67c:4e8:f002::b/128,DIRECT,no-resolve
   - IP-CIDR6,2001:67c:4e8:f004::b/128,DIRECT,no-resolve
   ```
   写在 `149.154.160.0/20 → TG` 之后就会被它先匹配，直连就变成了经代理。
2. 只想用 DC4 的 IPv4 端点，到这里就够了。DC2 只有 IPv6 端点，要用它得开 IPv6（下一节）。
3. `.env` 里设 `TG_DIRECT_MEDIA=v2`，`docker compose up -d bot`。日志里每个文件一行，
   末尾是 `route direct-v2` 或 `route proxy`。

护栏：本 DC 的文件永远走普通路线；同一 DC 同时最多 4 条直连，并且算在
`DOWNLOAD_CONNECTIONS` 里；key 被拒、导入失败或者遇到 FloodWait，这个 DC 就改走普通
路线 24 小时（连不上则 30 分钟）。新 key 存在 `data/db` 的数据库里（`direct_keys` 表），
和会话一样是凭据，数据库别外传。

关掉：`TG_DIRECT_MEDIA=off`，重启 bot。要连 key 一起清掉：
`sqlite3 data/db/<库文件> 'DELETE FROM direct_keys'`（不清也无害，off 时不会用）。

## IPv6

只有 v2 要连 IPv6 媒体端点时才需要。NAS 要有公网 IPv6（`ip -6 addr` 能看到 `240e:`
之类的全局地址）。Docker 29 上：

1. `docker-compose.yml` 末尾那段 `networks:` 取消注释。bot 用 `network_mode: service:proxy`，
   和 proxy 共用一个网络栈，所以只开 proxy 所在的 `default` 网络就够了，bot 那边什么都不用写。
2. 子网写一个 ULA（`fd00:…/64`）。Docker 27 起 `ip6tables` 默认打开，容器的 IPv6
   出站会 NAT 成宿主机的地址，和 IPv4 一样。如果你在 `/etc/docker/daemon.json` 里
   写过 `"ip6tables": false`，要删掉并重启 dockerd。
3. mihomo 配置顶层 `ipv6: true`（示例已改）；`dns.ipv6` 保持 `false`。
4. 网络要重建：`docker compose down && docker compose up -d`。
5. 验证：
   ```bash
   docker exec tg-bot-proxy ip -6 addr show scope global      # 有 fd00:7467:6d64:: 开头的地址
   docker exec tg-bot-proxy ip -6 route | grep default         # 有默认路由
   docker exec tg-bot-proxy sh -c 'nc -z -w 8 2001:67c:4e8:f004::b 443 && echo OK6 || echo FAIL6'
   ```
   `FAIL6` 也不影响 bot：v2 先试 IPv4，IPv6 连不上就跳过。

## 安全

- `mihomo/config.yaml` 里有你的订阅地址或节点凭据，`chmod 600`，别提交。
- `external-controller` 保持绑 `127.0.0.1`。mihomo 控制面默认无鉴权。
- `.env` 里有 `TG_BOT_TOKEN` 和 `TG_API_HASH`，同样 `chmod 600`。
