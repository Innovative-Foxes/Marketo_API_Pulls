/**
 * Tests for utm-first-touch.js
 *
 * Runs the script against a minimal document/window shim so cookie and URL
 * behaviour can be driven directly. No dependencies: `node utm-first-touch.test.js`
 */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SRC = fs.readFileSync(path.join(__dirname, "utm-first-touch.js"), "utf8");

/** Minimal cookie jar honouring expiry, which is how deletion is expressed. */
function makeSandbox(url) {
  const jar = new Map();
  const parsed = new URL(url);

  const document = {
    readyState: "complete",
    addEventListener() {},
    getElementsByTagName: () => [],
    get cookie() {
      return [...jar.entries()].map(([k, v]) => `${k}=${v}`).join("; ");
    },
    set cookie(str) {
      const [pair, ...attrs] = str.split(";");
      const eq = pair.indexOf("=");
      const name = pair.slice(0, eq).trim();
      const value = pair.slice(eq + 1);
      const expires = attrs
        .map((a) => a.trim())
        .find((a) => a.toLowerCase().startsWith("expires="));
      if (expires && new Date(expires.slice(8)).getTime() <= Date.now()) {
        jar.delete(name);
      } else {
        jar.set(name, value);
      }
    },
  };

  const window = {
    location: { hostname: parsed.hostname, search: parsed.search },
    document,
  };
  window.window = window;

  const sandbox = { window, document, MktoForms2: undefined };
  sandbox.globalThis = sandbox;
  return { sandbox, jar };
}

function run(url, jar) {
  const { sandbox, jar: fresh } = makeSandbox(url);
  if (jar) for (const [k, v] of jar) fresh.set(k, v);
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox);
  return fresh;
}

function read(jar, name) {
  const v = jar.get(name);
  return v === undefined ? null : decodeURIComponent(v);
}

let passed = 0;
let failed = 0;
function check(label, actual, expected) {
  const ok = actual === expected;
  if (ok) {
    passed++;
    console.log(`  PASS  ${label}`);
  } else {
    failed++;
    console.log(`  FAIL  ${label}\n          expected ${JSON.stringify(expected)}\n          actual   ${JSON.stringify(actual)}`);
  }
}

const BASE = "https://www.example.com";
const FULL =
  "?utm_source=iftest&utm_medium=iftest_med&utm_campaign=iftest_camp&utm_term=iftest_term&utm_content=iftest_content&utm_campaignID=IF123";

console.log("\n1. First tagged visit captures both sets");
{
  const jar = run(BASE + "/" + FULL);
  check("ftUtmSource", read(jar, "ftUtmSource"), "iftest");
  check("ftUtmCampaignId", read(jar, "ftUtmCampaignId"), "IF123");
  check("utmSource", read(jar, "utmSource"), "iftest");
  check("utmKeyword", read(jar, "utmKeyword"), "iftest_term");
}

console.log("\n2. Second tagged visit holds first touch, replaces last touch");
{
  let jar = run(BASE + "/" + FULL);
  jar = run(
    BASE + "/products?utm_source=SECOND&utm_medium=SECOND_med&utm_campaign=SECOND_camp",
    jar
  );
  check("ftUtmSource unchanged", read(jar, "ftUtmSource"), "iftest");
  check("utmSource updated", read(jar, "utmSource"), "SECOND");
  check(
    "utmKeyword cleared, not carried over (the merge bug)",
    read(jar, "utmKeyword"),
    null
  );
  check("ftUtmKeyword still first-touch", read(jar, "ftUtmKeyword"), "iftest_term");
}

console.log("\n3. Untagged pageview leaves everything alone");
{
  let jar = run(BASE + "/" + FULL);
  jar = run(BASE + "/about", jar);
  check("ftUtmSource", read(jar, "ftUtmSource"), "iftest");
  check("utmSource", read(jar, "utmSource"), "iftest");
}

console.log("\n4. Partial parameters never write the string 'null'");
{
  const jar = run(BASE + "/?utm_campaignID=CMP789");
  check("utmCampaignId", read(jar, "utmCampaignId"), "CMP789");
  check("utmSource not 'null'", read(jar, "utmSource"), null);
  check("ftUtmSource not 'null'", read(jar, "ftUtmSource"), null);
  check("ftUtmCampaignId", read(jar, "ftUtmCampaignId"), "CMP789");
}

console.log("\n5. Poisoned 'null' first touch self-heals on next tagged visit");
{
  const poisoned = new Map([
    ["ftUtmSource", "null"],
    ["ftUtmMedium", "null"],
    ["ftUtmCampaign", "null"],
    ["ftUtmKeyword", "null"],
    ["ftUtmContent", "null"],
  ]);
  const jar = run(BASE + "/" + FULL, poisoned);
  check("ftUtmSource repaired", read(jar, "ftUtmSource"), "iftest");
  check("ftUtmMedium repaired", read(jar, "ftUtmMedium"), "iftest_med");
}

console.log("\n6. A real first touch is never overwritten");
{
  const existing = new Map([
    ["ftUtmSource", "original"],
    ["ftUtmMedium", "original_med"],
  ]);
  const jar = run(BASE + "/" + FULL, existing);
  check("ftUtmSource held", read(jar, "ftUtmSource"), "original");
  check("ftUtmMedium held", read(jar, "ftUtmMedium"), "original_med");
}

console.log("\n7. Query-string parameter names are case-insensitive");
{
  const jar = run(BASE + "/?utm_CampaignID=MIXED&UTM_SOURCE=upper");
  check("utmCampaignId", read(jar, "utmCampaignId"), "MIXED");
  check("utmSource", read(jar, "utmSource"), "upper");
}

console.log("\n8. Cookie domain scoping");
{
  const { sandbox, jar } = makeSandbox("https://go.example.com/?utm_source=x");
  sandbox.window.utmFirstTouchOptions = { cookieDomains: ["example.com"] };
  let written = "";
  Object.defineProperty(sandbox.document, "cookie", {
    get: () => "",
    set: (v) => {
      written += v + "\n";
    },
    configurable: true,
  });
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox);
  check("scopes to the configured parent domain", /domain=example\.com/.test(written), true);
  void jar;
}

console.log("\n8b. Cookie domain defaults to the current hostname");
{
  const { sandbox } = makeSandbox("https://go.example.com/?utm_source=x");
  let written = "";
  Object.defineProperty(sandbox.document, "cookie", {
    get: () => "",
    set: (v) => {
      written += v + "\n";
    },
    configurable: true,
  });
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox);
  check("host-only without options", /domain=go\.example\.com/.test(written), true);
}

console.log("\n9. Encoded values survive a round trip");
{
  const jar = run(BASE + "/?utm_campaign=spring%20sale%20%2B%20promo");
  check("decoded once", read(jar, "utmCampaign"), "spring sale + promo");
}

console.log("\n10. Fragment on the final parameter is stripped");
{
  const jar = run(BASE + "/?utm_source=news&utm_content=banner#section");
  check("utmContent", read(jar, "utmContent"), "banner");
}

console.log("\n11. Form fields are filled, matching name case-insensitively");
{
  const inputs = [
    { name: "utm_source__c", value: "" },
    { name: "ft_utm_source", value: "" },
    { name: "fT_utm_medium", value: "" }, // capital-T spelling used by some builds
    { name: "FT_UTM_CAMPAIGN", value: "" },
    { name: "unrelated", value: "keep" },
  ].map((i) => ({
    ...i,
    getAttribute(a) {
      return a === "name" ? this.name : null;
    },
  }));

  const { sandbox, jar } = makeSandbox(BASE + "/" + FULL);
  sandbox.document.getElementsByTagName = (tag) =>
    tag === "input" ? inputs : [];
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox);

  check("exact match filled", inputs[0].value, "iftest");
  check("ft_ lowercase filled", inputs[1].value, "iftest");
  check("fT_ capital-T filled", inputs[2].value, "iftest_med");
  check("uppercase filled", inputs[3].value, "iftest_camp");
  check("unrelated field untouched", inputs[4].value, "keep");
  void jar;
}

console.log("\n12. Blank and 'null' cookies are never pushed into a form");
{
  const inputs = [
    { name: "ft_utm_source", value: "preexisting" },
    { name: "utm_source__c", value: "preexisting" },
  ].map((i) => ({
    ...i,
    getAttribute(a) {
      return a === "name" ? this.name : null;
    },
  }));

  const { sandbox } = makeSandbox(BASE + "/about");
  sandbox.document.getElementsByTagName = (tag) =>
    tag === "input" ? inputs : [];
  // Cookie jar holds a poisoned value and nothing else.
  sandbox.document.cookie = "ftUtmSource=null";
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox);

  check("'null' not written to field", inputs[0].value, "preexisting");
  check("missing cookie not written", inputs[1].value, "preexisting");
}

console.log("\n13. Multiple forms on one page all get filled");
{
  const inputs = [
    { name: "ft_utm_source", value: "" },
    { name: "ft_utm_source", value: "" },
  ].map((i) => ({
    ...i,
    getAttribute(a) {
      return a === "name" ? this.name : null;
    },
  }));

  const { sandbox } = makeSandbox(BASE + "/" + FULL);
  sandbox.document.getElementsByTagName = (tag) =>
    tag === "input" ? inputs : [];
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox);

  check("first form filled", inputs[0].value, "iftest");
  check("second form filled", inputs[1].value, "iftest");
}

console.log(`\n${passed} passed, ${failed} failed\n`);
process.exit(failed ? 1 : 0);
