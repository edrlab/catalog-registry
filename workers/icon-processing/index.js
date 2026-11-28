  // The icon Worker: turns an uploaded icon into a PNG and stores it in R2. See docs/icons.md.
  // Deployed by pasting this file into the Worker's editor in the Cloudflare dashboard.
  //
  // PUT /<catalog-id>, body = the image bytes, header Authorization: Bearer <ICON_WORKER_SECRET>.
  //   200 {"key": "<catalog-id>-icon.png", "etag": "..."}
  //   400 not a catalog id · 401 no or wrong secret · 405 not PUT or DELETE · 413 empty or over 5 MB
  //   422 not an image that can be read (the caller's file)
  //   500 the Worker is misconfigured · 502 Images or R2 failed (ours, not the caller's)
  // DELETE /<catalog-id>, same header: removes <catalog-id>-icon.png. 204, also when it was gone.
  //   Errors are RFC 9457 problem details (application/problem+json), like the registry's own.
  //
  // Bindings: Images as IMAGES, R2 bucket `catalog-registry` as ICONS. Secret: ICON_WORKER_SECRET.
  
  const SIZE = 512; // icons fit in a SIZE x SIZE box
  const MAX_BYTES = 5 * 1024 * 1024; // the registry refuses larger before sending
  const MAX_SIDE = 12000; // Cloudflare Images' own limit; refused here as the caller's error
  const CATALOG_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
  
  export default {
    async fetch(request, env) {
      if (request.method !== "PUT" && request.method !== "DELETE") {
        return reply(405, "Method not allowed");
      }
      if (!env.IMAGES || !env.ICONS) {
        console.error("icon worker: IMAGES or ICONS binding missing");
        return reply(500, "Worker misconfigured");
      }
      if (!authorised(request, env)) return reply(401, "Unauthorized");
  
      const id = new URL(request.url).pathname.slice(1);
      if (!CATALOG_ID.test(id)) return reply(400, "Not a catalog id");
      const key = `${id}-icon.png`;
      if (request.method === "DELETE") {
        try {
          await env.ICONS.delete(key); // R2 deletes are idempotent: a missing key is not an error
          return new Response(null, { status: 204 });
        } catch (error) {
          console.error("icon worker: deleting failed", key, String(error));
          return reply(502, "Deleting failed");
        }
      }
      const declared = Number(request.headers.get("content-length"));
      if (!(declared > 0) || declared > MAX_BYTES) return reply(413, "Send an image of at most 5 MB");
  
      // Read once (at most 5 MB): checked first, then converted.
      const bytes = await request.arrayBuffer();
      if (bytes.byteLength === 0 || bytes.byteLength > MAX_BYTES) {
        return reply(413, "Send an image of at most 5 MB");
      }
  
      // Is it an image at all? `info` is free. A failure here is the caller's file, not our service.
      let info;
      try {
        info = await env.IMAGES.info(asStream(bytes));
      } catch {
        return reply(422, "Not an image that can be read");
      }
      // Images reads SVG but does not rasterise it, so it is the caller's format, not our failure.
      // Checked on the bytes too: SVG/XML is text starting with "<"; PNG, JPEG, GIF and WebP never do.
      if (looksLikeText(bytes) || String(info.format).toLowerCase().includes("svg")) {
        return reply(422, "SVG cannot be converted; send PNG, JPEG, WebP or GIF");
      }
      if (info.width > MAX_SIDE || info.height > MAX_SIDE) {
        return reply(422, `Images are at most ${MAX_SIDE} px on a side`);
      }
  
      let png;
      try {
        const result = await env.IMAGES.input(asStream(bytes))
          .transform({ width: SIZE, height: SIZE, fit: "contain" })
          .output({ format: "image/png" });
        png = await result.response().arrayBuffer(); // bytes, not a stream: R2 needs the length
      } catch (error) {
        // A readable image that fails to convert: quota, outage, or a format Images rejects.
        console.error("icon worker: conversion failed", id, info.format, String(error));
        return reply(502, "Conversion failed");
      }
  
      try {
        const stored = await env.ICONS.put(key, png, { httpMetadata: { contentType: "image/png" } });
        return Response.json({ key, etag: stored.etag });
      } catch (error) {
        console.error("icon worker: storing failed", key, String(error));
        return reply(502, "Storing failed");
      }
    },
  };
  
  // Constant time, and closed when the secret is missing or is not text (e.g. added as a binding).
  function authorised(request, env) {
    if (typeof env.ICON_WORKER_SECRET !== "string" || env.ICON_WORKER_SECRET.length < 32) return false;
    const encoder = new TextEncoder();
    const expected = encoder.encode(`Bearer ${env.ICON_WORKER_SECRET}`);
    const given = encoder.encode(request.headers.get("authorization") ?? "");
    return expected.byteLength === given.byteLength && crypto.subtle.timingSafeEqual(expected, given);
  }
  
  function looksLikeText(bytes) {
    const head = new TextDecoder().decode(bytes.slice(0, 256)).replace(/^\uFEFF/, "").trimStart();
    return head.startsWith("<");
  }
  
  function asStream(bytes) {
    return new Response(bytes).body;
  }
  
  const TITLES = {
    400: "Bad Request",
    401: "Unauthorized",
    405: "Method Not Allowed",
    413: "Content Too Large",
    422: "Unprocessable Content",
    500: "Internal Server Error",
    502: "Bad Gateway",
  };
  
  // RFC 9457. `about:blank`: the status says what kind of problem it is; `detail` says which.
  function reply(status, detail) {
    const headers = { "content-type": "application/problem+json" };
    if (status === 405) headers.allow = "PUT, DELETE"; // RFC 9110 §15.5.6
    const body = { type: "about:blank", title: TITLES[status], status, detail };
    return new Response(JSON.stringify(body), { status, headers });
  }
