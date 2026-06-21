#!/bin/bash
# 一次性：生成一个固定的自签名「代码签名」证书并导入登录钥匙串。
# 之后 build.sh 会用它签名，App 身份稳定 → 重编译不再丢屏幕录制权限。
set -e

IDENTITY="ScreenRecorder Self-Signed"
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"

if security find-identity -p codesigning "$KEYCHAIN" 2>/dev/null | grep -q "$IDENTITY"; then
  echo "✅ 签名身份已存在：$IDENTITY，无需重复创建。"
  exit 0
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "生成自签名代码签名证书..."
openssl req -x509 -newkey rsa:2048 -keyout "$WORK/key.pem" -out "$WORK/cert.pem" \
  -days 7300 -nodes -sha256 \
  -subj "/CN=$IDENTITY" \
  -addext "basicConstraints=critical,CA:false" \
  -addext "keyUsage=critical,digitalSignature" \
  -addext "extendedKeyUsage=critical,codeSigning"

# -legacy + -macalg sha1：兼容 macOS security 的 PKCS12 解析（OpenSSL3 默认算法它认不了）
openssl pkcs12 -export -inkey "$WORK/key.pem" -in "$WORK/cert.pem" \
  -out "$WORK/cert.p12" -passout pass:tmp123 -name "$IDENTITY" \
  -legacy -macalg sha1

echo "导入登录钥匙串..."
# -A: 允许本机程序（含 codesign）直接使用该私钥，避免每次签名弹窗
security import "$WORK/cert.p12" -k "$KEYCHAIN" -P tmp123 -A

echo
echo "=== 现有代码签名身份 ==="
security find-identity -p codesigning "$KEYCHAIN" | grep "$IDENTITY" || true
echo "✅ 完成。现在可运行 bash build.sh 用该身份签名。"
