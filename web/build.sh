#!/usr/bin/env bash
# Linux counterpart of web/build.ps1 — builds web/pkg (wasm32) or web/pkg64.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

RELEASE=0
WASM64=0
CPU_SHARE=0.5
while [[ $# -gt 0 ]]; do
  case "$1" in
    -Release|--release) RELEASE=1 ;;
    -Wasm64|--wasm64) WASM64=1 ;;
    -CpuShare) CPU_SHARE="${2:?}"; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

JOBS=$(python3 - <<PY
import os
print(max(1, int(os.cpu_count() * float("$CPU_SHARE"))))
PY
)
echo "Build: $JOBS of $(nproc) logical cores"

PROFILE=browser
[[ "$RELEASE" == 1 ]] && PROFILE=browser-release
if [[ "$WASM64" == 1 ]]; then
  TARGET=wasm64-unknown-unknown
  TARGET_DIR=target/browser64
  OUT=web/pkg64
  MAX_MEM=17179869184
  EXTRA64=' -Zcrate-attr=feature(simd_wasm64) --cfg getrandom_backend="custom"'
else
  TARGET=wasm32-unknown-unknown
  TARGET_DIR=target/browser
  OUT=web/pkg
  MAX_MEM=4294967296
  EXTRA64=''
fi

SIMD=''
[[ "$RELEASE" == 1 ]] && SIMD=' -C target-feature=+simd128'

export RUST_TOOLCHAIN="${RUST_TOOLCHAIN:-nightly-2026-10-29}"
export CARGO_PROFILE_DEV_DEBUG=0
export RUSTFLAGS="-C target-feature=+atomics,+bulk-memory,+mutable-globals -C link-arg=--shared-memory -C link-arg=--import-memory -C link-arg=--max-memory=${MAX_MEM}${SIMD}${EXTRA64}"

echo "cargo rustc → $OUT ($PROFILE / $TARGET)"
cargo "+$RUST_TOOLCHAIN" rustc -p horizon-client --bin horizon-client \
  --target "$TARGET" --profile "$PROFILE" --target-dir "$TARGET_DIR" \
  -j "$JOBS" -Z build-std=std,panic_abort \
  --no-default-features --features audio --locked \
  -- -C link-arg=-zstack-size=8388608 \
     -C link-arg=--export=__wasm_init_tls \
     -C link-arg=--export=__tls_size \
     -C link-arg=--export=__tls_align \
     -C link-arg=--export=__tls_base

mkdir -p "$OUT"
wasm-bindgen "$TARGET_DIR/$TARGET/$PROFILE/horizon-client.wasm" \
  --target web --out-dir "$OUT" --out-name horizon_client

echo "Built $OUT/horizon_client.js + horizon_client_bg.wasm"
ls -lh "$OUT"/horizon_client.js "$OUT"/horizon_client_bg.wasm
