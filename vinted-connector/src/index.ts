import { handleMcp } from "./mcp";
import { vintedServer } from "./vinted";

export interface Env {
  CONNECTOR_SECRET: string;
}

function safeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

const notFound = () => new Response("Not found", { status: 404 });

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const secret = (env.CONNECTOR_SECRET ?? "").trim();
    if (secret.length < 24) {
      return new Response("CONNECTOR_SECRET fehlt oder ist kürzer als 24 Zeichen.", { status: 500 });
    }
    // The only route is /<secret>/mcp; everything else looks like an empty site.
    const [prefix, route, ...rest] = new URL(req.url).pathname.split("/").filter(Boolean);
    if (!prefix || !safeEqual(prefix, secret) || route !== "mcp" || rest.length) return notFound();
    if (req.method === "GET" && (req.headers.get("Accept") ?? "").includes("text/html")) {
      return new Response("Vinted-Connector läuft. Diese URL in claude.ai als Custom Connector eintragen.", {
        headers: { "Content-Type": "text/plain; charset=utf-8" },
      });
    }
    return handleMcp(req, vintedServer);
  },
} satisfies ExportedHandler<Env>;
