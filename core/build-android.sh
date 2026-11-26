#!/usr/bin/env bash
# Build the Rust core for Android (arm64-v8a) and regenerate the Kotlin bindings
# into ../android/app/src/main/{jniLibs,java}. Called by the Gradle preBuild hook
# (android/app/build.gradle.kts) or by hand. See TOOLCHAIN.md.
set -euo pipefail
cd "$(dirname "$0")"

export ANDROID_HOME="${ANDROID_HOME:-$HOME/Android/Sdk}"
export ANDROID_NDK_HOME="${ANDROID_NDK_HOME:-$ANDROID_HOME/ndk/27.3.13750724}"
PROFILE="${1:-release}"
OUT=../android/app/src/main

run() { if command -v mise >/dev/null; then mise exec -- "$@"; else "$@"; fi; }

flag=(); [ "$PROFILE" = release ] && flag=(--release)
run cargo ndk -t arm64-v8a -P 29 -o "$OUT/jniLibs" build "${flag[@]}" --lib

# Bindings from the host build (unstripped → UniFFI metadata available).
run cargo build --lib
run cargo run -q --bin uniffi-bindgen -- generate \
  --library target/debug/libringkeeper_core.so \
  --language kotlin --config uniffi.toml --no-format \
  --out-dir "$OUT/java"
echo "ok: $OUT/jniLibs/arm64-v8a/libringkeeper_core.so + Kotlin bindings"
