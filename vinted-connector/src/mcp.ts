const SUPPORTED_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"];

export interface ToolInfo {
  name: string;
  title: string;
  description: string;
  inputSchema: Record<string, unknown>;
}

export interface ToolResult {
  content: unknown[];
  isError?: boolean;
}

export interface McpServer {
  info: { name: string; title: string; version: string };
  instructions: string;
  tools: ToolInfo[];
  call(name: string, args: Record<string, unknown>): Promise<ToolResult>;
}

interface RpcRequest {
  jsonrpc: "2.0";
  id?: string | number | null;
  method: string;
  params?: any;
}

const rpcResult = (id: RpcRequest["id"], result: unknown) => ({ jsonrpc: "2.0", id, result });
const rpcError = (id: RpcRequest["id"], code: number, message: string) => ({ jsonrpc: "2.0", id, error: { code, message } });

export const errorResult = (text: string): ToolResult => ({ isError: true, content: [{ type: "text", text }] });

/** Stateless MCP server over Streamable HTTP (JSON responses, no SSE). */
export async function handleMcp(req: Request, server: McpServer): Promise<Response> {
  if (req.method !== "POST") {
    return new Response("Method Not Allowed", { status: 405, headers: { Allow: "POST" } });
  }
  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return Response.json(rpcError(null, -32700, "Parse error"), { status: 400 });
  }
  const batch = Array.isArray(body);
  const messages = (batch ? body : [body]) as RpcRequest[];
  const responses = [];
  for (const msg of messages) {
    // Notifications and client responses need no answer.
    if (msg.id === undefined || msg.id === null || !msg.method) continue;
    responses.push(await dispatch(msg, server));
  }
  if (responses.length === 0) return new Response(null, { status: 202 });
  return Response.json(batch ? responses : responses[0]);
}

async function dispatch(msg: RpcRequest, server: McpServer) {
  switch (msg.method) {
    case "initialize": {
      const requested = msg.params?.protocolVersion;
      return rpcResult(msg.id, {
        protocolVersion: SUPPORTED_VERSIONS.includes(requested) ? requested : SUPPORTED_VERSIONS[1],
        capabilities: { tools: { listChanged: false } },
        serverInfo: server.info,
        instructions: server.instructions,
      });
    }
    case "ping":
      return rpcResult(msg.id, {});
    case "tools/list":
      return rpcResult(msg.id, {
        tools: server.tools.map((t) => ({
          name: t.name,
          title: t.title,
          description: t.description,
          inputSchema: t.inputSchema,
          annotations: { title: t.title, readOnlyHint: true, destructiveHint: false, openWorldHint: true },
        })),
      });
    case "tools/call": {
      const name = msg.params?.name;
      if (!server.tools.some((t) => t.name === name)) return rpcResult(msg.id, errorResult(`Unbekanntes Tool: ${name}`));
      return rpcResult(msg.id, await server.call(name, msg.params?.arguments ?? {}));
    }
    default:
      return rpcError(msg.id, -32601, `Method not found: ${msg.method}`);
  }
}
