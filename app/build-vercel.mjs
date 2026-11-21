import { cp, mkdir, readFile, writeFile } from "node:fs/promises";

const site = new URL("./public/", import.meta.url);
const web = new URL("./web/", import.meta.url);
await mkdir(new URL("assets/", site), { recursive: true });
await cp(new URL("brand/", web), new URL("brand/", site), { recursive: true });
await cp(new URL("upfern-workspace.css", web), new URL("assets/upfern-workspace.css", site));

const message = "Website preview. Sign-in, clipping, and exports require the hosted Upfern engine.";
const banner = '<p style="margin:0;background:#ff5b24;color:#17110d;text-align:center;padding:10px 16px;font:600 14px system-ui">' + message + '</p>';
const landing = await readFile(new URL("landing.html", web), "utf8");
const origin = process.env.VERCEL_PROJECT_PRODUCTION_URL || process.env.VERCEL_URL;
const page = landing.replaceAll("{{SITE}}", origin ? "https://" + origin : "").replace("<body>", "<body>" + banner);
await writeFile(new URL("index.html", site), page);
await writeFile(new URL("preview.html", site), `<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Upfern preview</title>
<body style="background:#111110;color:#f1efeb;font:16px system-ui;padding:40px">
<img src="/brand/favicon.svg" width="48" alt="Upfern">
<h1>Upfern website preview</h1>
<p>${message}</p>
<a style="color:#ff8359" href="/">Back to the website</a>
</body>
</html>`);
