/* eslint-disable no-undef */
/**
 * Zotero MCP Bridge Plugin - Main Module
 *
 * Provides HTTP endpoints on Zotero's built-in server (port 23119) for
 * the Zotero MCP server to call for write operations.
 *
 * Endpoints:
 * - POST /zotero-mcp/rpc - JSON-RPC style endpoint for all commands
 *
 * Commands:
 * - createCollection(name, parentKey?) - Create a new collection
 * - getCollection(key) - Get collection info
 * - addToCollection(collectionKey, itemKeys) - Add items to collection
 * - removeFromCollection(collectionKey, itemKeys) - Remove items from collection
 * - searchItems(query, limit?) - Search for items
 * - getItem(key) - Get item details
 */

class ZoteroMcpPlugin {
  constructor() {
    this.endpoints = [];
  }

  async startup() {
    this.registerEndpoints();
    Zotero.debug("Zotero MCP: Endpoints registered on port 23119");
  }

  shutdown() {
    this.unregisterEndpoints();
    Zotero.debug("Zotero MCP: Endpoints unregistered");
  }

  registerEndpoints() {
    // Main RPC endpoint
    this.registerEndpoint("/zotero-mcp/rpc", {
      supportedMethods: ["POST", "OPTIONS"],
      supportedDataTypes: ["application/json"],
      permitBookmarklet: false,

      init: async (requestData) => {
        // Handle CORS preflight
        if (requestData.method === "OPTIONS") {
          return this.corsResponse(204, "");
        }

        try {
          const body = this.parseRequestBody(requestData);
          const result = await this.handleRpcRequest(body);
          return this.jsonResponse(200, result);
        } catch (error) {
          Zotero.debug(`Zotero MCP: RPC error - ${error.message}`);
          return this.jsonResponse(error.status || 500, {
            error: error.code || "INTERNAL_ERROR",
            message: error.message,
          });
        }
      },
    });

    // Health check endpoint
    this.registerEndpoint("/zotero-mcp/health", {
      supportedMethods: ["GET", "OPTIONS"],
      permitBookmarklet: false,

      init: async (requestData) => {
        if (requestData.method === "OPTIONS") {
          return this.corsResponse(204, "");
        }
        return this.jsonResponse(200, {
          status: "ok",
          version: "1.0.0",
          zoteroVersion: Zotero.version,
        });
      },
    });
  }

  registerEndpoint(path, handler) {
    const endpoint = function () {};
    endpoint.prototype = handler;
    Zotero.Server.Endpoints[path] = endpoint;
    this.endpoints.push(path);
  }

  unregisterEndpoints() {
    for (const path of this.endpoints) {
      delete Zotero.Server.Endpoints[path];
    }
    this.endpoints = [];
  }

  parseRequestBody(requestData) {
    if (!requestData.data) {
      throw this.rpcError("INVALID_REQUEST", "Request body is required", 400);
    }

    try {
      return JSON.parse(requestData.data);
    } catch (e) {
      throw this.rpcError("PARSE_ERROR", "Invalid JSON in request body", 400);
    }
  }

  async handleRpcRequest(body) {
    const { method, params = {} } = body;

    if (!method) {
      throw this.rpcError("INVALID_REQUEST", "Method is required", 400);
    }

    Zotero.debug(`Zotero MCP: RPC call - ${method}`);

    switch (method) {
      case "createCollection":
        return await this.createCollection(params);
      case "getCollection":
        return await this.getCollection(params);
      case "addToCollection":
        return await this.addToCollection(params);
      case "removeFromCollection":
        return await this.removeFromCollection(params);
      case "searchItems":
        return await this.searchItems(params);
      case "getItem":
        return await this.getItem(params);
      case "listCollections":
        return await this.listCollections(params);
      default:
        throw this.rpcError("METHOD_NOT_FOUND", `Unknown method: ${method}`, 404);
    }
  }

  // ============================================================
  // Collection Management Commands
  // ============================================================

  /**
   * Create a new collection
   * @param {Object} params
   * @param {string} params.name - Collection name
   * @param {string} [params.parentKey] - Parent collection key (optional)
   * @param {number} [params.libraryID] - Library ID (defaults to user library)
   */
  async createCollection({ name, parentKey, libraryID }) {
    if (!name || typeof name !== "string") {
      throw this.rpcError("INVALID_PARAMS", "Collection name is required", 400);
    }

    const libID = libraryID || Zotero.Libraries.userLibraryID;

    const collection = new Zotero.Collection();
    collection.libraryID = libID;
    collection.name = name;

    if (parentKey) {
      const parent = await Zotero.Collections.getByLibraryAndKeyAsync(libID, parentKey);
      if (!parent) {
        throw this.rpcError("NOT_FOUND", `Parent collection not found: ${parentKey}`, 404);
      }
      collection.parentID = parent.id;
    }

    await collection.saveTx();

    Zotero.debug(`Zotero MCP: Created collection "${name}" with key ${collection.key}`);

    return {
      key: collection.key,
      name: collection.name,
      libraryID: collection.libraryID,
      parentKey: parentKey || null,
      version: collection.version,
    };
  }

  /**
   * Get collection info
   * @param {Object} params
   * @param {string} params.key - Collection key
   * @param {number} [params.libraryID] - Library ID (defaults to user library)
   */
  async getCollection({ key, libraryID }) {
    if (!key) {
      throw this.rpcError("INVALID_PARAMS", "Collection key is required", 400);
    }

    const libID = libraryID || Zotero.Libraries.userLibraryID;
    const collection = await Zotero.Collections.getByLibraryAndKeyAsync(libID, key);

    if (!collection) {
      throw this.rpcError("NOT_FOUND", `Collection not found: ${key}`, 404);
    }

    const itemIDs = collection.getChildItems(true);
    const items = await Zotero.Items.getAsync(itemIDs);

    return {
      key: collection.key,
      name: collection.name,
      libraryID: collection.libraryID,
      parentKey: collection.parentKey || null,
      version: collection.version,
      itemCount: itemIDs.length,
      items: items.map((item) => this.serializeItemBrief(item)),
    };
  }

  /**
   * Add items to a collection
   * @param {Object} params
   * @param {string} params.collectionKey - Collection key
   * @param {string[]} params.itemKeys - Array of item keys to add
   * @param {number} [params.libraryID] - Library ID (defaults to user library)
   */
  async addToCollection({ collectionKey, itemKeys, libraryID }) {
    if (!collectionKey) {
      throw this.rpcError("INVALID_PARAMS", "Collection key is required", 400);
    }
    if (!Array.isArray(itemKeys) || itemKeys.length === 0) {
      throw this.rpcError("INVALID_PARAMS", "Item keys array is required", 400);
    }

    const libID = libraryID || Zotero.Libraries.userLibraryID;
    const collection = await Zotero.Collections.getByLibraryAndKeyAsync(libID, collectionKey);

    if (!collection) {
      throw this.rpcError("NOT_FOUND", `Collection not found: ${collectionKey}`, 404);
    }

    const added = [];
    const errors = [];

    for (const itemKey of itemKeys) {
      try {
        const item = await Zotero.Items.getByLibraryAndKeyAsync(libID, itemKey);
        if (!item) {
          errors.push({ key: itemKey, error: "Item not found" });
          continue;
        }

        if (item.isNote() || item.isAttachment()) {
          // Skip notes and attachments - they follow their parent
          errors.push({ key: itemKey, error: "Cannot add notes or attachments directly" });
          continue;
        }

        await collection.addItem(item.id);
        added.push(itemKey);
      } catch (e) {
        errors.push({ key: itemKey, error: e.message });
      }
    }

    Zotero.debug(`Zotero MCP: Added ${added.length} items to collection ${collectionKey}`);

    return {
      collectionKey,
      added,
      errors: errors.length > 0 ? errors : undefined,
    };
  }

  /**
   * Remove items from a collection
   * @param {Object} params
   * @param {string} params.collectionKey - Collection key
   * @param {string[]} params.itemKeys - Array of item keys to remove
   * @param {number} [params.libraryID] - Library ID (defaults to user library)
   */
  async removeFromCollection({ collectionKey, itemKeys, libraryID }) {
    if (!collectionKey) {
      throw this.rpcError("INVALID_PARAMS", "Collection key is required", 400);
    }
    if (!Array.isArray(itemKeys) || itemKeys.length === 0) {
      throw this.rpcError("INVALID_PARAMS", "Item keys array is required", 400);
    }

    const libID = libraryID || Zotero.Libraries.userLibraryID;
    const collection = await Zotero.Collections.getByLibraryAndKeyAsync(libID, collectionKey);

    if (!collection) {
      throw this.rpcError("NOT_FOUND", `Collection not found: ${collectionKey}`, 404);
    }

    const removed = [];
    const errors = [];

    for (const itemKey of itemKeys) {
      try {
        const item = await Zotero.Items.getByLibraryAndKeyAsync(libID, itemKey);
        if (!item) {
          errors.push({ key: itemKey, error: "Item not found" });
          continue;
        }

        await collection.removeItem(item.id);
        removed.push(itemKey);
      } catch (e) {
        errors.push({ key: itemKey, error: e.message });
      }
    }

    Zotero.debug(`Zotero MCP: Removed ${removed.length} items from collection ${collectionKey}`);

    return {
      collectionKey,
      removed,
      errors: errors.length > 0 ? errors : undefined,
    };
  }

  /**
   * List all collections
   * @param {Object} params
   * @param {number} [params.libraryID] - Library ID (defaults to user library)
   * @param {string} [params.parentKey] - Filter by parent collection key
   */
  async listCollections({ libraryID, parentKey }) {
    const libID = libraryID || Zotero.Libraries.userLibraryID;
    let collections = Zotero.Collections.getByLibrary(libID, true);

    if (parentKey) {
      const parent = await Zotero.Collections.getByLibraryAndKeyAsync(libID, parentKey);
      if (!parent) {
        throw this.rpcError("NOT_FOUND", `Parent collection not found: ${parentKey}`, 404);
      }
      collections = collections.filter((c) => c.parentID === parent.id);
    }

    return {
      collections: collections.map((c) => ({
        key: c.key,
        name: c.name,
        libraryID: c.libraryID,
        parentKey: c.parentKey || null,
        itemCount: c.getChildItems(true).length,
      })),
    };
  }

  // ============================================================
  // Item Commands
  // ============================================================

  /**
   * Search for items
   * @param {Object} params
   * @param {string} params.query - Search query
   * @param {number} [params.limit=50] - Maximum results
   * @param {number} [params.libraryID] - Library ID (defaults to user library)
   */
  async searchItems({ query, limit = 50, libraryID }) {
    if (!query || typeof query !== "string") {
      throw this.rpcError("INVALID_PARAMS", "Search query is required", 400);
    }

    const libID = libraryID || Zotero.Libraries.userLibraryID;

    const search = new Zotero.Search();
    search.libraryID = libID;
    search.addCondition("quicksearch-titleCreatorYear", "contains", query);

    const itemIDs = await search.search();
    const limitedIDs = itemIDs.slice(0, limit);
    const items = await Zotero.Items.getAsync(limitedIDs);

    // Filter out notes and attachments
    const regularItems = items.filter((item) => item.isRegularItem());

    return {
      query,
      total: regularItems.length,
      items: regularItems.map((item) => this.serializeItem(item)),
    };
  }

  /**
   * Get item details
   * @param {Object} params
   * @param {string} params.key - Item key
   * @param {number} [params.libraryID] - Library ID (defaults to user library)
   */
  async getItem({ key, libraryID }) {
    if (!key) {
      throw this.rpcError("INVALID_PARAMS", "Item key is required", 400);
    }

    const libID = libraryID || Zotero.Libraries.userLibraryID;
    const item = await Zotero.Items.getByLibraryAndKeyAsync(libID, key);

    if (!item) {
      throw this.rpcError("NOT_FOUND", `Item not found: ${key}`, 404);
    }

    return this.serializeItem(item);
  }

  // ============================================================
  // Serialization Helpers
  // ============================================================

  serializeItem(item) {
    const creators = item.getCreators().map((c) => ({
      firstName: c.firstName,
      lastName: c.lastName,
      creatorType: Zotero.CreatorTypes.getName(c.creatorTypeID),
    }));

    const collections = item.getCollections().map((colID) => {
      const col = Zotero.Collections.get(colID);
      return col ? col.key : null;
    }).filter(Boolean);

    return {
      key: item.key,
      libraryID: item.libraryID,
      itemType: Zotero.ItemTypes.getName(item.itemTypeID),
      title: item.getField("title"),
      creators,
      date: item.getField("date"),
      year: item.getField("year"),
      abstractNote: item.getField("abstractNote"),
      publicationTitle: item.getField("publicationTitle"),
      publisher: item.getField("publisher"),
      DOI: item.getField("DOI"),
      URL: item.getField("url"),
      collections,
      tags: item.getTags().map((t) => t.tag),
      dateAdded: item.dateAdded,
      dateModified: item.dateModified,
    };
  }

  serializeItemBrief(item) {
    const creators = item.getCreators();
    const firstAuthor = creators.length > 0 ? creators[0].lastName : "";

    return {
      key: item.key,
      title: item.getField("title"),
      firstAuthor,
      year: item.getField("year"),
      itemType: Zotero.ItemTypes.getName(item.itemTypeID),
    };
  }

  // ============================================================
  // Response Helpers
  // ============================================================

  jsonResponse(status, data) {
    return [
      status,
      "application/json",
      JSON.stringify(data),
      {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
      },
    ];
  }

  corsResponse(status, body) {
    return [
      status,
      "text/plain",
      body,
      {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
      },
    ];
  }

  rpcError(code, message, status = 500) {
    const error = new Error(message);
    error.code = code;
    error.status = status;
    return error;
  }
}

// Export for bootstrap.js
if (typeof window !== "undefined") {
  window.ZoteroMcpPlugin = ZoteroMcpPlugin;
}
