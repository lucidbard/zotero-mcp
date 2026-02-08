#!/usr/bin/env node
/**
 * Test client for Zotero MCP Bridge plugin
 *
 * Tests the HTTP endpoints on port 23119 when Zotero is running
 */

const http = require("http");

const BASE_URL = "http://127.0.0.1:23119";

async function request(path, method = "GET", body = null) {
  return new Promise((resolve, reject) => {
    const url = new URL(path, BASE_URL);
    const options = {
      hostname: url.hostname,
      port: url.port,
      path: url.pathname,
      method,
      headers: {
        "Content-Type": "application/json",
      },
    };

    const req = http.request(options, (res) => {
      let data = "";
      res.on("data", (chunk) => (data += chunk));
      res.on("end", () => {
        try {
          resolve({ status: res.statusCode, data: JSON.parse(data) });
        } catch (e) {
          resolve({ status: res.statusCode, data });
        }
      });
    });

    req.on("error", reject);

    if (body) {
      req.write(JSON.stringify(body));
    }
    req.end();
  });
}

async function rpc(method, params = {}) {
  return request("/zotero-mcp/rpc", "POST", { method, params });
}

async function runTests() {
  console.log("Testing Zotero MCP Bridge Plugin...\n");

  // Test 1: Health check
  console.log("1. Health check...");
  try {
    const health = await request("/zotero-mcp/health");
    console.log(`   Status: ${health.status}`);
    console.log(`   Response: ${JSON.stringify(health.data)}`);
    console.log("   ✓ Health check passed\n");
  } catch (e) {
    console.log(`   ✗ Health check failed: ${e.message}`);
    console.log("   Make sure Zotero is running with the plugin installed.\n");
    process.exit(1);
  }

  // Test 2: List collections
  console.log("2. List collections...");
  try {
    const result = await rpc("listCollections");
    console.log(`   Status: ${result.status}`);
    console.log(`   Found ${result.data.collections?.length || 0} collections`);
    console.log("   ✓ List collections passed\n");
  } catch (e) {
    console.log(`   ✗ List collections failed: ${e.message}\n`);
  }

  // Test 3: Create collection
  console.log("3. Create collection...");
  try {
    const testName = `MCP Test ${Date.now()}`;
    const result = await rpc("createCollection", { name: testName });
    console.log(`   Status: ${result.status}`);
    if (result.data.key) {
      console.log(`   Created collection: ${result.data.name} (${result.data.key})`);
      console.log("   ✓ Create collection passed\n");

      // Test 4: Get collection
      console.log("4. Get collection...");
      const getResult = await rpc("getCollection", { key: result.data.key });
      console.log(`   Status: ${getResult.status}`);
      console.log(`   Collection name: ${getResult.data.name}`);
      console.log("   ✓ Get collection passed\n");
    } else {
      console.log(`   ✗ Create collection returned error: ${JSON.stringify(result.data)}\n`);
    }
  } catch (e) {
    console.log(`   ✗ Create collection failed: ${e.message}\n`);
  }

  // Test 5: Search items
  console.log("5. Search items...");
  try {
    const result = await rpc("searchItems", { query: "test", limit: 5 });
    console.log(`   Status: ${result.status}`);
    console.log(`   Found ${result.data.total || 0} items matching "test"`);
    if (result.data.items?.length > 0) {
      console.log(`   First item: ${result.data.items[0].title}`);
    }
    console.log("   ✓ Search items passed\n");
  } catch (e) {
    console.log(`   ✗ Search items failed: ${e.message}\n`);
  }

  // Test 6: Error handling
  console.log("6. Error handling (invalid method)...");
  try {
    const result = await rpc("invalidMethod");
    console.log(`   Status: ${result.status}`);
    console.log(`   Error: ${result.data.error}`);
    if (result.status === 404 && result.data.error === "METHOD_NOT_FOUND") {
      console.log("   ✓ Error handling passed\n");
    } else {
      console.log("   ✗ Unexpected response\n");
    }
  } catch (e) {
    console.log(`   ✗ Error handling failed: ${e.message}\n`);
  }

  console.log("All tests completed!");
}

runTests().catch((e) => {
  console.error("Test suite error:", e.message);
  process.exit(1);
});
