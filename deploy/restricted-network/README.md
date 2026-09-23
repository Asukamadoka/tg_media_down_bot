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

## 安全

- `mihomo/config.yaml` 里有你的订阅地址或节点凭据，`chmod 600`，别提交。
- `external-controller` 保持绑 `127.0.0.1`。mihomo 控制面默认无鉴权。
- `.env` 里有 `TG_BOT_TOKEN` 和 `TG_API_HASH`，同样 `chmod 600`。
