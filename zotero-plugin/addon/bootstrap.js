/* eslint-disable no-undef */
/**
 * Zotero MCP Bridge - Zotero 7 Plugin Bootstrap
 *
 * This file handles plugin lifecycle (startup/shutdown) and loads the main plugin code.
 */

var ZoteroMcpBridge;

// Called when the plugin is first loaded
function install(data, reason) {}

// Called when the plugin is started
async function startup({ id, version, resourceURI, rootURI = resourceURI.spec }, reason) {
  await Zotero.initializationPromise;

  // Import main plugin module
  Services.scriptloader.loadSubScript(`${rootURI}content/zotero-mcp.js`);

  // Initialize the plugin
  ZoteroMcpBridge = new ZoteroMcpPlugin();
  await ZoteroMcpBridge.startup();

  Zotero.debug("Zotero MCP: Plugin started");
}

// Called when the plugin is shut down
function shutdown(data, reason) {
  if (ZoteroMcpBridge) {
    ZoteroMcpBridge.shutdown();
    ZoteroMcpBridge = undefined;
  }

  Zotero.debug("Zotero MCP: Plugin shut down");
}

// Called when the plugin is uninstalled
function uninstall(data, reason) {}
