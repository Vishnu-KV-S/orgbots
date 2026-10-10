"""What a bot "sees": the page as numbered interactive elements plus readable text.

DeepSeek reads text, not pixels, so the page is rendered into a compact listing:

    [3] button "Sign in"
    [4] input[email] "Email" value=""
    [5] link "Pricing" -> /pricing

and the bot answers with an element number. The numbers are written onto the DOM as
`data-bid`, so `click 3` resolves to exactly the node that was listed — not to a
selector re-derived from a label that might match two things.

Numbers are reassigned on every observation. An id from an earlier snapshot can point
at a different node after the page changes, which is why `act` always returns a fresh
snapshot and the bot is told to use only the latest one.

**A modal dialog is listed alone.** When one is open over the page (a followers list,
a share sheet, a confirmation), it is what a person is looking at and the page behind
it cannot be clicked. Sites render it at the end of `<body>`, so a listing in document
order used to fill its 150 elements and 4,000 characters of text with the page behind
and never reach the dialog — the bot saw nothing change when it opened one.

**Buttons that look alike say whose they are.** A list of people each with a "Follow"
button is a list of identical labels; an element whose label appears more than once
carries `near`, the text of the row it sits in, so the bot can tell which is which
(and `act` resuming an approved click can check it is still the same one).

**A long page's text is read from the scroll position.** A page whose text fits is
listed whole; a longer one from the top of the viewport down, a few screens deep, and
the listing says there is more above or below. Read from the top it was the same
sidebar and the start of the thread every time, however far the bot scrolled.

**An open menu is listed first, and controls say their state.** A dropdown's items
(radio and checkbox items included) come before the page behind, and a toggle reads
(on) or (off), a menu button (opens a menu). Without them a bot opened "Effort" again
and again, because nothing it could click ever appeared.

**A human-verification check is named** (`challenge`): Cloudflare's interstitial, a
CAPTCHA widget not yet answered. The bot does not solve one; it hands the screen to its
person, who answers it once, and the clearance stays in the profile's cookies.

**What is typed into a password box is never read back.** Not as its value, and not
as its label — an unlabelled input used to fall back to its value for a name, which
put a password a person had typed straight into the next prompt. A password box, a
one-time-code box and anything the vault filled (`data-vault-filled`) report only
that they are `filled`. `autocomplete`, `name`, `maxlength` and the enclosing form
are reported so `runtime.domain.vault.form_fields` can tell a login form from a
search box without guessing from the label alone.
"""

from __future__ import annotations

from typing import Any

MAX_ELEMENTS = 150
MAX_TEXT_CHARS = 6_000

CHALLENGE_JS = r"""
(shown) => {
  // A whole-page interstitial: Cloudflare's "Just a moment…" / "Performing security
  // verification", whose widget sits in a closed shadow root no selector reaches.
  if (window._cf_chl_opt || document.querySelector('#challenge-form, #challenge-stage')) {
    return 'cloudflare';
  }
  const title = document.title.trim().toLowerCase();
  if (title === 'just a moment...' || title.startsWith('attention required! | cloudflare')) {
    return 'cloudflare';
  }
  if (document.querySelector('#px-captcha')) return 'perimeterx';
  // A widget in the page. One that is answered has put its token in the form, and an
  // invisible one (reCAPTCHA v3, the badge in the corner) asks a person nothing.
  const answered = Array.from(document.querySelectorAll(
    '[name="cf-turnstile-response"], [name="g-recaptcha-response"], [name="h-captcha-response"]'
  )).some(el => el.value);
  if (answered) return null;
  const hosts = [
    ['challenges.cloudflare.com', 'cloudflare'], ['/recaptcha/', 'recaptcha'],
    ['hcaptcha.com', 'hcaptcha'], ['captcha-delivery.com', 'datadome'],
    ['arkoselabs.com', 'arkose'], ['funcaptcha.com', 'arkose'],
  ];
  for (const frame of document.querySelectorAll('iframe[src]')) {
    const hit = hosts.find(([host]) => frame.src.includes(host));
    if (hit && !frame.src.includes('size=invisible') && shown(frame)) return hit[1];
  }
  for (const el of document.querySelectorAll('.cf-turnstile')) {
    if (shown(el)) return 'cloudflare';
  }
  return null;
}
"""
"""Which human-verification check stands in front of the page, if any — the name of
its maker, or null. Not a solver and not a way around one: it is how the bot knows to
stop and hand the screen to its person (`graphs.bot_agent`)."""

SNAPSHOT_JS = r"""
({maxElements, maxText}) => {
  const challenge = __CHALLENGE_JS__;
  document.querySelectorAll('[data-bid]').forEach(el => el.removeAttribute('data-bid'));
  document.querySelectorAll('[data-bid-dialog]')
    .forEach(el => el.removeAttribute('data-bid-dialog'));
  const vw = window.innerWidth, vh = window.innerHeight;
  const shown = el => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return null;
    const st = window.getComputedStyle(el);
    const gone = st.visibility === 'hidden' || st.display === 'none';
    return gone || Number(st.opacity) === 0 ? null : r;
  };
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  // An element's words, spaced: innerText runs "alice" and "Alice Smith" in two spans
  // together as "aliceAlice Smith".
  const words = (el, skip) => {
    const parts = [];
    const walk = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    for (let t = walk.nextNode(); t; t = walk.nextNode()) {
      const host = t.parentElement;
      if (skip && skip.contains(t)) continue;
      if (host && host.checkVisibility && !host.checkVisibility()) continue;
      parts.push(t.nodeValue);
    }
    return clean(parts.join(' '));
  };
  // The top modal dialog, if one is open: the last visible one that is modal or big
  // enough to cover the page (a small role=dialog is a hover card or a tooltip).
  let dialog = null;
  const dialogs = 'dialog[open], [role=dialog], [role=alertdialog], [aria-modal="true"]';
  for (const el of document.querySelectorAll(dialogs)) {
    const r = shown(el);
    if (!r) continue;
    let modal = el.getAttribute('aria-modal') === 'true';
    try { modal = modal || el.matches(':modal'); } catch (e) { /* older engines */ }
    if (modal || r.width * r.height >= vw * vh * 0.15) dialog = el;
  }
  let dialogName = '';
  if (dialog) {
    dialog.setAttribute('data-bid-dialog', '1');
    const by = (dialog.getAttribute('aria-labelledby') || '').split(' ')[0];
    const ref = by && document.getElementById(by);
    const heading = dialog.querySelector('h1, h2, h3, [role=heading]');
    dialogName = clean(
      dialog.getAttribute('aria-label') || (ref && ref.innerText) || (heading && heading.innerText)
    ).slice(0, 80);
  }
  const root = dialog || document;
  const sel = [
    'a[href]', 'button', 'input:not([type=hidden])', 'textarea', 'select', 'summary',
    '[role=button]', '[role=link]', '[role=tab]', '[role=menuitem]', '[role=checkbox]',
    '[role=menuitemradio]', '[role=menuitemcheckbox]', '[role=radio]', '[role=treeitem]',
    '[role=option]', '[role=switch]', '[role=combobox]', '[role=textbox]', '[role=searchbox]',
    '[contenteditable=""]', '[contenteditable=true]', '[onclick]', '[tabindex]:not([tabindex="-1"])'
  ].join(',');
  const forms = Array.from(document.forms);
  const buttonish = new Set(['submit', 'button', 'reset']);
  const out = [];
  const nodes = [];
  let n = 0;
  // An open menu's items first. A menu is put at the end of <body>, so on a page with a
  // long sidebar the 150 elements ran out before it, and the bot that opened "Effort"
  // or "+" saw nothing appear, and opened it again.
  // Only a menu that floats over the page: a site's permanent nav bar may be role=menu.
  const floating = el => {
    for (let up = 0; el && up < 5; up += 1, el = el.parentElement) {
      const at = window.getComputedStyle(el).position;
      if (at === 'fixed' || at === 'absolute') return true;
    }
    return false;
  };
  const menus = Array.from(root.querySelectorAll('[role=menu], [role=listbox]')).filter(
    m => shown(m) && floating(m)
  );
  const candidates = [];
  for (const m of menus) candidates.push(...m.querySelectorAll(sel));
  candidates.push(...root.querySelectorAll(sel));
  const listed = new Set();
  for (const el of candidates) {
    if (out.length >= maxElements) break;
    if (listed.has(el)) continue;
    listed.add(el);
    const r = shown(el);
    if (!r) continue;
    // Keep what is on screen and a screen either side of it; the bot scrolls for the
    // rest. (A click scrolls its element into view, so one just above still works —
    // dropping it used to hide a profile's header the moment the bot scrolled down.)
    if (r.bottom < -vh || r.top > vh * 2 || r.right < 0 || r.left > vw) continue;
    n += 1;
    el.setAttribute('data-bid', String(n));
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    const autocomplete = (el.getAttribute('autocomplete') || '').toLowerCase();
    // An input's value is a label only when the input is a button.
    const valueAsLabel = tag === 'input' ? buttonish.has(type) : tag !== 'textarea';
    const label = (
      el.getAttribute('aria-label') || el.getAttribute('title') ||
      (el.labels && el.labels[0] && el.labels[0].innerText) ||
      el.getAttribute('placeholder') || el.getAttribute('alt') ||
      el.innerText || (valueAsLabel ? el.value : '') || ''
    ).replace(/\s+/g, ' ').trim().slice(0, 80);
    const holdsValue = tag === 'input' || tag === 'textarea' || tag === 'select';
    const sealed = type === 'password' || autocomplete.includes('one-time-code') ||
      autocomplete.startsWith('cc-') || el.hasAttribute('data-vault-filled');
    const raw = holdsValue ? String(el.value || '') : '';
    // Only what is set. A busy page lists 150 elements, and a result over the
    // gateway's artifact threshold is externalised — so empty fields cost real bytes.
    const item = {id: n, tag, label, in_view: r.top >= 0 && r.bottom <= vh};
    if (!item.in_view) item.where = r.bottom <= 0 ? 'above' : 'below';
    else {
      // On screen but not seen: scrolled out of a list's box, or under something.
      const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      if (hit && hit !== el && !el.contains(hit)) {
        item.in_view = false;
        item.where = 'hidden';
      }
    }
    const role = el.getAttribute('role');
    if (role) item.role = role;
    if (type) item.type = type;
    if (!sealed && raw) item.value = raw.slice(0, 80);
    if (sealed && raw) item.filled = true;
    if (holdsValue) {
      const name = (el.getAttribute('name') || el.getAttribute('id') || '').slice(0, 60);
      if (name) item.name = name;
      if (autocomplete) item.autocomplete = autocomplete;
      if (el.maxLength > 0 && el.maxLength < 100) item.maxlength = el.maxLength;
      if (el.form) item.form = forms.indexOf(el.form);
    }
    if (tag === 'a' && el.getAttribute('href')) item.href = el.getAttribute('href').slice(0, 120);
    // Toggles and menus say their state: a Research switch that is off, a button that
    // opens a menu — what a person sees at a glance and a label alone does not say.
    const ariaChecked = el.getAttribute('aria-checked') || el.getAttribute('aria-pressed');
    if (el.checked === true || ariaChecked === 'true') item.checked = true;
    else if (ariaChecked === 'false') item.off = true;
    const popup = el.getAttribute('aria-haspopup');
    if (popup && popup !== 'false') item.popup = true;
    if (el.getAttribute('aria-expanded') === 'true') item.expanded = true;
    if (el.disabled === true || el.getAttribute('aria-disabled') === 'true') item.disabled = true;
    out.push(item);
    nodes.push(el);
  }
  // Identical labels — a column of "Follow" buttons — get the text of the row each one
  // sits in: the nearest ancestor with more to say than the element itself.
  const seen = {};
  for (const item of out) {
    const key = item.label.toLowerCase();
    seen[key] = (seen[key] || 0) + 1;
  }
  out.forEach((item, i) => {
    if (seen[item.label.toLowerCase()] < 2) return;
    let p = nodes[i].parentElement;
    for (let up = 0; p && p !== document.body && up < 6; up += 1, p = p.parentElement) {
      if (clean(p.innerText).length > 400) break;
      const rest = words(p, nodes[i]);
      if (rest && rest !== item.label) {
        item.near = rest.slice(0, 70);
        break;
      }
    }
  });
  const body = dialog || document.body;
  let text = (body ? body.innerText : '').replace(/\n{3,}/g, '\n\n');
  let above = false, below = text.length > maxText;
  if (body && below) {
    // Too long to list whole: read from where the reader is. The top of innerText is
    // the sidebar and the start of the thread on every read of a long chat, so a bot
    // that scrolled to read an answer saw the same 4,000 characters each time and never
    // reached it. Text from the top of the viewport onward, a few screens deep, in
    // document order — so scrolling down is how the bot reads on.
    const blocks = new Map();
    const blockOf = el => {
      const start = el;
      if (blocks.has(start)) return blocks.get(start);
      while (el.parentElement && el !== body) {
        const display = window.getComputedStyle(el).display;
        if (!display.startsWith('inline') && display !== 'contents') break;
        el = el.parentElement;
      }
      blocks.set(start, el);
      return el;
    };
    const lines = [];
    let line = '', lineBlock = null, size = 0;
    below = false;
    const walk = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
    for (let t = walk.nextNode(); t && size < maxText; t = walk.nextNode()) {
      const host = t.parentElement;
      if (!host || !t.nodeValue.trim()) continue;
      if (/^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE)$/.test(host.tagName)) continue;
      if (host.checkVisibility && !host.checkVisibility()) continue;
      const r = host.getBoundingClientRect();
      if (r.bottom < 0) { above = true; continue; }
      if (r.top > vh * 3) { below = true; continue; }
      if (r.right < 0 || r.left > vw) continue;
      const block = blockOf(host);
      if (block !== lineBlock) {
        if (clean(line)) { lines.push(clean(line)); size += clean(line).length + 1; }
        line = '';
        lineBlock = block;
      }
      line += t.nodeValue;
    }
    if (clean(line)) lines.push(clean(line));
    if (size >= maxText) below = true;
    text = lines.join('\n');
  }
  return {
    url: location.href,
    title: document.title,
    dialog: dialog ? {name: dialogName} : null,
    menu: menus.length > 0,
    challenge: challenge(shown),
    elements: out,
    text: text.slice(0, maxText),
    text_above: above,
    text_truncated: below || text.length > maxText,
    scroll_y: Math.round(window.scrollY),
    scroll_height: Math.round(document.documentElement.scrollHeight),
    viewport_height: vh,
  };
}
""".replace("__CHALLENGE_JS__", CHALLENGE_JS.strip())


_TOGGLES = frozenset({"switch", "button", "menuitemcheckbox"})
"""Roles whose checked state reads as on/off rather than as a ticked box."""

_WHERE = {
    "above": " (above, scrolled past)",
    "below": " (below the fold)",
    "hidden": " (out of sight: scroll its list, or it is covered)",
}


def render(snapshot: dict[str, Any]) -> str:
    """The listing the model reads. Deterministic for a given snapshot."""
    lines = [
        f"URL: {snapshot.get('url', '')}",
        f"Title: {snapshot.get('title', '')}",
        (
            f"Scroll: {snapshot.get('scroll_y', 0)} of "
            f"{snapshot.get('scroll_height', 0)} (viewport {snapshot.get('viewport_height', 0)})"
        ),
    ]
    dialog = snapshot.get("dialog")
    if dialog:
        name = f" {dialog.get('name')!r}" if dialog.get("name") else ""
        lines.append(
            f"A dialog{name} is open over the page. Only the dialog is listed: the page "
            "behind it cannot be used until it is closed (its close button, or press Escape)."
        )
    if snapshot.get("menu"):
        lines.append(
            "A menu is open: its items are listed first. Choose one, open a submenu, or "
            "press Escape to close it."
        )
    if snapshot.get("challenge"):
        lines.append(
            f"This page is a human-verification check ({snapshot['challenge']}). A person "
            "has to answer it: do not click it, solve it or try to get around it."
        )
    lines += ["", "Interactive elements:"]
    for el in snapshot.get("elements", []):
        kind = el["tag"]
        if el.get("type"):
            kind += f"[{el['type']}]"
        if el.get("role"):
            kind += f"(role={el['role']})"
        line = f"[{el['id']}] {kind} {el.get('label', '')!r}"
        if el.get("near"):
            line += f" (beside {el['near']!r})"
        if el.get("value"):
            line += f" value={el['value']!r}"
        elif el.get("filled"):
            line += " (filled)"
        if el.get("href"):
            line += f" -> {el['href']}"
        if el.get("checked"):
            line += " (on)" if el.get("role") in _TOGGLES else " (checked)"
        elif el.get("off"):
            line += " (off)"
        if el.get("expanded"):
            line += " (open)"
        elif el.get("popup"):
            line += " (opens a menu)"
        if el.get("disabled"):
            line += " (disabled)"
        if not el.get("in_view"):
            line += _WHERE.get(str(el.get("where")), " (below the fold)")
        lines.append(line)
    if not snapshot.get("elements"):
        lines.append("(none)")
    lines += ["", "Dialog text:" if dialog else "Visible text:"]
    if snapshot.get("text_above"):
        lines.append("… (text above, scrolled past: scroll up to read it again)")
    lines.append(snapshot.get("text", ""))
    if snapshot.get("text_truncated"):
        lines.append("… (more text below: scroll down to read on)")
    return "\n".join(lines)
