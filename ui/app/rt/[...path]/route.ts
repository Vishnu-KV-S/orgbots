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
 *     GET                        /v1/observe/*, /v1/control/*, /v1/bots*, /v1/computer*, /healthz
 *     POST, PUT, PATCH, DELETE   /v1/control/*, /v1/bots*, /v1/computer* only
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
 * A method that is not one of the five is unroutable: Next only calls the
 * handlers a route file exports.
 */

const UPSTREAM = process.env.RUNTIME_API_URL ?? "http://127.0.0.1:8000";

const BOTS = ["v1/bots", "v1/computer"];
const READABLE = ["v1/observe/", "v1/control/", "healthz", ...BOTS];
const WRITABLE = ["v1/control/", ...BOTS];

export const dynamic = "force-dynamic";

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
        ...(hasBody
          ? { "content-type": request.headers.get("content-type") ?? "application/json" }
          : {}),
      },
      body,
      signal: request.signal,
      cache: "no-store",
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
