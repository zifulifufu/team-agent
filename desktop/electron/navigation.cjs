const path = require("node:path");
const { fileURLToPath } = require("node:url");

// The preload contains the local API token. Never navigate it into arbitrary HTML.
function isAppUrl(raw, { dev, devUrl, indexPath }) {
  try {
    const url = new URL(raw);
    if (url.username || url.password) return false;
    if (dev) return url.origin === new URL(devUrl).origin;
    return url.protocol === "file:" && !url.host &&
      path.resolve(fileURLToPath(url)) === path.resolve(indexPath);
  } catch {
    return false;
  }
}

module.exports = { isAppUrl };
