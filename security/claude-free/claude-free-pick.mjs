#!/usr/bin/env node
// Interactive model picker for claude-free with search support.
//
// Reads opencode's catalog and auth, shows a searchable list (type to filter,
// arrows to move, Enter to pick), writes the choice to claude-free.conf and
// restarts the proxy so the next run uses it. @inquirer/search reads keypresses
// directly, so it works even when TERM is unknown.

import search from "@inquirer/search"
import fs from "node:fs"
import os from "node:os"
import path from "node:path"

const HOME = os.homedir()
const CATALOG_PATH = process.env.OPENCODE_CATALOG || path.join(HOME, ".cache/opencode/models.json")
const AUTH_PATH = process.env.OPENCODE_AUTH || path.join(HOME, ".local/share/opencode/auth.json")
// The wrapper tells us where its config lives; fall back to the rhodep path.
const CONF_PATH = process.env.CLAUDE_FREE_CONF || path.join(HOME, ".config/rhodep/claude-free.conf")

// Load catalog + auth
function loadCatalog() {
	try {
		const alt = path.join(HOME, ".local/share/opencode/mcp-auth.json")
		const authFile = fs.existsSync(AUTH_PATH) ? AUTH_PATH : (fs.existsSync(alt) ? alt : AUTH_PATH)
		return {
			catalog: JSON.parse(fs.readFileSync(CATALOG_PATH, "utf8")),
			auth: JSON.parse(fs.readFileSync(authFile, "utf8"))
		}
	} catch {
		return { catalog: {}, auth: {} }
	}
}

// Build model list from catalog
function buildModelList(catalog, auth) {
	const models = []

	// Opencode free models first
	if (catalog.opencode?.models) {
		for (const [name, model] of Object.entries(catalog.opencode.models)) {
			if (!name.includes("free")) continue
			const limit = model.limit || {}
			models.push({
				name: `opencode/${name}`,
				value: `opencode/${name}`,
				description: `context: ${limit.context || "?"}, tools: ${model.tool_call ? "yes" : "no"}, free`,
				context: limit.context || 0
			})
		}
	}

	// Providers from auth.json
	for (const provider of Object.keys(auth)) {
		if (provider === "opencode") continue
		if (!catalog[provider]?.models) continue

		const providerModels = Object.entries(catalog[provider].models)
			.map(([name, model]) => {
				const limit = model.limit || {}
				return {
					name: `${provider}/${name}`,
					value: `${provider}/${name}`,
					description: `context: ${limit.context || "?"}, tools: ${model.tool_call ? "yes" : "no"}`,
					context: limit.context || 0
				}
			})
			.sort((a, b) => b.context - a.context)

		models.push(...providerModels)
	}

	return models.sort((a, b) => b.context - a.context)
}

// Save model choice to config, preserving the other keys
function saveConfig(model) {
	const configDir = path.dirname(CONF_PATH)
	if (!fs.existsSync(configDir)) {
		fs.mkdirSync(configDir, { recursive: true })
	}

	let config = {}
	if (fs.existsSync(CONF_PATH)) {
		const content = fs.readFileSync(CONF_PATH, "utf8")
		content.split("\n").forEach(line => {
			const match = line.match(/^([A-Z_]+)=(.*)$/)
			if (match) config[match[1]] = match[2]
		})
	}

	config.CLAUDE_FREE_MODEL = model

	const content = Object.entries(config)
		.map(([k, v]) => `${k}=${v}`)
		.join("\n")

	fs.writeFileSync(CONF_PATH, content + "\n")
}

// Main
async function main() {
	const { catalog, auth } = loadCatalog()
	const models = buildModelList(catalog, auth)

	if (models.length === 0) {
		console.error("No models found. Run opencode once to populate the catalog.")
		process.exit(1)
	}

	// Get current model
	let currentModel = process.env.CLAUDE_FREE_MODEL
	if (!currentModel && fs.existsSync(CONF_PATH)) {
		const content = fs.readFileSync(CONF_PATH, "utf8")
		const match = content.match(/^CLAUDE_FREE_MODEL=(.+)$/m)
		if (match) currentModel = match[1]
	}

	console.error(`\n  Type to search, arrow keys to navigate, Enter to select`)
	console.error(`  Current: ${currentModel || "none"}\n`)

	const choices = models.map(m => ({
		name: m.name,
		value: m.value,
		description: m.description
	}))

	const selected = await search({
		message: "Select model",
		source: async (input) => {
			if (!input) return choices.slice(0, 30)
			const query = input.toLowerCase()
			return choices.filter(c =>
				c.name.toLowerCase().includes(query) ||
				c.value.toLowerCase().includes(query)
			).slice(0, 30)
		}
	})

	if (selected) {
		saveConfig(selected)
		console.error(`\n  \u2713 Model set to: ${selected}`)
		console.error(`  Run 'claude-free' to use it\n`)

		// Kill proxy so it restarts with new model
		const { execSync } = await import("child_process")
		try {
			execSync("pkill -f 'anthropic-proxy.mjs'", { stdio: "ignore" })
		} catch {}
	}
}

main().catch(err => {
	console.error(err.message)
	process.exit(1)
})
