import { type NextRequest } from "next/server";

/**
 * Same-origin proxy to the runtime API.
 *
 * It exists for two reasons. The browser gets a same-origin URL, so the API
 * needs no CORS configuration and stays a private service. And an SSE tail is
 * handed back as the upstream `ReadableStream` untouched — buffering it into a
 * string would turn a live tail into a request that never finishes.
 *
 * **This file is where the surface's shape is enforced, and it is asymmetric on
 * purpose:**
 *
 *     GET                        /v1/observe/*, /v1/control/*, /v1/bots*, /v1/computer*,
 *                                /v1/vault*, /v1/skills*, /v1/marketplace*, /v1/groups*,
 *                                /v1/connectors*, /v1/push*, /v1/templates*, /v1/auth*,
 *                                /v1/members*, /v1/admin*, /v1/x*, /healthz
 *     POST, PUT, PATCH, DELETE   /v1/control/*, /v1/bots*, /v1/computer*, /v1/vault*,
 *                                /v1/skills*, /v1/marketplace*, /v1/groups*,
 *                                /v1/connectors*, /v1/push*, /v1/templates*, /v1/auth*,
 *                                /v1/members*, /v1/admin*, /v1/x* only
 *
 * `/v1/observe` stays GET-only because it is read-only *by construction* — every
 * statement in `runtime/api/observe.py` is a SELECT — and a proxy that forwarded a
 * POST there would be claiming a capability the upstream does not have. It would
 * answer 405, so the refusal is not load-bearing; stating it here keeps the two
 * halves legible in one place.
 *
 * Writes go to `/v1/control`, which is not a hole in that guarantee: every verb
 * there goes through `RunService`, the kill switch, the spec compiler and the
 * spec-root confinement check. The viewer is no longer only a viewer, and the
 * honest statement of what it may do is this allowlist.
 *
 * `/v1/bots` and `/v1/computer` are the bot surface, and they are writable for the
 * same reason `/v1/control` is: every message typed to a bot becomes a run through
 * `RunService`, and every click a person makes on a bot's screen is refused by the
 * computer unless that person has taken control of it. The screenshot is the one
 * non-JSON response that passes through here; the body is forwarded untouched.
 *
 * `/v1/skills` and `/v1/marketplace` are the organization's library of how-tos that
 * every bot reads; writing one changes what bots are told, never what they may do.
 * `/v1/connectors` are the apps (MCP servers) bots may call; no response carries a
 * connector's token, only whether one is set.
 *
 * `/v1/vault` lists saved logins by site and hint and can delete one; no response
 * from it carries a value. A credential card's answer is a POST under `/v1/bots`,
 * and like every body it passes through here as text, unparsed and unlogged.
 *
 * **Signing in.** With members (`RUNTIME_AUTH_MODE=members`) the runtime knows who is
 * calling from the session cookie, so the cookie is forwarded — only it, not every
 * cookie the browser holds for this origin — and a response's `Set-Cookie` and
 * redirect come back untouched (single sign-on's callback is a redirect that sets the
 * session). A write whose `Origin` is another site is refused here: the cookie is
 * SameSite=Lax, and this is the second lock on the same door.
 *
 * A method that is not one of the five is unroutable: Next only calls the
 * handlers a route file exports.
 */

const UPSTREAM = process.env.RUNTIME_API_URL ?? "http://127.0.0.1:8000";

const BOTS = [
  "v1/bots",
  "v1/computer",
  "v1/vault",
  "v1/skills",
  "v1/marketplace",
  "v1/groups",
  "v1/connectors",
  "v1/push",
  "v1/templates",
  "v1/auth",
  "v1/members",
  "v1/admin",
  "v1/x",
];
const SESSION_COOKIE = "aor_session";
const READABLE = ["v1/observe/", "v1/control/", "healthz", ...BOTS];
const WRITABLE = ["v1/control/", ...BOTS];

export const dynamic = "force-dynamic";

/**
 * Whether a write's `Origin` is this site. It is compared with the host the browser
 * addressed (`Host`, or `X-Forwarded-Host` behind a proxy), not `request.nextUrl.origin`:
 * a server bound to 0.0.0.0, as in the single container, reports that as its origin,
 * and every same-site write from `localhost:3000` would be refused.
 */
function sameSite(request: NextRequest, origin: string) {
  const host = request.headers.get("x-forwarded-host") ?? request.headers.get("host");
  try {
    return host !== null && new URL(origin).host === host;
  } catch {
    return false;
  }
}

function refuse(target: string, method: string) {
  return Response.json(
    { detail: `refused: ${method} ${target} is not proxied` },
    { status: 403 },
  );
}

async function forward(request: NextRequest, target: string, allowed: string[]) {
  const permitted = allowed.some((prefix) =>
    prefix.endsWith("/")
      ? target.startsWith(prefix)
      : target === prefix || target.startsWith(`${prefix}/`),
  );
  if (!permitted) {
    return refuse(target, request.method);
  }
  const origin = request.headers.get("origin");
  if (request.method !== "GET" && origin && !sameSite(request, origin)) {
    return Response.json({ detail: "refused: a write from another site" }, { status: 403 });
  }
  const session = request.cookies.get(SESSION_COOKIE)?.value;

  const url = `${UPSTREAM}/${target}${request.nextUrl.search}`;
  // The body is read as text and passed through rather than parsed: this proxy has
  // no opinion about the payload, and one that re-serialised JSON would be a second
  // place a request could be changed on its way to the runtime.
  const hasBody = request.method !== "GET" && request.method !== "DELETE";
  const body = hasBody ? await request.text() : undefined;

  let upstream: Response;
  try {
    upstream = await fetch(url, {
      method: request.method,
      headers: {
        accept: request.headers.get("accept") ?? "application/json",
        ...(request.headers.get("x-organization-id")
          ? { "x-organization-id": request.headers.get("x-organization-id") as string }
          : {}),
        ...(session ? { cookie: `${SESSION_COOKIE}=${session}` } : {}),
        "user-agent": request.headers.get("user-agent") ?? "",
        ...(hasBody
          ? { "content-type": request.headers.get("content-type") ?? "application/json" }
          : {}),
      },
      body,
      signal: request.signal,
      cache: "no-store",
      redirect: "manual",
    });
  } catch (error) {
    // A dead runtime is the single most likely thing to be wrong, and "failed to
    // fetch" in a console is a worse answer than saying which URL was tried.
    return Response.json(
      { detail: `runtime API unreachable at ${UPSTREAM}`, cause: String(error) },
      { status: 502 },
    );
  }

  const headers = new Headers();
  const contentType = upstream.headers.get("content-type");
  if (contentType) headers.set("content-type", contentType);
  headers.set("cache-control", "no-store, no-transform");
  for (const cookie of upstream.headers.getSetCookie()) headers.append("set-cookie", cookie);
  const location = upstream.headers.get("location");
  if (location) headers.set("location", location);

  return new Response(upstream.body, { status: upstream.status, headers });
}

// `RouteContext<'/rt/[...path]'>` is a global helper Next generates types for during
// `next dev` / `next build`, so the params shape follows the route rather than being
// restated here and drifting from it.
type Context = RouteContext<"/rt/[...path]">;

export async function GET(request: NextRequest, context: Context) {
  const { path } = await context.params;
  return forward(request, path.join("/"), READABLE);
}

export async function POST(request: NextRequest, context: Context) {
  const { path } = await context.params;
  return forward(request, path.join("/"), WRITABLE);
}

export async function PUT(request: NextRequest, context: Context) {
  const { path } = await context.params;
  return forward(request, path.join("/"), WRITABLE);
}

export async function PATCH(request: NextRequest, context: Context) {
  const { path } = await context.params;
  return forward(request, path.join("/"), WRITABLE);
}

export async function DELETE(request: NextRequest, context: Context) {
  const { path } = await context.params;
  return forward(request, path.join("/"), WRITABLE);
}
