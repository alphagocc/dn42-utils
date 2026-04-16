#!/bin/bash

# 确保脚本以 root 权限运行
if [ "$EUID" -ne 0 ]; then
  echo "❌ 请以 root 权限运行此脚本 (例如: sudo ./setup_dn42_roa.sh)"
  exit 1
fi

echo "⏳ 开始配置 DN42 ROA 自动下载任务..."

# 确保目标目录存在
mkdir -p /etc/bird

# 1. 创建 Service 文件
echo "📝 创建 Service 文件: /etc/systemd/system/dn42-roa-v6.service"
cat << 'EOF' > /etc/systemd/system/dn42-roa-v6.service
[Unit]
Description=Download DN42 ROA for BIRD2 (IPv6)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/curl -s -o /etc/bird/roa_dn42_v6.conf https://dn42.burble.com/roa/dn42_roa_bird2_6.conf

# 如果需要在下载后自动重载 BIRD 进程，请取消注释下一行：
ExecStartPost=/usr/sbin/birdc configure
EOF

# 2. 创建 Timer 文件
echo "📝 创建 Timer 文件: /etc/systemd/system/dn42-roa-v6.timer"
cat << 'EOF' > /etc/systemd/system/dn42-roa-v6.timer
[Unit]
Description=Daily timer to download DN42 ROA (IPv6)

[Timer]
OnCalendar=*-*-* 00:05:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
EOF

# 3. 重新加载并启动 systemd 定时器
echo "🔄 重新加载 systemd 配置..."
systemctl daemon-reload

echo "🚀 启用并启动定时器..."
systemctl enable --now dn42-roa-v6.timer

# 可选：立即触发一次下载以确保配置正确
echo "📥 正在触发首次下载测试..."
systemctl start dn42-roa-v6.service

echo "✅ 配置完成！定时器状态如下："
systemctl list-timers | grep dn42-roa-v6
