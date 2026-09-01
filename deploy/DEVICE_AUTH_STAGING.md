# 设备授权隔离测试环境

本环境只用于本机 A 与 250 B 的设备授权、复制拒绝和连续更新验收。它不是生产副本，
不得复制生产数据库、`.env`、上传、输出、RunningHub/MiniMax/Ark 凭据或正式签名私钥。

## 当前实例

- 公网源地址：`https://video-test.lanyingjk01.com`（DNS 与 HTTPS 已接通）
- 本机监听：`127.0.0.1:18085`
- Linux 用户：`rhvideo-test`
- Web 服务：`runninghub-video-staging-web.service`
- 证书续期：`runninghub-video-staging-cert-renew.timer`
- 程序目录：`/opt/runninghub-video-staging`
- 数据目录：`/var/lib/runninghub-video-staging`
- 配置目录：`/etc/runninghub-video-staging`
- 测试签名根：`/etc/runninghub-video-staging/device-auth/staging-20260901-01`
- 测试账号：管理员 `staging-admin`、普通账号 `device-test`
- 账号密码只保存在服务器 root 专用的
  `/etc/runninghub-video-staging/bootstrap-credentials.txt`，不得提交或复制到文档。

测试数据库从空库迁移到 `0050_device_work_admission`，设备额度固定为 `1`，初始控制模式为
`OFF`。服务器只安装并启动 Web 服务；不存在测试 audio/media/video Worker。Web 单元还设置
`IPAddressDeny=any` 与 `IPAddressAllow=localhost`，即使误填第三方账号也不能从测试进程访问
公网付费接口。

测试实例的源快照记录在
`/opt/runninghub-video-staging/.deployed-staging-snapshot`。测试签名私钥只留在服务器；导回构建机
的公钥文件为 `release/device-auth-staging/staging-20260901-01-public-keys.json`。

## 只读检查

```bash
systemctl is-active runninghub-video-staging-web.service
systemctl is-active runninghub-video-staging-cert-renew.timer
systemctl show runninghub-video-staging-web.service \
  -p User -p MainPID -p MemoryCurrent -p MemoryMax \
  -p IPAddressDeny -p IPAddressAllow
ss -ltn | grep ':18085 '
curl -fsS -H 'Host: video-test.lanyingjk01.com' \
  http://127.0.0.1:18085/healthz
curl -fsS https://video-test.lanyingjk01.com/healthz
sqlite3 -readonly /var/lib/runninghub-video-staging/app.db \
  'select mode, revision from workbench_device_control where id=1;'
```

任何测试操作前后都要再次确认生产四个服务仍为 `active`。不得把生产发布脚本的 `AppDir`、
`BackupDir` 或服务名改成测试值后强行运行；测试环境应使用自己的受限更新流程。

## DNS 与 HTTPS

公网 DNS 已添加并由多个公共解析器确认：

```text
video-test.lanyingjk01.com  A  47.111.171.139
```

独立 Nginx server block 位于
`/etc/nginx/conf.d/runninghub-video-staging.conf`。HTTP 仅保留
`/.well-known/acme-challenge/` 并把其他请求重定向到 HTTPS；HTTPS 只反向代理到
`127.0.0.1:18085`。测试证书位于
`/etc/letsencrypt/live/video-test.lanyingjk01.com/`，使用
`/var/lib/runninghub-video-staging/acme` 作为 webroot。

测试证书由 `runninghub-video-staging-cert-renew.timer` 单独检查和续期，不共用也不修改生产证书
续期单元。Nginx 用户对测试数据根只有目录穿过权限，不能列出目录；业务数据仍归
`rhvideo-test`。任何 Nginx 变更都必须先执行 `nginx -t`，验证通过后只允许
`systemctl reload nginx`；不得重启 Nginx，也不得改写 `runninghub-video.conf` 或生产证书。

## A/B 验收边界

1. 测试工作台只能编入测试公钥并把 `digital_human_server_url` 指向测试域名；不得作为正式包分发。
2. A 使用当前开发电脑，申请备注 `A-local`；管理员批准 A。
3. 测试环境按受控工具依次切换 `OFF -> OBSERVE -> ENFORCE`。
4. 把 A 的同一程序目录复制到 250 B，B 申请备注 `B-250`，保持未批准。
5. B 必须生成不同的设备公钥摘要并在 `ENFORCE` 下被拒绝；不得复制或删除 Windows CNG/TPM 私钥。
6. A 连续更新至少四个不同构建，设备、授权和公钥摘要必须保持不变，不得重复激活。
7. 测试结束先切回 `OBSERVE`，核对后再切回 `OFF`。测试模式变化不影响生产数据库。

更新测试代码时必须保留 `/var/lib/runninghub-video-staging`、
`/etc/runninghub-video-staging`、Windows CNG 身份和 `%ProgramData%` 授权缓存，因此 A 的正常程序
升级不需要重新激活。
