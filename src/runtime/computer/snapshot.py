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
MAX_TEXT_CHARS = 4_000

SNAPSHOT_JS = r"""
({maxElements, maxText}) => {
  document.querySelectorAll('[data-bid]').forEach(el => el.removeAttribute('data-bid'));
  const sel = [
    'a[href]', 'button', 'input:not([type=hidden])', 'textarea', 'select', 'summary',
    '[role=button]', '[role=link]', '[role=tab]', '[role=menuitem]', '[role=checkbox]',
    '[role=option]', '[role=switch]', '[role=combobox]', '[role=textbox]',
    '[contenteditable=""]', '[contenteditable=true]', '[onclick]', '[tabindex]:not([tabindex="-1"])'
  ].join(',');
  const vw = window.innerWidth, vh = window.innerHeight;
  const forms = Array.from(document.forms);
  const buttonish = new Set(['submit', 'button', 'reset']);
  const out = [];
  let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    if (out.length >= maxElements) break;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const st = window.getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || Number(st.opacity) === 0) continue;
    // Keep what is on screen or just below it; the bot scrolls for the rest.
    if (r.bottom < -50 || r.top > vh * 2 || r.right < 0 || r.left > vw) continue;
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
    if (el.checked === true) item.checked = true;
    if (el.disabled === true) item.disabled = true;
    out.push(item);
  }
  const text = (document.body ? document.body.innerText : '').replace(/\n{3,}/g, '\n\n');
  return {
    url: location.href,
    title: document.title,
    elements: out,
    text: text.slice(0, maxText),
    text_truncated: text.length > maxText,
    scroll_y: Math.round(window.scrollY),
    scroll_height: Math.round(document.documentElement.scrollHeight),
    viewport_height: vh,
  };
}
"""


def render(snapshot: dict[str, Any]) -> str:
    """The listing the model reads. Deterministic for a given snapshot."""
    lines = [
        f"URL: {snapshot.get('url', '')}",
        f"Title: {snapshot.get('title', '')}",
        (
            f"Scroll: {snapshot.get('scroll_y', 0)} of "
            f"{snapshot.get('scroll_height', 0)} (viewport {snapshot.get('viewport_height', 0)})"
        ),
        "",
        "Interactive elements:",
    ]
    for el in snapshot.get("elements", []):
        kind = el["tag"]
        if el.get("type"):
            kind += f"[{el['type']}]"
        if el.get("role"):
            kind += f"(role={el['role']})"
        line = f"[{el['id']}] {kind} {el.get('label', '')!r}"
        if el.get("value"):
            line += f" value={el['value']!r}"
        elif el.get("filled"):
            line += " (filled)"
        if el.get("href"):
            line += f" -> {el['href']}"
        if el.get("checked"):
            line += " (checked)"
        if el.get("disabled"):
            line += " (disabled)"
        if not el.get("in_view"):
            line += " (below the fold)"
        lines.append(line)
    if not snapshot.get("elements"):
        lines.append("(none)")
    lines += ["", "Visible text:", snapshot.get("text", "")]
    if snapshot.get("text_truncated"):
        lines.append("… (text truncated; scroll or narrow down to read more)")
    return "\n".join(lines)
