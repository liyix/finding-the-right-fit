#!/usr/bin/env node

// Transparent OpenRouter JSON-body injector/auditor for Chat Completions and
// Responses. In the default `inject` mode it changes only the configured model
// name (when incoming_model is set) and top-level `provider` field. In `audit`
// mode the request must already contain the exact configured model/provider and
// is forwarded byte-for-byte. Neither mode adds retries or timeouts, and
// neither records credentials, prompts, responses, or tool arguments.

import crypto from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import https from "node:https";

function argument(name) {
  const index = process.argv.indexOf(name);
  if (index < 0 || index + 1 >= process.argv.length) {
    throw new Error(`missing ${name}`);
  }
  return process.argv[index + 1];
}

function optionalArgument(name) {
  const index = process.argv.indexOf(name);
  if (index < 0) return null;
  if (index + 1 >= process.argv.length) {
    throw new Error(`missing value for ${name}`);
  }
  return process.argv[index + 1];
}

const configPath = argument("--config");
const upstreamBase = argument("--upstream").replace(/\/$/, "");
const logPath = argument("--log");
const port = Number(argument("--port"));
const listenHost = optionalArgument("--listen-host") ?? "127.0.0.1";
const config = JSON.parse(fs.readFileSync(configPath, "utf8"));
const mode = optionalArgument("--mode") ?? config.mode ?? "inject";

if (!config.model || !config.provider || !Array.isArray(config.provider.only)) {
  throw new Error("config requires model and provider.only");
}
if (config.incoming_model !== undefined && typeof config.incoming_model !== "string") {
  throw new Error("incoming_model must be a string when provided");
}
if (config.provider.allow_fallbacks !== false) {
  throw new Error("allow_fallbacks must be false");
}
if (!["inject", "audit"].includes(mode)) {
  throw new Error("mode must be inject or audit");
}

let sequence = 0;

function sha256(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}

function canonicalJson(value) {
  if (Array.isArray(value)) {
    return `[${value.map((item) => canonicalJson(item)).join(",")}]`;
  }
  if (value && typeof value === "object") {
    return `{${Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

function sameJson(left, right) {
  return canonicalJson(left) === canonicalJson(right);
}

function requestAudit(original) {
  const provider = original.provider;
  const reasoningEffort =
    original.reasoning_effort ??
    (original.reasoning && typeof original.reasoning === "object"
      ? original.reasoning.effort
      : null) ??
    null;
  const tools = Array.isArray(original.tools) ? original.tools : [];
  const inputItems = Array.isArray(original.input) ? original.input : [];
  const additionalTools = inputItems.filter(
    (item) => item && typeof item === "object" && item.type === "additional_tools",
  );
  return {
    provider_present: Object.prototype.hasOwnProperty.call(original, "provider"),
    provider_sha256:
      provider && typeof provider === "object"
        ? sha256(Buffer.from(canonicalJson(provider)))
        : null,
    provider_only: Array.isArray(provider?.only) ? provider.only : null,
    provider_quantizations: Array.isArray(provider?.quantizations)
      ? provider.quantizations
      : null,
    provider_allow_fallbacks: provider?.allow_fallbacks ?? null,
    provider_require_parameters: provider?.require_parameters ?? null,
    usage_include: original.usage?.include ?? null,
    reasoning_effort: reasoningEffort,
    max_tokens_present: Object.prototype.hasOwnProperty.call(original, "max_tokens"),
    max_tokens: original.max_tokens ?? null,
    max_output_tokens_present: Object.prototype.hasOwnProperty.call(
      original,
      "max_output_tokens",
    ),
    max_output_tokens: original.max_output_tokens ?? null,
    parallel_tool_calls_present: Object.prototype.hasOwnProperty.call(
      original,
      "parallel_tool_calls",
    ),
    parallel_tool_calls: original.parallel_tool_calls ?? null,
    stream: original.stream ?? null,
    tools_count: tools.length,
    tools_sha256: sha256(Buffer.from(canonicalJson(tools))),
    input_item_types: inputItems
      .filter((item) => item && typeof item === "object")
      .map((item) => item.type ?? null),
    additional_tools_count: additionalTools.length,
    additional_tools_sha256: sha256(Buffer.from(canonicalJson(additionalTools))),
    user_sha256:
      typeof original.user === "string"
        ? sha256(Buffer.from(original.user))
        : null,
  };
}

function appendLog(record) {
  fs.appendFileSync(logPath, `${JSON.stringify(record)}\n`, { mode: 0o600 });
  fs.chmodSync(logPath, 0o600);
}

function filteredRequestHeaders(headers) {
  const output = {};
  for (const [name, value] of Object.entries(headers)) {
    const lower = name.toLowerCase();
    if (["host", "content-length", "connection", "transfer-encoding"].includes(lower)) {
      continue;
    }
    if (Array.isArray(value)) {
      output[name] = value;
    } else if (value !== undefined) {
      output[name] = value;
    }
  }
  return output;
}

function filteredResponseHeaders(headers) {
  const output = {};
  for (const [name, value] of Object.entries(headers)) {
    if (["content-length", "connection", "transfer-encoding", "content-encoding"].includes(name.toLowerCase())) {
      continue;
    }
    if (value !== undefined) output[name] = value;
  }
  return output;
}

async function readBody(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(Buffer.from(chunk));
  return Buffer.concat(chunks);
}

function forward(request, response, body, onHeaders) {
  return new Promise((resolve, reject) => {
    const url = new URL(`${upstreamBase}${request.url}`);
    const transport = url.protocol === "https:" ? https : http;
    const headers = filteredRequestHeaders(request.headers);
    if (!["GET", "HEAD"].includes(request.method ?? "")) {
      headers["content-length"] = String(body.length);
    }
    const upstream = transport.request(
      url,
      {
        method: request.method,
        headers,
      },
      (upstreamResponse) => {
        const generationHeader = upstreamResponse.headers["x-generation-id"];
        const requestHeader = upstreamResponse.headers["x-request-id"];
        onHeaders({
          status: upstreamResponse.statusCode ?? 502,
          generationId: Array.isArray(generationHeader)
            ? generationHeader[0]
            : (generationHeader ?? null),
          requestId: Array.isArray(requestHeader)
            ? requestHeader[0]
            : (requestHeader ?? null),
        });
        response.writeHead(
          upstreamResponse.statusCode ?? 502,
          upstreamResponse.statusMessage,
          filteredResponseHeaders(upstreamResponse.headers),
        );
        upstreamResponse.on("data", (chunk) => response.write(chunk));
        upstreamResponse.on("end", () => {
          response.end();
          resolve(upstreamResponse.statusCode ?? 502);
        });
        upstreamResponse.on("error", reject);
      },
    );
    upstream.on("error", reject);
    // Node's native request has no request/socket timeout unless one is set.
    if (!["GET", "HEAD"].includes(request.method ?? "")) upstream.write(body);
    upstream.end();
  });
}

const server = http.createServer(async (request, response) => {
  if (request.method === "GET" && request.url === "/health") {
    response.writeHead(204).end();
    return;
  }

  const id = ++sequence;
  const started = Date.now();
  let incoming = Buffer.alloc(0);
  let forwarded = Buffer.alloc(0);
  let upstreamStatus = null;
  let upstreamGenerationId = null;
  let upstreamRequestId = null;
  let upstreamResponseCompleted = false;
  let error = null;
  let model = null;
  let providerValidation = null;
  let auditedRequest = null;
  let changedFields = [];

  try {
    incoming = await readBody(request);
    forwarded = incoming;
    const requestPath = request.url?.split("?", 1)[0];
    if (
      request.method === "POST" &&
      ["/v1/chat/completions", "/v1/responses"].includes(requestPath)
    ) {
      const original = JSON.parse(incoming.toString("utf8"));
      if (!original || typeof original !== "object" || Array.isArray(original)) {
        throw new Error("request body is not an object");
      }
      model = original.model;
      const expectedIncomingModel = config.incoming_model ?? config.model;
      if (model !== expectedIncomingModel) {
        throw new Error(`model mismatch: expected ${expectedIncomingModel}, got ${model}`);
      }
      const hasProvider = Object.prototype.hasOwnProperty.call(original, "provider");
      if (mode === "audit") {
        if (!hasProvider) {
          providerValidation = "missing";
          throw new Error("audit mode requires an existing provider object");
        }
        if (!sameJson(original.provider, config.provider)) {
          providerValidation = "mismatch";
          throw new Error("incoming provider does not match the frozen provider object");
        }
        providerValidation = "matched-existing";
      } else {
        if (hasProvider) {
          providerValidation = "unexpected-existing";
          throw new Error("incoming body already contains provider");
        }
        const injected = { ...original, model: config.model, provider: config.provider };
        forwarded = Buffer.from(JSON.stringify(injected));
        providerValidation = "injected";
        changedFields = ["provider"];
        if (original.model !== config.model) changedFields.unshift("model");
      }
      auditedRequest = requestAudit(
        mode === "audit"
          ? original
          : { ...original, model: config.model, provider: config.provider },
      );
    }

    upstreamStatus = await forward(request, response, forwarded, (metadata) => {
      upstreamStatus = metadata.status;
      upstreamGenerationId = metadata.generationId;
      upstreamRequestId = metadata.requestId;
      // Record routing identity as soon as response headers arrive. This line
      // survives a later client/container cancellation even when the streamed
      // response never reaches its normal end event.
      appendLog({
        event: "upstream_headers",
        sequence: id,
        method: request.method,
        path: request.url,
        mode,
        model,
        provider_validation: providerValidation,
        request_audit: auditedRequest,
        changed_fields: changedFields,
        incoming_body_sha256: sha256(incoming),
        forwarded_body_sha256: sha256(forwarded),
        incoming_bytes: incoming.length,
        forwarded_bytes: forwarded.length,
        upstream_status: upstreamStatus,
        openrouter_generation_id: upstreamGenerationId,
        openrouter_request_id: upstreamRequestId,
        injector_retry_count: 0,
        injector_timeout_ms: null,
        error: null,
      });
    });
    upstreamResponseCompleted = true;
  } catch (caught) {
    error = `${caught?.name ?? "Error"}: ${caught?.message ?? String(caught)}`;
    if (!response.headersSent) {
      response.writeHead(502, { "content-type": "application/json" });
      response.end(JSON.stringify({ error: "OpenRouter injector upstream failure" }));
    } else {
      response.destroy();
    }
  } finally {
    appendLog({
      event: "request_end",
      sequence: id,
      method: request.method,
      path: request.url,
      mode,
      model,
      provider_validation: providerValidation,
      request_audit: auditedRequest,
      changed_fields: changedFields,
      incoming_body_sha256: sha256(incoming),
      forwarded_body_sha256: sha256(forwarded),
      incoming_bytes: incoming.length,
      forwarded_bytes: forwarded.length,
      upstream_status: upstreamStatus,
      openrouter_generation_id: upstreamGenerationId,
      openrouter_request_id: upstreamRequestId,
      upstream_response_completed: upstreamResponseCompleted,
      elapsed_seconds: (Date.now() - started) / 1000,
      injector_retry_count: 0,
      injector_timeout_ms: null,
      error,
    });
  }
});

server.listen(port, listenHost);
