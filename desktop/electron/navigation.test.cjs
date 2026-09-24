const { test } = require("node:test");
const assert = require("node:assert/strict");
const { isAppUrl } = require("./navigation.cjs");
const config = { dev: true, devUrl: "http://localhost:5173", indexPath: "/app/dist/index.html" };

test("development navigation compares origins, never prefixes", () => {
  assert.ok(isAppUrl("http://localhost:5173/#group/1", config));
  for (const url of ["http://localhost:5173@evil.example", "http://localhost:51730/",
                     "https://localhost:5173/", "file:///tmp/evil.html", "javascript:alert(1)"]) {
    assert.equal(isAppUrl(url, config), false, url);
  }
});

test("packaged navigation only allows the application entry file", () => {
  const prod = { ...config, dev: false };
  assert.ok(isAppUrl("file:///app/dist/index.html#chat", prod));
  for (const url of ["file:///tmp/evil.html", "file:///app/dist/index.html.evil",
                     "file://other/app/dist/index.html", "http://localhost:5173/"]) {
    assert.equal(isAppUrl(url, prod), false, url);
  }
});
