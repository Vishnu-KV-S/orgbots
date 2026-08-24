import { type NextRequest } from "next/server";

/**
 * Read-only proxy to the runtime's `/v1/observe` surface.
 *
 * It exists for two reasons. The browser gets a same-origin URL, so the API
 * needs no CORS configuration and stays a private service. And an SSE tail is
 * handed back as the upstream `ReadableStream` untouched — buffering it into a
 * string would turn a live tail into a request that never finishes.
 *
 * GET only, and the path is checked against a prefix. A proxy that forwarded
 * any method to any path would be a way to start runs through a viewer that has
 * none of the checks the control surface has.
 */

const UPSTREAM = process.env.RUNTIME_API_URL ?? "http://127.0.0.1:8000";
const ALLOWED = ["v1/observe/", "healthz"];

export const dynamic = "force-dynamic";

export async function GET(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
) {
  const { path } = await context.params;
  const target = path.join("/");

  if (!ALLOWED.some((prefix) => target === prefix || target.startsWith(prefix))) {
    return Response.json({ detail: `refused: ${target}` }, { status: 403 });
  }

  const url = `${UPSTREAM}/${target}${request.nextUrl.search}`;
  let upstream: Response;
  try {
    upstream = await fetch(url, {
      headers: { accept: request.headers.get("accept") ?? "application/json" },
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
