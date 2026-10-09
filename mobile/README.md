# Orgbots for iOS and Android

The phone app for Orgbots, built with [Flutter](https://flutter.dev). Talk to your bots,
approve what they want to do, hand them a sign-in, and watch (or take over) their screen,
from anywhere. The bots are **the same animated 3D bots as the web app**.

![The bots list, a finished task, an approval and a sign-in request](../docs/images/mobile.png)
<sub>Rendered against sample data.</sub>

## The 3D bots

The web app draws its bots with three.js (`ui/features/bots/avatar`). Rather than
redraw them in Dart, the app ships **that same code**: `bot3d/` bundles the web app's
`BotScene` — camera, studio lighting, `BotModel`, moods — into one HTML file,
`assets/bot3d/index.html`, which the app shows in a transparent web view. A bot on the
phone has the same shape, eyes, colours and animations as in the browser.

- **Live 3D** (`Bot3D`): the conversation header and the big bot on an empty
  conversation act out what the bot is doing — browsing, typing, thinking, waiting on
  you, happy when it is done — from the same mood rules as the web.
- **Lists** (`BotAvatar`): one web view per row would be too heavy, so a single hidden
  renderer draws each bot in its current mood to a picture, cached on disk. A working
  bot gets a soft pulse in its own colour.
- Where WebGL is unavailable, a flat face in the bot's colours stands in, as on the web.

After changing the bots in `ui/features/bots/avatar`, rebuild the asset:

```bash
cd ui && npm ci                       # the bundle uses ui's three.js and React
cd ../mobile/bot3d && npm ci && npm run build
```

CI rebuilds it on every pull request and fails if the committed file is stale.

## How it connects

The app uses the **same web server people open in a browser**: it calls the web app's
existing same-origin proxy, `https://your-server/rt/v1/...`. There is nothing new to
deploy, and the API stays private behind the web app.

- **No sign-in** (`RUNTIME_AUTH_MODE=none`): enter the address and you are in.
- **Team sign-in** (`RUNTIME_AUTH_MODE=members`): the app opens the server's own
  `/signin` page in a web view, so invitation links and single sign-on work unchanged.
  When that page lets you in, the app takes the session cookie it set, keeps it in the
  phone's keychain, and sends it with every request. An admin's sign-in link can be
  pasted in with the clipboard button.

## Features

- **Bots list**: who is working, who is waiting for you, unread replies and last
  messages; search; long-press to pin or delete.
- **Chat**: full-width replies with links, **bold**, `code` and lists; your messages in
  bubbles; steps folded into a live *Working · 6 steps* line; starter prompts from the
  bot's duties; photo attachments; long-press to copy or react; stop a bot mid-task.
- **Approvals and sign-ins**: *Allow once*, *Always allow* or *Deny* with the real URL,
  element and text; sign-in cards that work with the phone's password manager and
  one-time-code autofill, sent straight to the server's vault.
- **Live screen**: the bot's browser, streamed. *Take control* pauses the bot: tap to
  click, type, scroll, go back or open an address. Leaving gives control back.
- **New bot** from the web app's templates, with a live 3D preview.
- Dark (true black) and light themes, following the system.

Briefs, memory, files, skills, routines and apps are set up in the web app (Settings →
*Open the web app*).

## Run it

You need [Flutter](https://docs.flutter.dev/get-started/install) (3.47 or newer) and a
running Orgbots server (see the [main README](../README.md)).

```bash
cd mobile
flutter pub get
flutter run            # on a connected phone or a simulator
```

Enter your server's address. For a server on your own network, use
`http://<your-computer's-LAN-IP>:3000`, not `localhost` (on the phone, that is the phone).

## Build installable apps

```bash
flutter build apk --release        # Android: build/app/outputs/flutter-apk/app-release.apk
flutter build appbundle --release  # Android: for Google Play
flutter build ipa --release        # iOS: needs a Mac with Xcode and a signing team
```

Every CI run also builds an Android APK you can download from the run's artifacts and
install directly. Before publishing to the stores, change the app id `dev.orgbots.app`
(`android/app/build.gradle.kts`, and the bundle identifier in Xcode) to your own, and
set up release signing.

## Deploying the server for phones

- **Use HTTPS** on a public address, and with `RUNTIME_AUTH_MODE=members` set
  `RUNTIME_UI_URL` to that `https://` address: it makes the session cookie `Secure` and
  is where single sign-on returns to.
- Plain HTTP works for trying the app against a server on your own network (Android
  allows it; on iOS, local network addresses only).

## Checks

```bash
flutter analyze
flutter test
```

The tests drive the real screens against a fake Orgbots server, and check that bots
with no saved look get exactly the colours the web app derives for them.

## Not yet

- **Push notifications.** The server sends Web Push to the browser app; native push
  (APNs and FCM) needs a sender on the server and is the natural next step.
- Voice chat, group chats and the Files pane are web-only for now.
