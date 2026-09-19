#!/bin/bash
# 忘了面板密码、又没有恢复码时的兜底重置脚本（在运行本容器的**宿主机**上执行，需要能读写数据目录）
#
# 用法：
#   bash reset_panel_password.sh              # 随机生成一个新密码并打印
#   bash reset_panel_password.sh 我的新密码    # 指定新密码
#   bash reset_panel_password.sh --help       # 看帮助（不会改任何东西）
#
# 默认配置路径 = 本脚本所在目录下的 data/config.json，
# 也可以用环境变量指定：TG_CONFIG=/path/to/config.json bash reset_panel_password.sh
#
# 效果：立即生效（无需重启容器）；同时生成新的恢复码；其它设备上的旧登录会失效。
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CFG="${TG_CONFIG:-$HERE/data/config.json}"

usage() {
  cat <<'EOF'
用法：
  bash reset_panel_password.sh                # 随机生成新密码并打印
  bash reset_panel_password.sh 我的新密码      # 指定新密码
  TG_CONFIG=/path/data/config.json bash reset_panel_password.sh [新密码]

说明：立即生效（不用重启容器）；会同时生成新的恢复码；其它设备上的旧登录失效。
EOF
}

# ⚠️ 安全闸：任何以 - 开头的参数都当选项处理，绝不当作密码。
# （2026-09-19 踩过：有人拿 --help 跑这个脚本，结果密码被设成字面量 "--help"）
case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  -*) echo "❌ 参数不能以 - 开头（看起来像命令行选项）：$1" >&2; usage; exit 2 ;;
  "") ;;
  *) [ ${#1} -ge 4 ] || { echo "❌ 新密码太短（至少 4 位）" >&2; exit 2; } ;;
esac

NEWPW="${1:-}"

python3 - "$CFG" "$NEWPW" <<'PY'
import hashlib, json, os, re, secrets, shutil, sys, time

cfg_path, newpw = sys.argv[1], (sys.argv[2] or '')
if not os.path.exists(cfg_path):
    sys.exit('找不到配置文件：%s\n（用 TG_CONFIG=/你的/data/config.json 指定正确路径）' % cfg_path)

cfg = json.load(open(cfg_path, encoding='utf-8'))
if not newpw:
    newpw = 'tg' + secrets.token_hex(4)

code = '-'.join(secrets.token_hex(2).upper() for _ in range(4))
salt = secrets.token_hex(8)
norm = re.sub(r'[^0-9a-z]', '', code.lower())

w = cfg.setdefault('web', {})
bak = cfg_path + '.bak-' + time.strftime('%Y%m%d-%H%M%S')
shutil.copy2(cfg_path, bak)

w['password'] = newpw
w['recovery_salt'] = salt
w['recovery_hash'] = hashlib.sha256(('%s|%s' % (salt, norm)).encode()).hexdigest()
w['pw_ver'] = int(w.get('pw_ver') or 1) + 1
w['pw_changed_at'] = time.strftime('%Y-%m-%d %H:%M:%S')

tmp = cfg_path + '.tmp'
with open(tmp, 'w', encoding='utf-8') as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
os.replace(tmp, cfg_path)
os.chmod(cfg_path, 0o600)

print('✅ 面板密码已重置（立即生效，不用重启容器）')
print('   新密码   : %s' % newpw)
print('   新恢复码 : %s' % code)
print('   备份     : %s' % bak)
print('   其它设备上的旧登录已失效，请用新密码登录。')
PY
