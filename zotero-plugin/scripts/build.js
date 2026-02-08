#!/usr/bin/env node
/**
 * Build script for Zotero MCP Bridge plugin
 *
 * Creates an .xpi file (ZIP archive) that can be installed in Zotero 7
 */

const fs = require("fs");
const path = require("path");

// Check for adm-zip, provide helpful message if not installed
let AdmZip;
try {
  AdmZip = require("adm-zip");
} catch (e) {
  console.error("Error: adm-zip not installed. Run 'npm install' first.");
  process.exit(1);
}

const rootDir = path.resolve(__dirname, "..");
const addonDir = path.join(rootDir, "addon");
const distDir = path.join(rootDir, "dist");

// Read manifest for version info
const manifest = JSON.parse(fs.readFileSync(path.join(addonDir, "manifest.json"), "utf-8"));
const version = manifest.version;
const pluginId = manifest.applications.zotero.id;

console.log(`Building Zotero MCP Bridge v${version}...`);

// Create dist directory
if (!fs.existsSync(distDir)) {
  fs.mkdirSync(distDir, { recursive: true });
}

// Create XPI (ZIP) file
const zip = new AdmZip();

// Add all files from addon directory
function addDirectoryToZip(dirPath, zipPath = "") {
  const entries = fs.readdirSync(dirPath, { withFileTypes: true });

  for (const entry of entries) {
    const fullPath = path.join(dirPath, entry.name);
    const entryZipPath = zipPath ? `${zipPath}/${entry.name}` : entry.name;

    if (entry.isDirectory()) {
      addDirectoryToZip(fullPath, entryZipPath);
    } else {
      zip.addLocalFile(fullPath, zipPath || undefined);
    }
  }
}

addDirectoryToZip(addonDir);

// Write XPI file
const xpiName = `zotero-mcp-bridge-${version}.xpi`;
const xpiPath = path.join(distDir, xpiName);
zip.writeZip(xpiPath);

console.log(`Created: dist/${xpiName}`);

// Also create a latest symlink/copy for easy installation
const latestPath = path.join(distDir, "zotero-mcp-bridge-latest.xpi");
fs.copyFileSync(xpiPath, latestPath);
console.log(`Created: dist/zotero-mcp-bridge-latest.xpi`);

// Generate update.json for auto-updates
const updateJson = {
  addons: {
    [pluginId]: {
      updates: [
        {
          version: version,
          update_link: `https://github.com/lucidbard/zotero-mcp/releases/download/zotero-plugin-v${version}/${xpiName}`,
          applications: {
            zotero: {
              strict_min_version: "7.0",
            },
          },
        },
      ],
    },
  },
};

fs.writeFileSync(path.join(distDir, "update.json"), JSON.stringify(updateJson, null, 2));
console.log("Created: dist/update.json");

console.log("\nBuild complete!");
console.log("\nTo install:");
console.log("1. Open Zotero 7");
console.log("2. Go to Tools > Add-ons");
console.log("3. Click the gear icon > Install Add-on From File");
console.log(`4. Select: ${xpiPath}`);
