# Problem Statement Document
### iOS Companion App for Android Set-Top Box (STB) — Remote Pairing & Cross-Device Keyboard Input

| | |
|---|---|
| **Document Type** | Problem Statement |
| **Product Idea** | iOS application for Android STB pairing and remote input |
| **Version** | 0.1 (Draft) |
| **Date** | 17 July 2026 |
| **Prepared For** | Product / Engineering Review |

---

## 1. Document Overview

This document defines the problem statement for a proposed iOS application that pairs with an Android-based Set-Top Box (STB) and acts as a remote control / companion device. It specifically expands on one core capability requested for the app: using the iPhone as a wireless keyboard input device for the Android STB, so that text typed on the iPhone is transferred in real time to whichever text field is currently focused on the Android STB screen.

## 2. Background and Context

Android-based STBs (smart TV boxes, hybrid set-top boxes, etc.) are typically operated using a physical remote control or an on-screen keyboard navigated via D-pad. Both input methods are slow and error-prone for anything beyond simple navigation — for example, entering search queries, Wi-Fi passwords, account credentials, or app store search terms.

Many STB platforms already support companion "remote control" mobile apps that emulate D-pad buttons over the same local network. The idea proposed here extends this pattern one step further: instead of the phone emulating a remote, the phone becomes a full keyboard input source, and text entry is synchronized live with whatever text field currently has focus on the Android STB.

## 3. Problem Statement

Users who operate an Android STB with a traditional remote control face significant friction when they need to type text, because:

- On-screen keyboards navigated with a D-pad are slow, requiring many directional key presses per character.
- There is no fast, reliable way to use a familiar, full-featured keyboard (such as the iPhone's own keyboard, with autocorrect, predictive text, and voice-to-text) to enter text into the STB.
- Existing remote-control companion apps mostly emulate simple button presses; they do not treat the phone as a live, focus-aware keyboard for whatever field the user is currently interacting with.
- Users must manually switch between using the remote to navigate and separately triggering an on-screen keyboard entry mode, breaking the flow of interaction.

There is currently no seamless mechanism by which an iOS device, once paired with an Android STB, can automatically detect that a text field has gained focus on the STB and immediately begin streaming typed input to that field, without extra manual steps.

## 4. Proposed Solution — Overview

Build an iOS application that:

1. Discovers and pairs with an Android STB on the same local network (or via Bluetooth, depending on final transport decision).
2. Provides standard remote-control functionality (navigation, playback controls, power, volume, etc.).
3. Adds a dedicated capability that turns the iPhone into an external, wireless keyboard for the STB: when any text field on the Android STB receives focus, the iOS app is notified and text typed on the iPhone is transmitted and reflected live in that Android text field.

## 5. Feature Deep-Dive: iOS Device as Wireless Keyboard for Android STB

### 5.1 Description

Once the iPhone and Android STB are paired, an on-focus event on any text field within the Android STB UI should trigger the iOS app to open its keyboard input mode automatically. Text entered on the iPhone should then be transferred to the Android STB and reflected in the focused field, effectively making the iPhone behave like an external hardware keyboard connected to the STB.

### 5.2 Intended User Flow

1. User pairs the iOS app with the Android STB (one-time setup).
2. User navigates the STB UI using the remote-control functions in the iOS app (or the STB's own remote).
3. User highlights/selects a text field on the STB — for example, a search box, login field, or Wi-Fi password field.
4. The Android side detects the focus event on that text field and notifies the paired iOS app over the active connection.
5. The iOS app automatically surfaces its keyboard input UI.
6. As the user types on the iPhone, each keystroke or text change is sent to the Android STB and rendered live in the focused field, similar to a Bluetooth hardware keyboard.
7. On submission (Enter/Done) or when focus moves away, the input session ends or hands off to the newly focused field.

### 5.3 Preconditions and Constraints — "This will only work when…"

> **Draft note:** your original message ended mid-sentence at *"this will only work when."* I've drafted the set of preconditions this feature would realistically depend on below — please review and tell me which to keep, remove, or replace.

- The iOS device and Android STB are actively paired and connected over a shared transport (local Wi-Fi network and/or Bluetooth).
- Both the Android STB and the iPhone have the companion app installed, running, and granted the necessary permissions (network access, and on Android, an Accessibility Service or Input Method integration to detect focus events and inject text).
- The Android STB app exposes a way to detect "text field focused" events system-wide (for example, via an Accessibility Service or a custom Input Method Editor) and to relay that event to the paired iOS app.
- The connection between the two devices remains active and within range/on the same network for the duration of text entry.
- Only one text field is focused at a time on the Android STB, so there is no ambiguity about where iOS keystrokes should be routed.
- The feature is scoped to text entry only; it is not intended to replace full remote navigation, media control, or gestures.

### 5.4 Key Technical Questions to Resolve

- Transport layer: Wi-Fi (local network sockets), Bluetooth, or both, and how failover between them is handled.
- Android-side integration model: Accessibility Service vs. a custom Input Method Editor (IME) vs. requiring the Android app itself to integrate an SDK.
- Latency requirements for keystroke-level syncing to feel "live" rather than laggy.
- Security: encrypting the pairing/connection and preventing an unpaired device from injecting text.
- Behavior when multiple STBs are paired, or when the connection drops mid-entry.
- Whether autocorrect/predictive text/voice-to-text on iOS should be supported and how they interact with the sync.

## 6. Assumptions

- The Android STB side is either an app the same team controls, or a platform that permits the level of system integration required (Accessibility Service or IME) to detect focus and inject text.
- Pairing between the iPhone and Android STB happens over a trusted local network or Bluetooth, with the two devices already discoverable to each other.
- The primary goal is faster, more comfortable text entry, not full remote-control replacement.

## 7. Success Criteria

- Text typed on the iPhone appears in the focused Android STB field with low, near-real-time latency.
- The keyboard-mode UI on iOS appears automatically when a text field is focused on the STB, without a separate manual trigger.
- The feature works reliably across the supported transport (Wi-Fi/Bluetooth) within normal home-network conditions.
- Users report materially faster text entry compared to using the STB's on-screen keyboard with a D-pad remote.

## 8. Out of Scope (for this phase)

- Full replacement of the physical STB remote for navigation and playback controls.
- Support for non-Android STB platforms (e.g., tvOS, Roku, Fire TV) in this phase.
- Voice-command based STB control (separate from voice-to-text typed via the iOS keyboard).

## 9. Open Questions for Stakeholder Input

1. Should pairing be Wi-Fi-only, Bluetooth-only, or support both with automatic fallback?
2. Does the Android STB app already exist, or is it being built alongside this iOS app?
3. Is there a specific set of STB devices/brands this needs to support, or is it a generic Android TV/STB target?
4. What is the acceptable latency threshold for the "live typing" experience?
