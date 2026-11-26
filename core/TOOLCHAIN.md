# Android toolchain (no sudo)

Everything lives in the user's home; nothing system-wide.

| component | version | where / how |
| --- | --- | --- |
| Rust | 1.100.0 (rustup via mise, `core/mise.toml`, `android/mise.toml`) | target `aarch64-linux-android` (`rustup target add`) |
| cargo-ndk | 4.1.2 | `cargo install cargo-ndk --locked` → `~/.cargo/bin` |
| JDK | Temurin 17.0.21.1+2 | `mise` (`java = "temurin-17.0.21.1+2"` in `android/`/`core/` mise.toml) |
| Android cmdline-tools | 22.0 (`commandlinetools-linux-15859902_latest.zip`) | `~/Android/Sdk/cmdline-tools/latest` |
| SDK platform | android-35 rev 2 | `sdkmanager "platforms;android-35"` |
| build-tools | 35.0.0 | `sdkmanager "build-tools;35.0.0"` |
| platform-tools | 37.0.3 | `sdkmanager platform-tools` (adb) |
| NDK | r27d = 27.3.13750724 (LTS) | `sdkmanager "ndk;27.3.13750724"` |
| Gradle | 8.14.8 (wrapper, `distributionSha256Sum` pinned) | `android/gradlew` |
| AGP / Kotlin | 8.11.1 / 2.1.21 (+ compose plugin) | `android/build.gradle.kts` |
| UniFFI | 0.28.3 | `core/Cargo.lock` |
| rusqlite / libsqlite3-sys | 0.31.0 / 0.28.0 (`bundled`, compiled by NDK clang) | via oura-store |

Download verification:
- cmdline-tools zip from `https://dl.google.com/android/repository/commandlinetools-linux-15859902_latest.zip`,
  SHA-256 `4e4c464f145a7512b57d088ac6c278c03c9eea610886b35a5e0804e74eedf583` — matched the value
  published on https://developer.android.com/studio (at the time of writing). `sha256sum -c`: OK.
- SDK/NDK packages: installed by `sdkmanager`, which verifies each archive against the
  checksums in Google's signed repository manifest. Licenses accepted via `sdkmanager --licenses`.
- Gradle distribution: SHA-256 `94da8969…7af651` from services.gradle.org, pinned in
  `gradle-wrapper.properties` (wrapper refuses a mismatching zip).
- JDK: mise's Temurin plugin (Adoptium release asset).

Upstream: `core/` depends on open_oura **by path** (`../upstream/open_oura/crates/*`) at
commit `e57c43ce5fe0cf9a646afa790bda3731be767507`. `upstream/open_oura` is a git submodule;
run `scripts/fetch-upstream.sh` (or `git submodule update --init`) after cloning. The optional
BlueZ 5.87 workaround patch (`patches/`) only touches `oura-link/src/ble.rs`, which the
Android core doesn't use (`ble` feature off).

## Build

```sh
cd core
mise exec -- cargo test                 # host unit tests (fake ring, synthetic frames)
RINGKEEPER_TEST_DB=/tmp/copy.db mise exec -- cargo test real_db -- --nocapture   # optional, read-only
./build-android.sh                      # .so → android/app/src/main/jniLibs/arm64-v8a + Kotlin bindings
cd ../android
echo "sdk.dir=$HOME/Android/Sdk" > local.properties
mise exec -- ./gradlew assembleDebug    # preBuild runs core/build-android.sh (skip: -PskipRust)
# → android/app/build/outputs/apk/debug/app-debug.apk
```

Env used by `build-android.sh`: `ANDROID_HOME=~/Android/Sdk`,
`ANDROID_NDK_HOME=$ANDROID_HOME/ndk/27.3.13750724`, API level 29 (= minSdk).
Bindings are generated from the unstripped host lib (library mode); `uniffi.toml` sets
`android = true` (SystemCleaner on API 33+, JNA fallback below — needed for minSdk 29 lint).
