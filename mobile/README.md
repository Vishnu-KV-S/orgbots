# Orgbots for iOS and Android

The phone app for Orgbots: talk to your bots, approve what they want to do, hand them a
sign-in, and watch (or take over) their screen — from anywhere.

![The bots list, a finished task, an approval and a sign-in request](../docs/images/mobile.png)
<sub>Rendered against sample data.</sub>

| Bots | Chat | Approvals | Sign-in requests |
|---|---|---|---|
| Who is working, who is waiting on you, and what each one said last | Replies read full-width; steps fold into a live *Working · 6 steps* line | *Allow once*, *Always allow* or *Deny*, with the real URL, element and text | The fields the page asked for, typed straight into your server's vault |

Built with [Expo](https://expo.dev) (SDK 57, React Native, Expo Router, TypeScript).

## How it connects

The app uses the **same web server people open in a browser**. There is nothing new to
deploy and no API to expose: the app calls the web app's existing same-origin proxy,
`https://your-server/rt/v1/...`, exactly as the browser does.

```
iPhone / Android ──HTTPS──▶ Orgbots web app (:3000)  ──▶  API (:8000), private
                            /rt/v1/* proxy
```

- **No sign-in** (`RUNTIME_AUTH_MODE=none`): enter the address and you are in.
- **Team sign-in** (`RUNTIME_AUTH_MODE=members`): the app opens the server's own
  `/signin` page, so invitation links and single sign-on work unchanged. The session
  cookie the page sets is shared with the app's networking (iOS `sharedCookiesEnabled`,
  Android's system `CookieManager`), so the app is signed in from then on. An admin's
  sign-in link can also be pasted in (the clipboard button), or opened as
  `orgbots://signin?link=<token>`.

The server address is kept in the OS keychain; the session is an HttpOnly cookie the
app never reads.

## Features

- **Bots list** — live *Working…*, unread and *Waiting for you* states; search across
  names and last messages; long-press to pin or delete.
- **Chat** — a Grok-style conversation: full-width replies with links, **bold**, `code`
  and lists; your messages in bubbles; starter prompts drawn from the bot's duties;
  photo attachments (library or camera); long-press to copy or react; one tap to stop
  a bot mid-task.
- **Approvals and sign-ins** — the same cards as the web app, with haptics. Credential
  fields use the platform's password manager and one-time-code autofill.
- **Live screen** — the bot's browser, streamed. *Take control* pauses the bot: tap to
  click, type into the focused field, scroll, go back, or open an address — for the
  CAPTCHA or confirmation only a person can do. Leaving gives control back.
- **New bot** — the web app's starter templates, a name and a mission.
- Dark (true black) and light themes, following the system.

Setting up briefs, memory, files, skills, routines and apps stays in the web app (Settings
→ *Open the web app*).

## Run it

You need Node.js 20+ and a running Orgbots server (see the [main README](../README.md)).

```bash
cd mobile
npm install
npx expo start
```

Scan the QR code with **Expo Go** on your phone, then enter your server's address.
For a server on your own network, type `http://<your-computer's-LAN-IP>:3000` — not
`localhost`, which on the phone means the phone.

## Build installable apps

With an [Expo account](https://expo.dev/signup), [EAS Build](https://docs.expo.dev/build/introduction/)
builds in the cloud — no Xcode or Android Studio needed:

```bash
npx eas-cli@latest build --profile preview --platform android   # an .apk to install directly
npx eas-cli@latest build --profile production --platform all    # store builds
npx eas-cli@latest submit --platform ios                        # to App Store Connect
```

Change `ios.bundleIdentifier` and `android.package` in `app.json` (`dev.orgbots.app`) to
your own before publishing.

## Deploying the server for phones

- **Use HTTPS** on a public address. With `RUNTIME_AUTH_MODE=members`, set
  `RUNTIME_UI_URL` to that `https://` address: it makes the session cookie `Secure`,
  and it is where single sign-on returns to.
- Android builds allow plain HTTP (`usesCleartextTraffic`) so you can try the app
  against a server on your network; iOS requires HTTPS outside development.

## Checks

```bash
npm run typecheck
npm run lint
```

CI runs both and bundles the app for iOS and Android on every pull request.

## Not yet

- **Push notifications.** The server sends Web Push to the browser PWA; native push
  (APNs/FCM via Expo) needs a server-side sender and is the natural next step.
- Voice chat, group chats and the Files pane are web-only for now.
