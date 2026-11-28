# Catalog icons

Every catalog can have one standard icon: a **512 × 512 PNG with transparency**, stored in the registry's own object
storage. Reading apps then load icons from one place, in one format, whatever the library's website does.

The conversion does not happen in Python. Image libraries are heavy native dependencies that need constant updates;
a small Cloudflare Worker converts the image with Cloudflare Images and stores the result in an R2 bucket. It runs
only when an icon is added or replaced. Its code is [`workers/icon-processing/index.js`](../workers/icon-processing/index.js),
deployed by pasting it into the Worker's editor in the Cloudflare dashboard.

## The flow

```
back office ──upload──▶ registry ──────────image─────────▶ Worker ──convert──▶ Cloudflare Images
                           ▲                                   │
                           └──── file name + version tag ──────┴──store PNG──▶ R2 bucket
```

1. An editor uploads an image: `PUT /admin/catalogs/{id}/icon?version=N`, the body being the image itself
   (`Content-Type: image/*`, at most 5 MB).
2. The registry checks the editor's role, that they saw the latest version, and that the catalog is not archived.
3. With the catalog row locked, the registry sends the image to the Worker.
4. The Worker converts it to a 512 px PNG (aspect ratio kept), stores it as `<catalog-id>-icon.png`, and answers with
   the file name and a version tag.
5. The registry replaces the catalog's icon links with one link to the stored PNG, ending in `?v=<version tag>-<time>`,
   new on every store, so an app or a cache that keeps icons by address picks up a replaced one. The catalog's version
   goes up by one.

Removing an icon, or going back to an earlier one, is an ordinary catalog edit (`PATCH` with the links).

### One icon per catalog

Once an icon is stored, a save that keeps it drops any other icon link it carries (the library's original icon,
brought back by a restored draft or the website check): nothing is downloaded and no second icon appears. To use a
different icon, the save leaves the stored one out and sends the new one, which is then stored over the same file.
The website check never reports the stored icon as missing from the library's site; the site's own icon is still
reported, so the back office can offer it as a replacement.

### Removing stored files

When a draft with a stored icon is deleted, or an edit leaves a catalog without its stored icon (removed, or a
replacement that could not be stored), the registry asks the Worker to delete the file (`DELETE /<catalog-id>`). This
happens after the change is committed and never fails it; a file that cannot be deleted is noted in the request log.
An archived catalog keeps its icon. A deleted file can still be served from Cloudflare's cache for a while.

### Icons saved as links

Most icons arrive as a link: the feed check finds the library's icon, or an editor types its address. When a create
(`POST /admin/catalogs`) or an edit of the links (`PATCH`) **adds** an icon link that is not already a stored PNG, the
registry downloads it (with the protections of every outbound fetch, 5 MB, 10 s) and stores it as above, right after
the save. The answer is the catalog after both, so its version can be two higher. An edit that keeps the same icon
downloads nothing.

If the icon cannot be stored (the library's site is down or refuses us, or the file is a format Cloudflare Images
cannot read), **the save still succeeds** and keeps the library's link. The answer then carries `icon_problem`, one
plain sentence for the editor, with no status codes or format names; the technical reason goes to the request log. `icon_stored` on every catalog detail says whether the icon is the stored PNG, so the back
office can offer to try again later: `POST /admin/catalogs/{id}/icon/store?version=N` downloads the current icon link
again and stores it.

## Who does what

| Part | Role |
|---|---|
| Back office | Sends the uploaded file to the registry. Never talks to the Worker or the bucket. |
| Registry | Checks permissions and versions, calls the Worker, updates the catalog. The Worker's only caller. |
| Worker | Converts and stores. Holds no catalog data and makes no decisions. |
| Cloudflare Images | The conversion, called by the Worker. |
| R2 bucket | One flat bucket, one file per catalog, served publicly. |

## Errors

| Answer | Meaning |
|---|---|
| 409 `version-conflict` / `archived-read-only` | As for any edit. Nothing is sent to the Worker. |
| 413 `icon-too-large` | Over 5 MB. The registry stops reading at the limit. |
| 422, `errors[0].field = body` | Not an image type, empty, or an image the Worker cannot read. The message is in plain words for an editor ("We don't support this icon's format (SVG) yet, so it can't be saved on our servers"); the technical reason is in the request log. |
| 503 `unavailable` | Uploads are not configured, or the Worker or Cloudflare failed. Not the editor's fault. |
| 422, `errors[0].field = icon` | From `icon/store` only: no icon link, or it could not be downloaded or converted; the message says which. |

The Worker answers errors as RFC 9457 problem details, like the registry. It keeps the two kinds apart: it first asks Images whether the bytes are an image at all, so a broken file is a
422 and a failed conversion or store is the service's error, never reported as the editor's. The registry also
checks the Worker's answer before turning it into a public link.

## Configuration

Three settings, set together or not at all (the registry refuses to start otherwise). Without them, uploads answer 503 and icon links are saved as links.

| Setting | What |
|---|---|
| `REGISTRY_ICON_WORKER_URL` | The Worker's address. https outside local. |
| `REGISTRY_ICON_WORKER_SECRET` | The Worker's key. From a secret store. |
| `REGISTRY_ICON_BASE_URL` | The public address the bucket's files are served from. https outside local. |

## Limits

- Converted (tested through the Worker): PNG, JPEG, WebP, GIF, HEIC, TIFF and AVIF. An animated GIF or WebP keeps
  its first frame.
- Not converted: SVG, ICO (even one holding a PNG) and BMP. Cloudflare Images does not read them, and the registry does
  not add an image library to do it. Libraries are asked for a PNG. The format is told from the file's bytes, not its
  name or `Content-Type`, so a `favicon.ico` that is really a PNG goes through.
- Icons smaller than 512 px are enlarged, so a tiny favicon looks soft.
- A non-square image stays non-square (e.g. 512 × 256), not padded.
- The file name is the same for every upload to a catalog, so a replaced icon overwrites the old file.
