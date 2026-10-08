# Giffer
Paste a public video link, preview it, choose a 0.5–30 second clip and download a GIF.

## Render
Create a Web Service from this repository. Runtime: Docker. Instance: Free.
Health check: /health. No database or disk is required. render.yaml also supports a Blueprint deployment.

One background worker queues up to three operations. Browser sessions isolate previews and GIFs. Temporary media expires after 30 minutes when the worker is idle, or on restart. Source files are limited to 100 MB. The service does not access visitors' browser cookies or private accounts.

Some video sites block cloud-hosted downloaders even when local downloads work. Test the relevant links after deployment. Render Free sleeps when idle and has resource and traffic limits.
