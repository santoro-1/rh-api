# JYD Auth Center

独立的统一账号中心，只保存账号、密码哈希和会话密钥，不处理或保存视频。

部署约束：

- 独立目录：`/opt/jyd-auth`
- 独立服务：`jyd-auth.service`
- 仅监听服务器本机：`127.0.0.1:18082`
- 独立 Nginx 文件：`/etc/nginx/conf.d/jyd-auth.conf`
- 数据目录：`/opt/jyd-auth/data`

必需环境变量：`JYD_AUTH_ADMIN_PASSWORD`。
