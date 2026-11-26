# patches/

## 0001-no-uuid-discovery-filter-bluez-2282.patch

**What:** a two-line change to open_oura's `crates/oura-link/src/ble.rs`: start the BLE scan
**without** a service-UUID discovery filter (`ScanFilter { services: Vec::new() }`). The code
already checks each discovered device for the ring's service afterwards, so behaviour is the
same — the filtering just happens in our process instead of inside `bluetoothd`.

**Why:** on Linux, **BlueZ 5.87**'s `bluetoothd` crashes with SIGSEGV in `is_filter_match()`
when a client sets a UUID discovery filter (upstream issue
[bluez/bluez#2282](https://github.com/bluez/bluez/issues/2282), fixed by PR #2283 in BlueZ 5.88).
btleplug (used by the open_oura CLI) sets exactly that filter, so every scan/connect attempt
killed the Bluetooth daemon ("Remote peer disconnected" / "Operation already in progress",
core dumps from `bluetoothd`). With the patch, pairing and syncing work on 5.87.

**Do I need it?** Only if you use the **PC-mode CLI on Linux with BlueZ 5.87**. Check with
`bluetoothctl --version`. On 5.88+ (or macOS/Windows), you don't. The Android app doesn't use
this code path at all (`oura-link` is built without the `ble` feature there).

**Apply:** `scripts/fetch-upstream.sh --patch --build`
(or `git -C upstream/open_oura apply ../../patches/0001-no-uuid-discovery-filter-bluez-2282.patch`).

**Undo:** `git -C upstream/open_oura checkout -- crates/oura-link/src/ble.rs`.

License: MIT, same as open_oura (see NOTICE).
