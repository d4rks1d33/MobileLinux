#!/usr/bin/env node
// Anthropic Messages API in, whatever your opencode providers speak out.
//
// Claude Code only ever talks to one shape of API and lets you move it with
// ANTHROPIC_BASE_URL. This sits there and routes each request to the provider
// named in the model - "google/gemini-2.5-flash", "opencode/glm-5-free",
// "openrouter/qwen/qwen3-coder" - reusing the API keys opencode already holds in
// ~/.local/share/opencode/auth.json. Nothing is copied and no key is ever
// written anywhere by this process.
//
// Node's standard library only, because adding a dependency tree to a phone to
// translate two JSON shapes would be a poor trade.
//
// Three things about this are not obvious:
//
//  1. Most providers are reachable with the OpenAI shape, but the catalog only
//     records an "api" URL for the ones opencode drives with its generic
//     openai-compatible SDK. google, openai, groq, mistral, xai and cerebras all
//     have an OpenAI-compatible path that simply is not in the catalog, so those
//     are in a table below. Verified for google: its
//     /v1beta/openai/chat/completions answers, including tool calls.
//
//  2. Anthropic is not translated at all. If you hold an Anthropic key, the
//     request is already in the right shape, so it is passed straight through -
//     less code and perfect fidelity.
//
//  3. The streaming translation is the actual work. Claude Code wants
//     message_start, content_block_start/delta/stop, message_delta, message_stop,
//     with tool calls arriving as input_json_delta fragments, while OpenAI
//     streams the same information in a different arrangement. Content blocks
//     have to be numbered and opened and closed exactly once, and a tool call
//     has to close any open text block first. Providers differ here too: Zen
//     streams token by token, Gemini's OpenAI endpoint sends one chunk with the
//     whole answer. Both end up as the same event sequence.

import http from "node:http"
import fs from "node:fs"
import os from "node:os"

const PORT = Number(process.env.ZEN_PROXY_PORT || 8787)
const HOST = process.env.ZEN_PROXY_HOST || "127.0.0.1"
const MAIN_MODEL = process.env.ZEN_MODEL || "opencode/nemotron-3-ultra-free"
const SMALL_MODEL = process.env.ZEN_SMALL_MODEL || "opencode/ling-3.0-tiny-free"
const DEFAULT_PROVIDER = "opencode"
const DEBUG = Boolean(process.env.ZEN_PROXY_DEBUG)

const HOME = os.homedir()
const AUTH_PATH = process.env.OPENCODE_AUTH || `${HOME}/.local/share/opencode/auth.json`
const CATALOG_PATH = process.env.OPENCODE_CATALOG || `${HOME}/.cache/opencode/models.json`

// Providers whose catalog entry carries no api URL because opencode drives them
// with a dedicated SDK. Each of these does expose an OpenAI-compatible path.
const KNOWN_BASES = {
	google: "https://generativelanguage.googleapis.com/v1beta/openai",
	openai: "https://api.openai.com/v1",
	groq: "https://api.groq.com/openai/v1",
	mistral: "https://api.mistral.ai/v1",
	xai: "https://api.x.ai/v1",
	cerebras: "https://api.cerebras.ai/v1",
	deepseek: "https://api.deepseek.com/v1",
	anthropic: "https://api.anthropic.com",
}
// Already the right shape: forwarded untouched.
const NATIVE = new Set(["anthropic"])
// Signed requests or deployment-specific routing; out of reach from here.
const UNSUPPORTED = new Set(["amazon-bedrock", "azure", "vertex", "google-vertex"])

const log = (...a) => console.error(new Date().toISOString(), ...a)
const debug = (...a) => { if (DEBUG) log("debug", ...a) }

// Read-through cache: these files change when you run `opencode auth login`, and
// re-reading them on every request would be silly.
const cache = new Map()
function readJson(path) {
	try {
		const stamp = fs.statSync(path).mtimeMs
		const hit = cache.get(path)
		if (hit && hit.stamp === stamp) return hit.value
		const value = JSON.parse(fs.readFileSync(path, "utf8"))
		cache.set(path, { stamp, value })
		return value
	} catch {
		return {}
	}
}

function credentialFor(provider) {
	const entry = readJson(AUTH_PATH)[provider]
	if (!entry || typeof entry !== "object") return ""
	// api keys, and the access token shape oauth logins leave behind.
	return entry.key || entry.access || entry.token || ""
}

// "google/gemini-2.5-flash" -> google + gemini-2.5-flash
// "openrouter/qwen/qwen3-coder" -> openrouter + qwen/qwen3-coder
// "glm-5-free" -> the default provider
function resolveTarget(spec) {
	const clean = String(spec || "").replace(/\[[^\]]*\]$/, "").trim()
	const slash = clean.indexOf("/")
	let provider = DEFAULT_PROVIDER
	let model = clean
	if (slash > 0) {
		const head = clean.slice(0, slash)
		if (readJson(CATALOG_PATH)[head] || KNOWN_BASES[head]) {
			provider = head
			model = clean.slice(slash + 1)
		}
	}
	if (UNSUPPORTED.has(provider))
		return { error: `${provider} needs signed requests, which this proxy cannot do` }

	const catalog = readJson(CATALOG_PATH)[provider] || {}
	const base = (KNOWN_BASES[provider] || catalog.api || "").replace(/\/$/, "")
	if (!base) return { error: `no endpoint known for provider "${provider}"` }

	const key = credentialFor(provider)
	// Zen's free models are the one case that needs no credential at all.
	if (!key && !(provider === DEFAULT_PROVIDER && model.includes("free")))
		return { error: `no credential for "${provider}" - run: opencode auth login` }

	return { provider, model, base, key, native: NATIVE.has(provider) }
}

// Claude Code asks for its own model names for background work, and appends
// things like [1m] when it wants a larger window. Neither is a real model here.
function pickSpec(requested) {
	const m = String(requested || "").replace(/\[[^\]]*\]$/, "").trim()
	if (!m) return MAIN_MODEL
	// Claude Code's own names are the only ones rewritten: haiku is what it uses
	// for cheap background work, and claude-*/sonnet/opus is whatever it thinks it
	// is talking to.
	if (/haiku/i.test(m)) return SMALL_MODEL
	if (/^claude-|^sonnet|^opus/i.test(m)) return MAIN_MODEL
	// Anything a human named is used as given, so an unusable choice is reported
	// rather than quietly swapped for a different model.
	return m
}

function textOf(content) {
	if (typeof content === "string") return content
	if (!Array.isArray(content)) return ""
	return content
		.filter((b) => b && (b.type === "text" || typeof b.text === "string"))
		.map((b) => b.text || "")
		.join("\n")
}

function toOpenAI(body) {
	const messages = []
	const system = textOf(body.system)
	if (system) messages.push({ role: "system", content: system })

	for (const m of body.messages || []) {
		if (typeof m.content === "string") {
			messages.push({ role: m.role, content: m.content })
			continue
		}
		const texts = []
		const toolCalls = []
		const toolResults = []
		for (const b of m.content || []) {
			if (!b || !b.type) continue
			switch (b.type) {
				case "text":
					texts.push(b.text || "")
					break
				case "tool_use":
					toolCalls.push({
						id: b.id,
						type: "function",
						function: { name: b.name, arguments: JSON.stringify(b.input ?? {}) },
					})
					break
				case "tool_result":
					toolResults.push({
						role: "tool",
						tool_call_id: b.tool_use_id,
						content: textOf(b.content) || (b.is_error ? "error" : ""),
					})
					break
				case "image": {
					// Kept rather than dropped: a model that cannot see it will say so,
					// which is more useful than silently losing the attachment.
					const src = b.source || {}
					if (src.type === "base64" && src.data)
						texts.push(`[image ${src.media_type || "image/png"}, ${src.data.length} base64 chars omitted]`)
					break
				}
				// thinking and redacted_thinking are Anthropic-only bookkeeping; the
				// upstream model has its own reasoning channel and does not want ours.
				default:
					break
			}
		}
		// Tool results are their own messages upstream and must not be folded into
		// the user turn that carried them.
		messages.push(...toolResults)
		if (m.role === "assistant") {
			const out = { role: "assistant" }
			const joined = texts.join("\n").trim()
			if (joined) out.content = joined
			if (toolCalls.length) out.tool_calls = toolCalls
			if (out.content || out.tool_calls) messages.push(out)
		} else {
			const joined = texts.join("\n").trim()
			if (joined) messages.push({ role: "user", content: joined })
		}
	}

	const req = {
		// The router sets the model, once the provider is known.
		messages,
		stream: Boolean(body.stream),
	}
	if (body.max_tokens) req.max_tokens = body.max_tokens
	if (typeof body.temperature === "number") req.temperature = body.temperature
	if (typeof body.top_p === "number") req.top_p = body.top_p
	if (Array.isArray(body.stop_sequences) && body.stop_sequences.length) req.stop = body.stop_sequences
	if (req.stream) req.stream_options = { include_usage: true }

	// Only client-side tools travel; Anthropic's server-side tools have no
	// equivalent and would be rejected as malformed functions.
	const tools = (body.tools || []).filter((t) => t && t.name && t.input_schema)
	if (tools.length) {
		req.tools = tools.map((t) => ({
			type: "function",
			function: {
				name: t.name,
				description: t.description || "",
				parameters: t.input_schema || { type: "object", properties: {} },
			},
		}))
	}
	const tc = body.tool_choice
	if (tc && req.tools) {
		if (tc.type === "auto") req.tool_choice = "auto"
		else if (tc.type === "any") req.tool_choice = "required"
		else if (tc.type === "tool" && tc.name) req.tool_choice = { type: "function", function: { name: tc.name } }
		else if (tc.type === "none") req.tool_choice = "none"
	}
	return req
}

const STOP_REASONS = {
	stop: "end_turn",
	length: "max_tokens",
	tool_calls: "tool_use",
	function_call: "tool_use",
	content_filter: "end_turn",
}

function upstreamFetch(payload, target) {
	const headers = { "content-type": "application/json" }
	if (target.key) headers.authorization = `Bearer ${target.key}`
	return fetch(`${target.base}/chat/completions`, {
		method: "POST",
		headers,
		body: JSON.stringify(payload),
	})
}

// An Anthropic key needs no translation in either direction: the request is
// already in the right shape and so is the answer. Rewrite the model name, add
// the credential, and get out of the way.
async function passthrough(res, body, target) {
	const upstream = await fetch(`${target.base}/v1/messages`, {
		method: "POST",
		headers: {
			"content-type": "application/json",
			"x-api-key": target.key,
			"anthropic-version": "2023-06-01",
		},
		body: JSON.stringify({ ...body, model: target.model }),
	})
	res.writeHead(upstream.status, {
		"content-type": upstream.headers.get("content-type") || "application/json",
	})
	if (!upstream.body) return res.end()
	for await (const chunk of upstream.body) res.write(Buffer.from(chunk))
	res.end()
}

function anthropicError(res, status, message) {
	const body = JSON.stringify({ type: "error", error: { type: "api_error", message } })
	res.writeHead(status, { "content-type": "application/json" })
	res.end(body)
}

async function handleNonStreaming(res, payload, model, target) {
	const upstream = await upstreamFetch(payload, target)
	const text = await upstream.text()
	if (!upstream.ok) return anthropicError(res, upstream.status, `upstream ${upstream.status}: ${text.slice(0, 400)}`)
	let data
	try {
		data = JSON.parse(text)
	} catch {
		return anthropicError(res, 502, `upstream sent no JSON: ${text.slice(0, 200)}`)
	}
	const choice = (data.choices || [])[0] || {}
	const msg = choice.message || {}
	const content = []
	if (msg.content) content.push({ type: "text", text: msg.content })
	for (const call of msg.tool_calls || []) {
		let input = {}
		try {
			input = JSON.parse(call.function?.arguments || "{}")
		} catch {
			input = { _raw: call.function?.arguments || "" }
		}
		content.push({ type: "tool_use", id: call.id || `call_${Math.random().toString(36).slice(2)}`, name: call.function?.name, input })
	}
	if (!content.length) content.push({ type: "text", text: "" })
	const usage = data.usage || {}
	res.writeHead(200, { "content-type": "application/json" })
	res.end(JSON.stringify({
		id: data.id || `msg_${Date.now()}`,
		type: "message",
		role: "assistant",
		model,
		content,
		stop_reason: STOP_REASONS[choice.finish_reason] || "end_turn",
		stop_sequence: null,
		usage: {
			input_tokens: usage.prompt_tokens || 0,
			output_tokens: usage.completion_tokens || 0,
		},
	}))
}

async function handleStreaming(res, payload, model, target) {
	const upstream = await upstreamFetch(payload, target)
	if (!upstream.ok || !upstream.body) {
		const text = upstream.body ? await upstream.text() : ""
		return anthropicError(res, upstream.status || 502, `upstream ${upstream.status}: ${text.slice(0, 400)}`)
	}

	res.writeHead(200, {
		"content-type": "text/event-stream",
		"cache-control": "no-cache",
		connection: "keep-alive",
	})
	const send = (event, data) => res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`)

	const id = `msg_${Date.now()}`
	send("message_start", {
		type: "message_start",
		message: {
			id, type: "message", role: "assistant", model, content: [],
			stop_reason: null, stop_sequence: null,
			usage: { input_tokens: 0, output_tokens: 0 },
		},
	})

	// Anthropic numbers content blocks and expects each one opened and closed
	// exactly once, so the mapping from OpenAI's flat deltas has to be tracked.
	let nextIndex = 0
	let textIndex = null
	const toolBlocks = new Map() // upstream tool_call index -> our block index
	let stopReason = "end_turn"
	let usage = { input_tokens: 0, output_tokens: 0 }

	const closeText = () => {
		if (textIndex !== null) {
			send("content_block_stop", { type: "content_block_stop", index: textIndex })
			textIndex = null
		}
	}

	let buffer = ""
	try {
		for await (const chunk of upstream.body) {
			buffer += Buffer.from(chunk).toString("utf8")
			const lines = buffer.split("\n")
			buffer = lines.pop() || ""
			for (const line of lines) {
				const trimmed = line.trim()
				if (!trimmed.startsWith("data:")) continue
				const raw = trimmed.slice(5).trim()
				if (!raw || raw === "[DONE]") continue
				let parsed
				try {
					parsed = JSON.parse(raw)
				} catch {
					debug("unparseable chunk", raw.slice(0, 120))
					continue
				}
				if (parsed.usage) {
					usage = {
						input_tokens: parsed.usage.prompt_tokens || usage.input_tokens,
						output_tokens: parsed.usage.completion_tokens || usage.output_tokens,
					}
				}
				const choice = (parsed.choices || [])[0]
				if (!choice) continue
				const delta = choice.delta || {}

				if (typeof delta.content === "string" && delta.content.length) {
					if (textIndex === null) {
						textIndex = nextIndex++
						send("content_block_start", {
							type: "content_block_start", index: textIndex,
							content_block: { type: "text", text: "" },
						})
					}
					send("content_block_delta", {
						type: "content_block_delta", index: textIndex,
						delta: { type: "text_delta", text: delta.content },
					})
				}

				for (const call of delta.tool_calls || []) {
					const key = call.index ?? 0
					if (!toolBlocks.has(key)) {
						// A tool call cannot be interleaved with text in Anthropic's
						// stream, so the text block closes first.
						closeText()
						const index = nextIndex++
						toolBlocks.set(key, index)
						send("content_block_start", {
							type: "content_block_start", index,
							content_block: {
								type: "tool_use",
								id: call.id || `call_${key}_${Date.now()}`,
								name: call.function?.name || "unknown",
								input: {},
							},
						})
					}
					const fragment = call.function?.arguments
					if (fragment) {
						send("content_block_delta", {
							type: "content_block_delta", index: toolBlocks.get(key),
							delta: { type: "input_json_delta", partial_json: fragment },
						})
					}
				}

				if (choice.finish_reason) stopReason = STOP_REASONS[choice.finish_reason] || "end_turn"
			}
		}
	} catch (err) {
		log("stream failed:", err.message)
	}

	closeText()
	for (const index of toolBlocks.values()) send("content_block_stop", { type: "content_block_stop", index })
	send("message_delta", {
		type: "message_delta",
		delta: { stop_reason: stopReason, stop_sequence: null },
		usage: { output_tokens: usage.output_tokens },
	})
	send("message_stop", { type: "message_stop" })
	res.end()
}

const server = http.createServer((req, res) => {
	if (req.method === "GET" && (req.url === "/health" || req.url === "/")) {
		res.writeHead(200, { "content-type": "application/json" })
		const providers = Object.keys(readJson(AUTH_PATH))
		return res.end(JSON.stringify({
			ok: true, model: MAIN_MODEL, small: SMALL_MODEL,
			authenticated: providers, free: "opencode/*-free needs no credential",
		}))
	}

	let body = ""
	req.on("data", (c) => { body += c })
	req.on("end", async () => {
		let parsed = {}
		try {
			parsed = body ? JSON.parse(body) : {}
		} catch {
			return anthropicError(res, 400, "request body was not JSON")
		}

		// Claude Code asks for a token count before large requests. An estimate
		// keeps it moving; Zen exposes no counting endpoint.
		if (req.url?.includes("count_tokens")) {
			const chars = JSON.stringify(parsed.messages || []).length + textOf(parsed.system).length
			res.writeHead(200, { "content-type": "application/json" })
			return res.end(JSON.stringify({ input_tokens: Math.ceil(chars / 4) }))
		}

		if (!req.url?.includes("/messages")) return anthropicError(res, 404, `no route for ${req.url}`)

		const spec = pickSpec(parsed.model)
		const target = resolveTarget(spec)
		if (target.error) {
			log(`cannot route "${spec}": ${target.error}`)
			return anthropicError(res, 400, `${target.error} (model "${spec}")`)
		}
		debug("->", `${target.provider}/${target.model}`, "stream:", Boolean(parsed.stream),
			"tools:", parsed.tools?.length || 0, "messages:", (parsed.messages || []).length)
		try {
			if (target.native) return await passthrough(res, parsed, target)
			const payload = toOpenAI(parsed)
			payload.model = target.model
			const shown = `${target.provider}/${target.model}`
			if (payload.stream) await handleStreaming(res, payload, shown, target)
			else await handleNonStreaming(res, payload, shown, target)
		} catch (err) {
			log("request failed:", err.message)
			if (!res.headersSent) anthropicError(res, 502, err.message)
			else res.end()
		}
	})
})

server.listen(PORT, HOST, () => {
	const providers = Object.keys(readJson(AUTH_PATH))
	log(`anthropic-proxy on http://${HOST}:${PORT}`)
	log(`main ${MAIN_MODEL}, background ${SMALL_MODEL}`)
	log(providers.length
		? `credentials from opencode for: ${providers.join(", ")} (plus opencode/*-free, which needs none)`
		: `no opencode credentials found; only opencode/*-free will work`)
})
