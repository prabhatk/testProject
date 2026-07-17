# Problem Statement: iOS Companion App for Android STB Wi-Fi Provisioning

## Background
Android-based set-top boxes (STBs) and similar headless/limited-input Android devices require an active internet connection to complete first-time setup (activation, license checks, content catalog sync, OTA updates, etc.). These devices typically lack a keyboard and rely on a remote control, making manual Wi-Fi SSID/password entry slow and error-prone.

## Problem
There is currently no fast, user-friendly way for a customer to provision Wi-Fi credentials from their iPhone to a new Android STB during out-of-box setup, forcing users to either:
- Type SSID/password using an on-screen keyboard with a D-pad remote, or
- Connect the STB via Ethernet (not always available/desired)

## Goal
Build an iOS companion app that securely transmits the user's Wi-Fi SSID and password to the Android STB during initial setup, so the STB can join the home network without manual on-screen credential entry.

## Chosen Approach
**Transport**: Bluetooth Low Energy (BLE), with the Android STB acting as a BLE peripheral / GATT server, and the iOS app acting as a BLE central / GATT client.

**Security**: App-layer ECDH (P-256) key exchange to derive a shared AES-GCM key, on top of the BLE transport. This protects the SSID/password even though BLE's own link-layer bonding is not solely relied upon, and also gives a foundation for verifying device identity if an out-of-band trust anchor (e.g. a pairing code shown on the STB) is added later.

**Network discovery**: rather than the user typing or confirming an SSID manually, the **STB performs its own Wi-Fi scan** (it is the device that actually needs to join the network) and reports the discovered networks — including each network's security type — back to the iOS app over GATT. The user then picks from a list instead of typing, and the app can show only the fields that network's security type actually requires.

**Architecture** (iOS side): SwiftUI + MVVM, layered as:
```
Presentation (Views + ViewModels)
      -> Domain (models + repository protocol)
      -> Data (repository implementation)
      -> Core (CoreBluetooth wrapper, CryptoKit wrapper)
```

## Detailed Provisioning Flow
1. iPhone discovers and connects to the STB over BLE; ECDH key exchange establishes a shared encryption key (as before).
2. iOS writes to a **scan-request characteristic** to trigger (or re-trigger, as a "refresh") a Wi-Fi scan on the STB.
3. The STB performs a local Wi-Fi scan (Android's `WifiManager` can scan without an active connection) and collects nearby SSIDs, their security type (Open / WPA2-Personal / WPA3-Personal / Enterprise), and signal strength.
4. The STB sends the results back over a **networks-list characteristic**, encrypted with the shared key. Because a scan can return 15–30+ networks — more than fits in one BLE packet — this list is sent across multiple chunked notifications with an explicit "list complete" marker.
5. iOS displays the networks as a picker, **plus a manual "enter network name" option** for hidden SSIDs, which never appear in any scan result.
6. The user selects a network. iOS shows a credential form tailored to that network's reported security type:
   - **Open** — no credentials needed, just a confirmation
   - **WPA2/WPA3-Personal** — password only
   - **Enterprise (802.1X)** — flagged as unsupported in this scope (see Out of Scope); the network still appears in the list but is not selectable for automated provisioning
7. iOS encrypts the selected SSID + credentials and writes them to the existing **credentials characteristic**.
8. The STB decrypts, attempts to join using the appropriate Android Wi-Fi API for that security type, and reports success/failure back over the existing **status characteristic**.

## Key Constraints
1. **No shared network initially** — the STB isn't on Wi-Fi yet, so iOS and the STB have no common IP network to talk over; BLE is used precisely because it doesn't require one.
2. **iOS platform restrictions** — iOS restricts programmatic Wi-Fi joining and background BLE/Wi-Fi behavior; this app only needs to *transmit* credentials over BLE, not join Wi-Fi itself, which avoids most of these restrictions.
3. **Android BLE peripheral support varies by chipset** — not every Android STB SoC supports BLE peripheral/GATT server (advertising) mode. This is the single biggest technical risk and must be confirmed against the actual target hardware before committing further engineering effort.
4. **Security** — credentials must never be transmitted or stored unencrypted on either side.
5. **STB counterpart required** — the Android side needs a `BluetoothGattServer` implementation exposing matching service/characteristic UUIDs, performing the matching ECDH derivation and AES-GCM decrypt, and attempting the Wi-Fi join.
6. **Scan result payload size** — a Wi-Fi scan can return more networks than fit in a single BLE packet; the networks-list characteristic must support chunked/paginated transfer.
7. **Hidden SSIDs never appear in a scan** — on either the STB or the iPhone. The UI must keep a manual "enter network name" fallback alongside the picker.

## Stakeholders
- End user (setting up STB at home)
- iOS app (this project's deliverable)
- Android STB software/firmware team (counterpart component, not covered by this app)

## Success Criteria

**Functional / Reliability**
- Provisioning succeeds on first attempt for a high percentage of users (target e.g. >95%) without needing a retry
- STB correctly reports back success/failure of the Wi-Fi join so the iOS app can confirm, not just "fire and forget"
- Graceful handling of wrong password — clear error surfaced back to iOS app, not a silent hang
- Works across common router setups: WPA2/WPA3, hidden SSIDs, 2.4GHz-only, dual-band
- Handles multiple STBs in range (e.g. apartment with two boxes) without cross-provisioning the wrong device
- STB-side Wi-Fi scan completes and is delivered to the iOS app within a reasonable time (e.g. under 10 seconds), with a visible "refresh" option
- Manual network-name entry remains available for hidden SSIDs not returned by any scan
- The credential form always matches the selected network's actual reported security type — never assumes password-only

**Time / UX**
- End-to-end provisioning completes within a target time window (e.g. under 60 seconds from scan to STB online)
- Minimal user steps: open app → pick device → confirm Wi-Fi → done
- Auto-fill SSID from the iPhone's currently joined network where technically possible, to avoid manual typing
- Clear visual/status feedback at each stage (searching → connecting → securing → sending → joining → confirmed)

**Security**
- Credentials never persisted unencrypted on either side after use
- Transport encrypted end-to-end via app-layer ECDH + AES-GCM, not reliant solely on BLE bonding
- Some form of device authentication/pairing (e.g. a displayed pairing code) so a rogue app can't provision an attacker's STB, and a rogue STB can't harvest credentials from a nearby phone

**Compatibility / Scale**
- Works across STB hardware/chipset variants, pending confirmation of BLE peripheral mode support (see Constraint 3)
- Backward compatible with STB firmware already in the field, or explicitly scoped to new firmware only
- iOS app works across supported iOS versions given Apple's Wi-Fi/BLE API restrictions per OS version

**Operational**
- Support/diagnosability: an in-app "why did this fail" path, since debugging Wi-Fi handshake issues remotely is hard
- Recovery path if provisioning fails midway — STB should not end up in a half-configured state; needs a retry or factory-reset path

**Business/Adoption**
- Reduces support call volume / setup abandonment rate compared to the current on-screen-keyboard flow (the underlying business justification, worth stating explicitly for a proposal or portfolio piece)

## Out of Scope
- Provisioning over cellular/hotspot-shared internet
- Multi-STB / fleet provisioning
- Enterprise Wi-Fi (802.1X) support — such networks may still appear in the STB's scan results and picker, but are flagged as unsupported rather than offered for automated credential entry
- **Captive portal networks** (public/hotel/hotspot Wi-Fi that requires a login page after joining) — the STB is a headless device with no way to interact with a portal login page, so this flow explicitly targets open/WPA2-PSK home networks only. STBs on a captive-portal network may report "joined Wi-Fi" while still lacking real internet access; this failure mode is documented but not handled in this scope.

## Assumptions
- The Android STB team can implement a matching `BluetoothGattServer` with the UUIDs and encryption scheme defined on the iOS side
- Target STB hardware supports BLE peripheral/advertising mode (to be confirmed — see Constraint 3)
- Users are provisioning STBs on their own home Wi-Fi network (WPA2/WPA3-PSK or open), not enterprise or captive-portal networks
