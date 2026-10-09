"""The marketplace — packaged skills, and connectors to well-known apps.

Shipped with the runtime as data, not fetched: a catalog that came from the network
would be instructions every bot follows, written by whoever controls that network.
Installing copies the skill into the organization's library as a `ready` skill the
person can then read and change like any other; nothing here runs on its own.

**Connectors** are remote MCP servers the vendors run (`domain.connectors`). Each entry
is the server's address and how it authenticates — none, or a token the person pastes
when installing. Servers that only accept an OAuth sign-in are not listed: the runtime
speaks tokens, not OAuth flows, yet. `checked` marks an entry this runtime has been
connected to; the others are as their vendors document them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from runtime.domain.skills import SkillBody


@dataclass(frozen=True, slots=True)
class CatalogSkill:
    key: str
    name: str
    category: str
    blurb: str
    body: SkillBody = field(default_factory=SkillBody)


def _skill(key: str, category: str, blurb: str, **body: object) -> CatalogSkill:
    return CatalogSkill(
        key=key, name=key, category=category, blurb=blurb, body=SkillBody.model_validate(body)
    )


SKILLS: tuple[CatalogSkill, ...] = (
    _skill(
        "research-brief",
        "Research",
        "A sourced one-page brief on any topic.",
        title="Research brief",
        when="Asked to research a topic, company, product or question and report back.",
        inputs="The topic and what the person wants to decide with it; any sources to prefer.",
        steps=[
            "Restate the question in one line and note what a good answer must cover.",
            "Search the web; open at least four independent, recent sources.",
            "Note each claim with its source link in your notes as you go.",
            "Look for disagreement between sources and note which is more credible and why.",
            "Write the brief to /research/<topic>.md: summary, key findings, open questions, "
            "sources.",
        ],
        checks="Every finding has a link to a page you actually opened; nothing is invented.",
        output="A 5-line summary in the chat and the path of the full brief.",
        approvals="None — research only reads.",
    ),
    _skill(
        "compare-products",
        "Shopping",
        "A side-by-side comparison table with prices and reviews.",
        title="Compare products",
        when="Asked to find or compare products, plans or vendors against criteria.",
        inputs="What is being bought, the budget, must-haves and nice-to-haves.",
        steps=[
            "List the criteria from the request; ask if the budget or must-haves are missing.",
            "Find 4-6 candidates from reputable retailers or the makers' sites.",
            "For each, record price (with currency and date), key specs and the review score.",
            "Drop candidates that miss a must-have, and say why.",
            "Write the table to /shopping/<item>.csv.",
        ],
        checks="Prices come from the product page itself, read today.",
        output="The top pick with one-line reasons, then the table's path.",
        approvals="Never add to cart or buy without asking.",
    ),
    _skill(
        "inbox-triage",
        "Productivity",
        "Sort an inbox into urgent, reply-needed and FYI.",
        title="Inbox triage",
        when="Asked to go through an email inbox or support queue.",
        inputs="Which inbox (signed in on the computer) and how far back to look.",
        steps=[
            "Open the inbox and list unread messages from the period asked about.",
            "Classify each: urgent, needs a reply, FYI, or junk.",
            "For each urgent or needs-a-reply message, note the sender, the ask and a "
            "suggested reply in one or two lines.",
            "Draft replies only where the person asked for drafts; leave them unsent.",
        ],
        checks="No message is marked read or archived unless the person asked for that.",
        output="Urgent items first with links, then reply-needed, then a count of FYI.",
        approvals="Sending, archiving or deleting any email.",
    ),
    _skill(
        "competitor-scan",
        "Research",
        "What changed on competitors' sites this week.",
        title="Competitor scan",
        when="Asked to watch competitors' websites, pricing or announcements.",
        inputs="The competitors and what to watch (pricing, features, blog, jobs).",
        steps=[
            "Read /competitors/last-scan.md for what was seen last time, if it exists.",
            "Visit each competitor's pricing page, product updates or blog, and news.",
            "Note anything new or changed since the last scan, with links.",
            "Overwrite /competitors/last-scan.md with what you saw today.",
        ],
        checks="A change is only reported if it differs from the last scan's notes.",
        output="A short list of changes per competitor; 'no change' is a valid answer.",
        approvals="None.",
    ),
    _skill(
        "price-watch",
        "Shopping",
        "Check a list of products for price drops.",
        title="Price watch",
        when="Asked to watch prices on specific product pages.",
        inputs="The product links and target prices, kept in /shopping/watch.csv.",
        steps=[
            "Read /shopping/watch.csv (link, target price, last price).",
            "Open each link and read the current price and availability.",
            "Update the last price column and append a dated line to /shopping/price-history.csv.",
        ],
        checks="The price read is the item's own price, not a bundle or a related item.",
        output="Only the items at or below their target, with links; otherwise one line.",
        approvals="Never buy.",
    ),
    _skill(
        "lead-list",
        "Sales",
        "Build a list of prospects that fit a profile.",
        title="Lead list",
        when="Asked to find companies or people matching an ideal customer profile.",
        inputs="The profile (industry, size, region, role) and how many leads.",
        steps=[
            "Confirm the profile and the number wanted.",
            "Search directories, company sites and news for matches.",
            "For each lead: company, website, size estimate, why it fits, and a public "
            "contact route (no guessing of personal emails).",
            "Append rows to /sales/leads.csv, skipping companies already in it.",
        ],
        checks="Each row's 'why it fits' cites something on a page you opened.",
        output="The count added and the file path.",
        approvals="Contacting anyone.",
    ),
    _skill(
        "meeting-actions",
        "Productivity",
        "Turn meeting notes into owners, actions and dates.",
        title="Meeting actions",
        when="Given meeting notes or a transcript to turn into follow-ups.",
        inputs="The notes (pasted, or a path in the team files).",
        steps=[
            "Read the notes once fully before extracting anything.",
            "List decisions made, then actions with an owner and due date where stated.",
            "Mark actions with no owner or date as 'unassigned' rather than guessing.",
            "Write /meetings/<date>-actions.md.",
        ],
        checks="Every action traces to a sentence in the notes.",
        output="Decisions and actions in the chat, and the file path.",
        approvals="Sending the actions to anyone.",
    ),
    _skill(
        "expense-receipts",
        "Finance",
        "Collect receipts from an inbox into an expense sheet.",
        title="Expense receipts",
        when="Asked to gather receipts or invoices for a period.",
        inputs="The inbox or portal (signed in) and the period.",
        steps=[
            "Search for receipts and invoices in the period.",
            "For each: date, vendor, amount, currency, category and a link.",
            "Append to /finance/expenses-<period>.csv, skipping duplicates by vendor, "
            "date and amount.",
        ],
        checks="Totals in the sheet match the receipts' totals.",
        output="Count and total per currency, and the file path.",
        approvals="Submitting an expense claim or paying anything.",
    ),
)


def catalog_skill(key: str) -> CatalogSkill | None:
    return next((s for s in SKILLS if s.key == key), None)


@dataclass(frozen=True, slots=True)
class CatalogConnector:
    key: str
    title: str
    category: str
    blurb: str
    url: str
    auth_kind: str = "none"
    header_name: str = ""
    key_help: str = ""
    """What to paste, and where to get it, for a server that needs a token."""
    optional_token: bool = False
    checked: bool = False


CONNECTORS: tuple[CatalogConnector, ...] = (
    CatalogConnector(
        "deepwiki",
        "DeepWiki",
        "Developer",
        "Ask questions about any public GitHub repository's code and docs.",
        "https://mcp.deepwiki.com/mcp",
        checked=True,
    ),
    CatalogConnector(
        "context7",
        "Context7",
        "Developer",
        "Up-to-date documentation and code examples for libraries.",
        "https://mcp.context7.com/mcp",
        auth_kind="header",
        header_name="CONTEXT7_API_KEY",
        key_help="Optional: an API key from context7.com for higher limits.",
        optional_token=True,
    ),
    CatalogConnector(
        "github",
        "GitHub",
        "Developer",
        "Issues, pull requests, code search and repositories.",
        "https://api.githubcopilot.com/mcp/",
        auth_kind="bearer",
        key_help="A GitHub personal access token (Settings → Developer settings).",
    ),
    CatalogConnector(
        "huggingface",
        "Hugging Face",
        "AI",
        "Search models, datasets, Spaces and papers.",
        "https://huggingface.co/mcp",
        auth_kind="bearer",
        key_help="Optional: a Hugging Face access token.",
        optional_token=True,
    ),
    CatalogConnector(
        "stripe",
        "Stripe",
        "Finance",
        "Customers, payments, invoices and the Stripe docs.",
        "https://mcp.stripe.com",
        auth_kind="bearer",
        key_help="A Stripe restricted API key (rk_…), scoped to what bots may touch.",
    ),
    CatalogConnector(
        "cloudflare-docs",
        "Cloudflare Docs",
        "Developer",
        "Search Cloudflare's documentation.",
        "https://docs.mcp.cloudflare.com/mcp",
    ),
    CatalogConnector(
        "microsoft-learn",
        "Microsoft Learn",
        "Developer",
        "Search Microsoft's official documentation.",
        "https://learn.microsoft.com/api/mcp",
    ),
    CatalogConnector(
        "exa",
        "Exa",
        "Research",
        "Web search and page contents, built for agents.",
        "https://mcp.exa.ai/mcp",
    ),
)


def catalog_connector(key: str) -> CatalogConnector | None:
    return next((c for c in CONNECTORS if c.key == key), None)
