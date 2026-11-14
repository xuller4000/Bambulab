import { parse } from "node-html-parser";
import { errorResult, McpServer, ToolInfo } from "./mcp";

const UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36";
const MAX_IMAGES = 4;
/** Anonymous Vinted sessions stay valid for a while; renewing every 20 minutes keeps us well inside that. */
const SESSION_TTL_MS = 20 * 60_000;

const HOSTS: Record<string, string> = {
  de: "www.vinted.de",
  at: "www.vinted.at",
  fr: "www.vinted.fr",
  it: "www.vinted.it",
  es: "www.vinted.es",
  nl: "www.vinted.nl",
  be: "www.vinted.be",
  lu: "www.vinted.lu",
  pl: "www.vinted.pl",
  cz: "www.vinted.cz",
  sk: "www.vinted.sk",
  lt: "www.vinted.lt",
  pt: "www.vinted.pt",
  se: "www.vinted.se",
  dk: "www.vinted.dk",
  fi: "www.vinted.fi",
  uk: "www.vinted.co.uk",
};

/** Vinted's status_ids; the order matches the condition dropdown on the site. */
const CONDITIONS: Record<string, number> = {
  neu_mit_etikett: 6,
  neu: 1,
  sehr_gut: 2,
  gut: 3,
  zufriedenstellend: 4,
};

type Content = { type: "text"; text: string } | { type: "image"; data: string; mimeType: string };

const sessions = new Map<string, { cookie: string; expires: number }>();

function hostFor(country: string | undefined): string {
  const key = (country ?? "de").toLowerCase().replace(/^\./, "");
  const host = HOSTS[key === "co.uk" || key === "gb" ? "uk" : key];
  if (!host) throw new Error(`Unbekanntes Vinted-Land „${country}“. Möglich: ${Object.keys(HOSTS).join(", ")}.`);
  return host;
}

/**
 * The JSON API only answers with the cookies the start page hands out to anonymous visitors
 * (access_token_web and friends). They are cached per host for the lifetime of the isolate.
 */
async function session(host: string, renew = false): Promise<string> {
  const cached = sessions.get(host);
  if (cached && !renew && cached.expires > Date.now()) return cached.cookie;
  const res = await fetch(`https://${host}/`, {
    headers: { "User-Agent": UA, Accept: "text/html,application/xhtml+xml", "Accept-Language": "de-DE,de;q=0.9" },
  });
  if (res.status === 403 || res.status === 429) {
    throw new Error(`Vinted blockiert die Anfrage gerade (HTTP ${res.status} auf der Startseite, vermutlich Bot-Schutz).`);
  }
  // Workers keep each Set-Cookie header separate only through getAll.
  const cookies = res.headers
    .getAll("Set-Cookie")
    .map((c) => c.split(";")[0].trim())
    .filter((c) => c.includes("=") && !c.endsWith("="));
  if (!cookies.some((c) => c.startsWith("access_token_web="))) {
    throw new Error(`Vinted hat keine Sitzung ausgegeben (HTTP ${res.status}). Vermutlich greift der Bot-Schutz.`);
  }
  const cookie = cookies.join("; ");
  sessions.set(host, { cookie, expires: Date.now() + SESSION_TTL_MS });
  return cookie;
}

async function api(host: string, path: string): Promise<any> {
  for (const renew of [false, true]) {
    const res = await fetch(`https://${host}${path}`, {
      headers: {
        "User-Agent": UA,
        Accept: "application/json, text/plain, */*",
        "Accept-Language": "de-DE,de;q=0.9",
        Referer: `https://${host}/`,
        Cookie: await session(host, renew),
      },
    });
    // An expired or rejected session comes back as 401; one fresh session is worth a retry.
    if (res.status === 401 && !renew) continue;
    if (res.status === 403 || res.status === 429) {
      throw new Error(`Vinted blockiert die Anfrage gerade (HTTP ${res.status}). Später erneut versuchen.`);
    }
    if (!res.ok) throw Object.assign(new Error(`Vinted antwortete mit HTTP ${res.status} für ${path}.`), { status: res.status });
    return res.json();
  }
  throw new Error("Vinted lehnt die Sitzung ab (HTTP 401).");
}

const clean = (s: unknown) => String(s ?? "").replace(/\s+/g, " ").trim();

/** Prices show up either as {amount, currency_code} or as a plain string next to a currency field. */
function money(value: any, currency?: string): string {
  if (value === undefined || value === null || value === "") return "";
  const amount = typeof value === "object" ? value.amount : value;
  const cur = (typeof value === "object" ? value.currency_code : undefined) ?? currency ?? "EUR";
  const n = Number(amount);
  if (!Number.isFinite(n)) return clean(amount);
  return `${n.toFixed(2).replace(".", ",")} ${cur === "EUR" ? "€" : cur}`;
}

interface SearchArgs {
  query: string;
  country?: string;
  min_price?: number;
  max_price?: number;
  sort?: "relevance" | "newest_first" | "price_low_to_high" | "price_high_to_low";
  condition?: string[];
  page?: number;
  per_page?: number;
}

function searchParams(a: SearchArgs): URLSearchParams {
  const p = new URLSearchParams({ search_text: a.query.trim() });
  if (a.min_price !== undefined) p.set("price_from", String(a.min_price));
  if (a.max_price !== undefined) p.set("price_to", String(a.max_price));
  if (a.min_price !== undefined || a.max_price !== undefined) p.set("currency", "EUR");
  if (a.sort) p.set("order", a.sort);
  for (const c of a.condition ?? []) {
    const id = CONDITIONS[c];
    if (id) p.append("status_ids[]", String(id));
  }
  return p;
}

function formatItem(it: any, host: string): string {
  const price = money(it.price, it.currency);
  const total = money(it.total_item_price, it.currency);
  const priceText = total && total !== price ? `${price} (${total} mit Käuferschutz)` : price;
  const facts = [it.brand_title, it.size_title, it.status].map(clean).filter(Boolean);
  const favs = Number(it.favourite_count) > 0 ? `${it.favourite_count} ♥` : "";
  const seller = it.user?.login ? `von ${it.user.login}` : "";
  const url = it.url ? (String(it.url).startsWith("http") ? it.url : `https://${host}${it.url}`) : `https://${host}/items/${it.id}`;
  return [
    `- **${clean(it.title)}**${priceText ? ` · ${priceText}` : ""}`,
    `  ${[...facts, favs, seller].filter(Boolean).join(" · ")}`,
    `  ${url}`,
  ]
    .filter((l) => l.trim())
    .join("\n");
}

async function search(a: SearchArgs): Promise<Content[]> {
  if (!a.query?.trim()) return [{ type: "text", text: "Bitte einen Suchbegriff angeben." }];
  const host = hostFor(a.country);
  const page = Math.max(1, a.page ?? 1);
  const perPage = Math.min(96, Math.max(1, a.per_page ?? 24));
  const params = searchParams(a);
  const browserUrl = `https://${host}/catalog?${params}`;
  params.set("page", String(page));
  params.set("per_page", String(perPage));
  const data = await api(host, `/api/v2/catalog/items?${params}`);
  const items: any[] = data.items ?? [];
  if (!items.length) return [{ type: "text", text: `Keine Artikel für „${a.query}“ auf ${host} gefunden.\n${browserUrl}` }];
  const total = data.pagination?.total_entries;
  const head = `${total ? `${total} Treffer` : `${items.length} Treffer auf dieser Seite`} für „${a.query}“ auf ${host}, Seite ${page}.`;
  return [{ type: "text", text: `${head}\nSuche im Browser: ${browserUrl}\n\n${items.map((it) => formatItem(it, host)).join("\n\n")}` }];
}

function parseItemRef(input: string, country?: string): { host: string; id: string } {
  const v = input.trim();
  if (/^\d{5,}$/.test(v)) return { host: hostFor(country), id: v };
  const u = new URL(v.startsWith("http") ? v : `https://${v}`);
  if (!Object.values(HOSTS).includes(u.hostname) && !Object.values(HOSTS).includes(`www.${u.hostname}`)) {
    throw new Error("Nur Links von Vinted oder Artikel-IDs sind erlaubt.");
  }
  const id = u.pathname.match(/\/items\/(\d+)/)?.[1];
  if (!id) throw new Error("Im Link steckt keine Artikel-ID (erwartet …/items/1234567890-…).");
  return { host: u.hostname.startsWith("www.") ? u.hostname : `www.${u.hostname}`, id };
}

interface ItemDetails {
  lines: string[];
  photos: string[];
}

function fromApi(it: any, host: string): ItemDetails {
  const state = it.is_closed ? "Verkauft: " : it.is_reserved ? "Reserviert: " : it.is_hidden ? "Verborgen: " : "";
  const user = it.user ?? {};
  const rating =
    user.feedback_reputation !== undefined
      ? `${(Number(user.feedback_reputation) * 5).toFixed(1).replace(".", ",")}/5 aus ${user.feedback_count ?? "?"} Bewertungen`
      : "";
  const place = [user.city ?? it.city, user.country_title ?? it.country].map(clean).filter(Boolean).join(", ");
  const created = it.created_at_ts ? new Date(it.created_at_ts).toLocaleDateString("de-DE") : "";
  const price = money(it.price, it.currency);
  const total = money(it.total_item_price, it.currency);
  const details = [
    ["Marke", it.brand_dto?.title ?? it.brand],
    ["Größe", it.size_title ?? it.size],
    ["Zustand", it.status],
    ["Farbe", [it.color1, it.color2].filter(Boolean).join(", ")],
    ["Material", it.material?.title ?? it.material],
    ["Paketgröße", it.package_size?.title],
  ]
    .filter(([, v]) => clean(v))
    .map(([k, v]) => `${k}: ${clean(v)}`);
  const url = it.url ? (String(it.url).startsWith("http") ? it.url : `https://${host}${it.url}`) : `https://${host}/items/${it.id}`;
  const photos: string[] = (it.photos ?? []).map((p: any) => p.full_size_url ?? p.url).filter(Boolean);
  return {
    photos,
    lines: [
      `# ${state}${clean(it.title)}`,
      `Preis: ${price || "keine Angabe"}${total && total !== price ? ` (${total} mit Käuferschutz)` : ""}`,
      user.login ? `Verkäufer: ${user.login}${rating ? ` (${rating})` : ""}` : "",
      place ? `Ort: ${place}` : "",
      created ? `Eingestellt: ${created}` : "",
      [it.favourite_count !== undefined ? `${it.favourite_count} Favoriten` : "", it.view_count !== undefined ? `${it.view_count} Aufrufe` : ""]
        .filter(Boolean)
        .join(" · "),
      details.length ? `\n## Details\n${details.join("\n")}` : "",
      `\n## Beschreibung\n${String(it.description ?? "").trim() || "keine"}`,
      `\n${photos.length} Bilder · ${url}`,
    ],
  };
}

/** Fallback when the item API refuses: the product page carries JSON-LD and Open Graph tags. */
async function fromPage(host: string, id: string): Promise<ItemDetails | null> {
  const res = await fetch(`https://${host}/items/${id}`, {
    headers: { "User-Agent": UA, Accept: "text/html", "Accept-Language": "de-DE,de;q=0.9", Cookie: await session(host) },
  });
  if (res.status === 403 || res.status === 429) {
    throw new Error(`Vinted blockiert die Anfrage gerade (HTTP ${res.status}). Später erneut versuchen.`);
  }
  if (!res.ok) return null;
  const root = parse(await res.text());
  const meta = (p: string) => root.querySelector(`meta[property="${p}"]`)?.getAttribute("content") ?? "";
  let product: any;
  for (const s of root.querySelectorAll('script[type="application/ld+json"]')) {
    try {
      const data = JSON.parse(s.text);
      product = [data, ...(data["@graph"] ?? [])].find((d: any) => d?.["@type"] === "Product");
      if (product) break;
    } catch {
      // Broken JSON-LD blocks are skipped.
    }
  }
  const title = clean(product?.name) || clean(meta("og:title"));
  if (!title) return null;
  const offer = Array.isArray(product?.offers) ? product.offers[0] : product?.offers;
  const images = product?.image ? (Array.isArray(product.image) ? product.image : [product.image]) : [meta("og:image")];
  const sold = /SoldOut|OutOfStock/.test(String(offer?.availability ?? ""));
  return {
    photos: images.filter(Boolean),
    lines: [
      `# ${sold ? "Verkauft: " : ""}${title}`,
      `Preis: ${money(offer?.price, offer?.priceCurrency) || "keine Angabe"}`,
      product?.brand?.name ? `Marke: ${clean(product.brand.name)}` : "",
      offer?.itemCondition ? `Zustand: ${clean(String(offer.itemCondition).split("/").pop())}` : "",
      `\n## Beschreibung\n${clean(product?.description ?? meta("og:description")) || "keine"}`,
      `\n(Details aus der Artikelseite, die Vinted-API hat den Artikel nicht geliefert.) https://${host}/items/${id}`,
    ],
  };
}

function toBase64(buf: Uint8Array): string {
  let bin = "";
  for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode(...buf.subarray(i, i + 0x8000));
  return btoa(bin);
}

async function getItem(input: string, country: string | undefined, withImages: boolean): Promise<Content[]> {
  const { host, id } = parseItemRef(input, country);
  let details: ItemDetails | null = null;
  try {
    const data = await api(host, `/api/v2/items/${id}`);
    if (data?.item) details = fromApi(data.item, host);
  } catch (e) {
    // Blocks are reported as-is; any other API refusal falls back to the HTML page.
    if (/blockiert/.test((e as Error).message)) throw e;
  }
  details ??= await fromPage(host, id);
  if (!details) return [{ type: "text", text: `Artikel ${id} auf ${host} nicht gefunden (gelöscht oder verkauft?).` }];

  const content: Content[] = [{ type: "text", text: details.lines.filter((l) => l.trim()).join("\n") }];
  if (withImages) {
    for (const src of details.photos.slice(0, MAX_IMAGES)) {
      const img = await fetch(src, { headers: { "User-Agent": UA } });
      if (!img.ok) continue;
      const mime = (img.headers.get("Content-Type") ?? "image/jpeg").split(";")[0];
      if (!mime.startsWith("image/")) continue;
      content.push({ type: "image", data: toBase64(new Uint8Array(await img.arrayBuffer())), mimeType: mime });
    }
  }
  return content;
}

const COUNTRY_PROP = {
  type: "string",
  enum: Object.keys(HOSTS),
  description: "Vinted-Länderseite, Standard de (vinted.de)",
};

const TOOLS: ToolInfo[] = [
  {
    name: "search_items",
    title: "Vinted durchsuchen",
    description:
      "Sucht Artikel auf Vinted und liefert je Treffer Titel, Preis (auch inkl. Käuferschutz), Marke, Größe, Zustand, Favoriten, Verkäufer und Link.",
    inputSchema: {
      type: "object",
      properties: {
        query: { type: "string", description: "Suchbegriff, z. B. „Patagonia Fleece“ oder „Lego 42115“" },
        country: COUNTRY_PROP,
        min_price: { type: "number", minimum: 0, description: "Mindestpreis in Euro" },
        max_price: { type: "number", minimum: 0, description: "Höchstpreis in Euro" },
        sort: {
          type: "string",
          enum: ["relevance", "newest_first", "price_low_to_high", "price_high_to_low"],
          description: "Sortierung, Standard relevance",
        },
        condition: {
          type: "array",
          items: { type: "string", enum: Object.keys(CONDITIONS) },
          description: "Nur diese Zustände",
        },
        page: { type: "integer", minimum: 1, maximum: 50, description: "Ergebnisseite (Standard 1)" },
        per_page: { type: "integer", minimum: 1, maximum: 96, description: "Treffer pro Seite (Standard 24)" },
      },
      required: ["query"],
    },
  },
  {
    name: "get_item",
    title: "Vinted-Artikel anzeigen",
    description:
      "Liest einen einzelnen Vinted-Artikel: Preis, Verkäufer mit Bewertung, Ort, Details wie Marke, Größe, Zustand und Material, Beschreibung und optional die ersten Fotos.",
    inputSchema: {
      type: "object",
      properties: {
        url: { type: "string", description: "Link zum Artikel oder die Artikel-ID" },
        country: { ...COUNTRY_PROP, description: "Nur nötig, wenn statt eines Links eine ID übergeben wird (Standard de)" },
        images: { type: "boolean", description: `Die ersten ${MAX_IMAGES} Fotos mitliefern (Standard true)` },
      },
      required: ["url"],
    },
  },
];

export const vintedServer: McpServer = {
  info: { name: "vinted", title: "Vinted", version: "0.1.0" },
  instructions:
    "Lesender Zugriff auf Vinted ohne Login, Standard ist vinted.de. search_items für Suchen (Preisgrenzen, Zustand, " +
    "Sortierung), get_item für Details und Fotos eines Artikels. Immer den Link zum Artikel mitgeben. " +
    "Kaufen, Favorisieren und Nachrichten an Verkäufer kann dieser Connector nicht.",
  tools: TOOLS,
  async call(name, args) {
    try {
      if (name === "search_items") return { content: await search(args as unknown as SearchArgs) };
      return {
        content: await getItem(String(args.url ?? ""), args.country as string | undefined, args.images !== false),
      };
    } catch (e) {
      return errorResult(`Fehler: ${(e as Error).message}`);
    }
  },
};
