# 宝塔服务器参考视频中转：独立 8443 端口

下文用示例 IP `203.0.113.10` 表示目标服务器公网 IP，部署时换成自己的服务器；执行脚本前先 `export MEDIA_RELAY_PUBLIC_IP=<服务器公网 IP>`，Nginx 模板里的 `__PUBLIC_IP__` 也要替换。本包为本地／知识库视频提供带签名、短时有效的 HTTPS 下载地址，供视频模型读取。视频生成仍由模型平台完成。

**部署前提示：** 如果服务器 80 端口已有网站，不能直接套用本包 HTTP 模板；8443 是否空闲必须在服务器上检查。

## 先看影响范围

- 新服务只监听 `127.0.0.1:18741`，Nginx 新增独立 `8443` 入口；现有 `80/443` 网站配置、数据库和业务程序不由脚本修改。
- 脚本只使用 `/opt/yifabao-media-relay`、`/etc/yifabao-media-relay`、`/var/lib/yifabao-media-relay`、独立证书目录和以 `yifabao-media-` 开头的 systemd 单元。拒绝接管没有本包标记的同名目录／单元。重新安装保留原密钥。
- 共享服务器仍然共享 CPU、内存、磁盘和带宽，不能保证零影响。服务限制为半个 CPU 核、256 MiB 内存、64 个任务、5 GiB 素材空间；证书续期最高 512 MiB。每 IP 最多 4 条连接，总计 8 条；下载每请求约 0.5 MiB/s，8 条并发约 32 Mbit/s。上传仍可能占用较多入站带宽，先用短视频测试并观察宝塔监控。
- 首次启用 HTTPS 时需要管理员执行一次 Nginx 配置检查及平滑重载；只新增本服务配置。自动续期成功后也会先 `nginx -t`，通过才平滑重载。配置有错时不重载。
- 证书验证必须经过公网 **80** 端口，在现有 HTTP 网站目录下添加标准 ACME 验证文件；不覆盖业务页面。证书脚本不会自动改网站规则。现有跳转、登录或 `.well-known` 禁止规则可能阻止验证，需要管理员审阅。
- IP 证书可覆盖 `https://203.0.113.10:8443`，不需要域名。但**火山是否接受并能抓取 8443，必须做一次真实参考视频生成验收**；本包不能提前保证。

## 1. 宝塔上传部署包，先只读检查

在宝塔「文件」中上传并解压到 **`/root/yifabao-media-relay`**。包内应有 `server.py`、3 个 `.sh` 脚本、2 个 Nginx 模板及本说明。不要把部署包或连接文件放进现有网站根目录。

打开宝塔「终端」，执行：

```bash
cd /root/yifabao-media-relay
bash preflight.sh
```

这一步只读：检查 Linux、Python 3.10+、systemd、宝塔 Nginx、80/443/8443/18741 监听，以及各虚拟主机的 `listen/server_name`。不打印完整配置、密钥或令牌。

有 `FAIL` 先处理对应条件；有 `ACTION` 先核对现有配置归属。Python 太旧或没有 venv 时，按实际系统安装匹配的 Python 3.10+ 与 venv 包，不要替换原业务虚拟环境。8443 已占用时停止本流程，不要停掉占用端口的原程序。

在宝塔「网站」中查看 **IP 请求在 80 端口实际落到的站点**，记下其「网站目录」；阅读该站点配置，确认 `/.well-known/acme-challenge/` 能原样读取静态文件。只看网站名称不足以判断默认 IP 站点。

## 2. 安装独立服务

确认空间和基础检查通过后执行：

```bash
cd /root/yifabao-media-relay
bash install-service.sh
systemctl status yifabao-media-relay --no-pager
```

安装器创建禁止登录的专用账户、独立 systemd 服务和两份随机密钥，自动启动并做本机健康检查。首装发现 18741 被占用会退出，不停止其他程序。**此时不会改 Nginx、防火墙或云端安全组，也不会重载 Nginx。**

默认下载签名 30 分钟过期；素材最多保留 24 小时，服务负责过期清理；磁盘素材总量上限 5 GiB。连接文件在 `/etc/yifabao-media-relay/client-connection.json`，仅 root 可读，安装器不会回显其中令牌。

## 3. 验证 HTTP 通道并签发 IP 证书

请先阅读 [Let’s Encrypt 订户协议](https://letsencrypt.org/repository/)。下列 `--accept-ca-terms` 表示操作者明确接受该 CA 当前条款；不带参数时脚本直接退出。

把命令中的 `/www/wwwroot/实际的HTTP站点目录` 换成第 1 步确认的目录，不要原样复制这个占位路径：

```bash
cd /root/yifabao-media-relay
bash install-certificate.sh --webroot /www/wwwroot/实际的HTTP站点目录 --accept-ca-terms
```

如需登记管理员邮箱，在末尾加 `--email 你的实际邮箱`。未提供邮箱时，脚本创建不含邮箱的 CA 账户。

脚本首先在所选目录的 `.well-known/acme-challenge/` 创建唯一随机测试文件，通过公网 IP HTTP 请求核对内容，并只删除自己创建的测试文件。它不跟随重定向；验证失败时退出，不会自动改登录／跳转配置。测试文件不是凭据。测试通过只能说明从服务器访问成功，正式签发还会由 CA 从外部验证。

随后在 `/opt/yifabao-media-certbot` 创建隔离 venv，仅从 PyPI 安装固定版 `certbot==5.4.0`，用 `--ip-address 203.0.113.10 --preferred-profile shortlived` 签发证书，保持全局 Python 包不变。证书位于：

```text
/etc/yifabao-media-certbot/config/live/yifabao-media-203.0.113.10/fullchain.pem
/etc/yifabao-media-certbot/config/live/yifabao-media-203.0.113.10/privkey.pem
```

Let’s Encrypt IP 证书有效期约 6 天，因此脚本安装每天两次的自动续期检查。验证用的原 HTTP 站点及 webroot 必须持续可访问；后续迁移该站点目录时需要同步更新续期配置。首次签发不重载 Nginx。

核对续期任务：

```bash
systemctl list-timers yifabao-media-certbot-renew.timer --no-pager
```

可在 HTTPS 启用后做一次 CA 测试环境续期验证（不替换正式证书）：

```bash
/opt/yifabao-media-certbot/bin/certbot renew --dry-run --config-dir /etc/yifabao-media-certbot/config --work-dir /etc/yifabao-media-certbot/work --logs-dir /etc/yifabao-media-certbot/logs
```

**当前这台服务器已有 HTTP 服务，不用 `nginx-http-acme.conf.template`。** 该模板仅为另一种情况预留：80 完全空闲、管理员确认后，可新增单独 HTTP 验证站点，webroot 使用 `/var/lib/yifabao-media-relay/acme`。不要把它复制成现有网站配置。

## 4. 新增独立 8443 HTTPS 入口

先在宝塔文件管理器确认 `/www/server/nginx/conf/nginx.conf` 的 `http { ... }` 内确实包含 `/www/server/panel/vhost/nginx/*.conf`；确认 8443 没有其他服务、同名文件不存在。**这是文件级配置**：模板顶部含 `limit_conn_zone`，不能粘贴进某个已有 `server { ... }` 的内部，也不要覆盖其他网站文件。

在宝塔「文件」中新增：

```text
/www/server/panel/vhost/nginx/yifabao-media-relay-8443.conf
```

把 `nginx-https.conf.template` 的完整内容放入这个新文件。证书成功签发后再添加模板，否则检查会因证书不存在失败。保持原有 80/443 配置不动。

验证并平滑重载：

```bash
/www/server/nginx/sbin/nginx -t
```

**只在上一步成功后执行：**

```bash
/www/server/nginx/sbin/nginx -s reload
```

在阿里云轻量服务器「防火墙」及宝塔系统防火墙中，仅按本服务需要新增 TCP **8443** 入站规则；模型平台需要访问，所以不能只允许自己的家庭 IP。保持原规则，**不开放 18741**。80 原有验证通道要可从公网读取。

在你自己的电脑终端检查（不要加 `-k` 跳过证书验证）：

```powershell
curl.exe --fail --max-time 20 https://203.0.113.10:8443/healthz
```

应返回服务健康 JSON，且没有 TLS 证书错误。失败时按 `服务健康 → Nginx 配置 → 本机防火墙 → 阿里云防火墙 → 公网证书` 顺序核查。

## 5. 本地导入连接文件并验收

在宝塔「文件」中私下下载 `/etc/yifabao-media-relay/client-connection.json` 到本机，然后使用本地项目提供的连接导入命令。不要把文件内容贴到聊天，不要提交到 Git，不要通过公共静态网站下载。它包含 `base_url`、`token`、`url_ttl_seconds`；`base_url` 应是 `https://203.0.113.10:8443`。

在本机 PowerShell 进入项目目录，先验证连接。把示例下载位置换成你的实际路径：

```powershell
cd <项目目录>\backend
.\.venv\Scripts\python.exe scripts/configure_media_relay.py C:/path/to/client-connection.json
```

这会用合成探测数据验证上传／签名下载／清理，不上传用户视频，不调用付费生成接口。验证通过后追加 `--apply`，自动备份并写入本地 `.env`，无需手抄令牌：

```powershell
.\.venv\Scripts\python.exe scripts/configure_media_relay.py C:/path/to/client-connection.json --apply
```

导入后重启本地内容服务，刷新工程素材通道检测。先选一段符合当前模型时长和格式限制的短视频进行上传，再提交一次真实参考视频生成任务。分别确认：

1. 本地上传成功，生成地址使用 8443、包含短时签名、不包含上传令牌。
2. 模型平台成功抓取视频并完成任务；只有这一步成功才代表整条通道验收通过。
3. 宝塔 CPU／内存／磁盘／带宽监控可接受，原应用继续正常使用。

若平台拒绝 8443，先保留原网站，再根据明确错误选择独立公网入口或对象存储；不要直接把现有 443 改成中转服务。

## 暂停与回滚

只暂停本服务和本包续期任务，不碰其他程序：

```bash
systemctl disable --now yifabao-media-relay.service
systemctl disable --now yifabao-media-certbot-renew.timer
```

在宝塔文件管理器把 **本次新增** 的 `yifabao-media-relay-8443.conf` 改名为 `yifabao-media-relay-8443.conf.disabled`；仅在确认是本包新增文件时操作。重新 `nginx -t`，通过后 `nginx -s reload`。撤销专为本服务新增的 8443 防火墙规则。现有业务站点保持原配置。

如果要移除开机单元，确认以下三个文件首行是本包管理标记后，只删除这三个文件：

```text
/etc/systemd/system/yifabao-media-relay.service
/etc/systemd/system/yifabao-media-certbot-renew.service
/etc/systemd/system/yifabao-media-certbot-renew.timer
```

再执行 `systemctl daemon-reload`。默认保留上传媒体、连接文件、密钥、证书及独立程序目录，方便恢复；不提供递归清空命令。如果曾在 80 空闲时新增独立 ACME 文件，确认它属于本次安装后再禁用，不能删除现有站点的 `.well-known` 公共目录。

## 验证依据与待完成项

依据 [Let’s Encrypt：Certbot 支持 IP 与短期证书](https://letsencrypt.org/2026/03/11/shorter-certs-certbot) 与 [PyPI Certbot 5.4.0](https://pypi.org/project/certbot/5.4.0/)，采用 Python 3.10+、隔离安装和自动续期。

本地脚本可做语法检查；Nginx 配置是否兼容这台服务器、HTTP 默认站点选择、证书签发、云端防火墙和火山读取 8443 均须在实际部署时验证。不要把本说明中的预期结果当成已完成的生产验证。
