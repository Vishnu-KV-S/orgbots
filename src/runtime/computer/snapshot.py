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
    const label = (
      el.getAttribute('aria-label') || el.getAttribute('title') ||
      (el.labels && el.labels[0] && el.labels[0].innerText) ||
      el.getAttribute('placeholder') || el.getAttribute('alt') ||
      el.innerText || el.value || ''
    ).replace(/\s+/g, ' ').trim().slice(0, 80);
    out.push({
      id: n,
      tag,
      role: el.getAttribute('role') || '',
      type: el.getAttribute('type') || '',
      label,
      value: (tag === 'input' || tag === 'textarea' || tag === 'select')
        ? String(el.value || '').slice(0, 80) : '',
      href: tag === 'a' ? (el.getAttribute('href') || '').slice(0, 120) : '',
      checked: el.checked === true,
      disabled: el.disabled === true,
      in_view: r.top >= 0 && r.bottom <= vh,
    });
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
