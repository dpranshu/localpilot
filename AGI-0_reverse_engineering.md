# AGI, Inc. (AGI-0 / "Hermes") — Reverse Engineering Report

**Target:** `com.agiinc.androidagent` v2.14.0 (build 686), installed from Google Play
**Device:** Redmi Pad — Xiaomi `22081283G` (codename *yunluo*), Android 14+ (targetSdk 36)
**Site:** https://theagi.company — product "AGI-0", on-device "action AI"
**Method:** ADB pull of base + arm64 split APK, static inventory of native libs / assets / dex strings. No Java tooling (jadx) available, so no full decompile — findings are from embedded strings, the manifest components, and the native-lib set.

---

## 1. What it actually is

A **screen-reading, screen-acting phone agent**. It uses the Android **Accessibility** API to read the on-screen UI tree and perform gestures, driven by a model that returns tool calls. The marketing says "on-device," but **this consumer build is cloud-backed** — there is **no bundled LLM, no llama.cpp / ONNX / ExecuTorch / MediaPipe / MNN** in the APK. The model (`agi-1-apex`) runs on AGI's servers; the phone is the eyes and hands.

The internal engine codename is **"Hermes"** (also "eve" appears). Everything agent-related is namespaced `hermes_*`.

## 2. Hard configuration (extracted verbatim from dex)

```
AppConfig(
  agentApiUrl   = https://androidagent.agi.tech
  apiBaseUrl    = https://workers.agi.tech        # Cloudflare Workers backend
  appName=AGI  appVersion=2.14.0  buildNumber=686
  runType=production   collectionType=automatic
  defaultModelKey = agi-1-apex                     # the model, referenced by key (remote)
  maxSteps = 150                                    # agent loop cap
  gitBranch=release-2.14  builtAt=2026-10-28  buildFlavor=prod
)
```
Other hosts: `api.agi.tech`, `https://agi.tech`.

## 3. Architecture (how a task runs)

```
 Voice/text prompt
        │
        ▼
 AccessibilityService  ──reads──►  UI tree + MediaProjection screenshot
 (AgentAccessibilityService)
        │  upload screen state
        ▼
 androidagent.agi.tech  (Hermes model "agi-1-apex")
        │  returns tool calls encoded as HTML comments:
        │   <!-- hermes_tool_call ... -->
        │   <!-- hermes_phone_result: {...} -->
        │   <!-- hermes_present_choices: [...] -->
        │   <!-- hermes_image_results: [...] -->
        ▼
 Client parses tool calls → performs gestures via Accessibility
 (click / long-click / swipe / scroll / type / back / home / open app)
        │
        └──loop up to maxSteps=150──► until task done
```

Key behaviors:
- **`/api/hermes/screenshot_relevance`** — the client asks the backend whether the current screen is relevant before/while acting (filters noise, reduces frames sent).
- **`/api/hermes/warmup`** — pre-warms a session/model.
- **Human-in-the-loop:** `/api/needs_you/{id}/complete` and `/unsnooze`, plus a `needs_you_card` UI — when the agent needs you (e.g. a login, a payment, a choice) it pauses and surfaces a card. `hermes_present_choices` renders pick-lists.
- **`maxSteps=150`** caps runaway loops.

## 4. Components (from the manifest)

| Component | Role |
|---|---|
| `service.AgentAccessibilityService` | **Core** — reads screen tree, executes gestures (`BIND_ACCESSIBILITY_SERVICE`) |
| `HermesNotificationListenerService` | Reads notifications (`BIND_NOTIFICATION_LISTENER_SERVICE`) |
| `agent.internal.AgentForegroundService` | Keeps the agent loop alive in background |
| `VoiceAgentActivity` / `VoiceSessionForegroundService` | Real-time voice mode |
| `SuggestedActionsSyncWorker`, `context_suggestions` | Proactive suggestions from context |
| `DevicePairingDestination` | Pair/control other devices |
| `BuyCreditsDestination` + RevenueCat | Paid credits / subscription |
| `MainActivity`, `ChatDestination`, `TaskInteractionActivity` | UI |

## 5. The stack (third-party SDKs found)

- **WebRTC** (`libjingle_peerconnection_so.so`, `wss://`) — real-time **voice agent** streaming (mic ↔ server). This is the "talk to it hands-free" path.
- **Clerk** (`com.clerk.api.*`) — authentication (sign-in/up, passkeys, MFA, OAuth, SSO) + **Google Sign-In** (GMS).
- **RevenueCat** (`a.revenue.cat`) — subscriptions / credit packs billing on top of Play Billing.
- **Firebase** (FCM push via `notification_devices/register`), **Sentry** (crash), **PostHog** (analytics + feature flags via `/api/feature_flag/local_evaluation`).
- **Room** (`AppDatabase`) — local DB for sessions/history.

## 6. Backend API surface (`workers.agi.tech` / `androidagent.agi.tech`)

```
POST /api/context_suggestions          # proactive suggestions from device context
     /api/memory     /api/memory/add     /api/memory/fact   # persistent user memory ("learns your preferences")
     /api/transcribe                    # speech-to-text
     /api/history_actions?limit=100     # past actions log
     /api/billing/entitlement?fresh=1   # credits/subscription check
     /api/hermes/screenshot_relevance   # is this screen relevant?
     /api/hermes/warmup                 # warm model/session
     /api/onboarding/first-task-cards   # onboarding suggestions
     /api/needs_you/{id}/complete|unsnooze   # human-in-the-loop
     /api/notification_devices/register # FCM token registration
     /api/feature_flag/local_evaluation # PostHog flags
```

The **"memory"** endpoints are how it "learns your preferences and gets better over time" — your facts/preferences are stored server-side and injected into the agent context.

## 7. Permissions it requests (and why)

- `BIND_ACCESSIBILITY_SERVICE` → read/act on any app's screen (the whole product)
- `SYSTEM_ALERT_WINDOW` → overlay UI while acting
- `MEDIA_PROJECTION` (`FOREGROUND_SERVICE_MEDIA_PROJECTION`) → screenshots for the vision model
- `RECORD_AUDIO` + `FOREGROUND_SERVICE_MICROPHONE` → voice agent
- `BIND_NOTIFICATION_LISTENER_SERVICE` → read notifications
- `ACCESS_FINE/COARSE_LOCATION`, `READ_CALENDAR`, `ACTIVITY_RECOGNITION`, `CAMERA` → context signals
- `INTERNET` + FCM → everything is network-dependent

## 8. Bottom line

- It is a **cloud agent with a thin, highly-privileged Android client**. The intelligence (`agi-1-apex` / Hermes) is remote; the phone contributes **screen capture + accessibility actuation + voice**.
- The clever/novel parts are: the **HTML-comment tool-call protocol**, the **`screenshot_relevance` gate** to cut frames, the **`needs_you` human-in-the-loop cards**, and **server-side memory** for personalization.
- "Works offline / on-device" as advertised on the site is **not what this Play build does** — it depends on `*.agi.tech`. An on-device model (the 2B–20B "AGI-0" + AGI-RUN engine) would ship as a downloaded model file or native lib; none is present in v2.14.0.

---

### To go deeper (optional next steps)
1. **Full decompile** — install JDK + jadx and decompile `classes.dex` to read `AgentAccessibilityService` and the Hermes tool parser line-by-line.
2. **Live traffic capture** — mitmproxy + a user cert to see the actual request/response payloads to `androidagent.agi.tech` (the exact prompt/screen format and tool schema).
3. **Confirm on-device claim** — watch the app's data dir (`/data/data/com.agiinc.androidagent/files`) during a task to see if any model is downloaded at runtime.

*Artifacts saved locally:* base + arm64 APKs and unpacked trees under the session scratchpad `agi_apk/`.
