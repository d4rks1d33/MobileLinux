# Claude Code on your opencode providers

	sudo ./install.sh
	claude-free                               # Claude Code, routed through opencode
	claude-free --model                       # pick one interactively (search + arrows)
	claude-free --model google/gemini-2.5-pro # or set it directly
	claude-free --doctor

`claude-free --model` with no argument opens a searchable picker: type to filter,
arrow keys to move, Enter to select. It saves the choice and restarts the proxy,
so the next run uses it. It is a small node script (`claude-free-pick.mjs`) using
`@inquirer/search`, which reads keypresses directly and so works even when TERM
is `unknown` (the norm in this phone's shell). When run non-interactively (piped,
or no picker/node), `--model` prints the plain grouped listing instead.

Anything in the environment wins over the config file, so
`CLAUDE_FREE_MODEL=google/gemini-2.5-pro claude-free` is a one-off without editing
anything.

Models are named `provider/model`. **Whatever you have logged into with `opencode
auth login` is available here**, because the proxy reads the keys opencode already
holds — nothing is copied, and no key is written anywhere by this:

	opencode - free, no credential needed
	     1000000   128000  yes    opencode/nemotron-3-ultra-free
	s     262144    32768  yes    opencode/ling-3.0-tiny-free

	google - your key, held by opencode
	    1048576     65536  yes    google/gemini-flash-latest
	*   1048576     65536  yes    google/gemini-2.5-flash

`*` is in use, `s` is background work. A bare name means `opencode`, so older
configs keep working. Add a provider to opencode and its models appear here with
no further work.

## In a root shell

Both `claude` and `claude-free` work as root. Two separate problems had to be
solved for that:

- **`claude` is not on root's PATH.** It installs and updates itself inside a home
  directory, so `/usr/local/bin/claude` is a shim that finds it wherever it lives
  rather than a symlink to a path an update could move.
- **root's home has none of opencode's files.** The catalog, the keys and the model
  choice all live in the desktop user's home, so a root shell falls back to that
  user rather than reporting nothing configured:

	running as    root, using /home/kali for opencode's files and config

  The model choice is shared, which is what one person on one machine expects.
  Trust is not shared: `~/.claude.json` stays per user, as Claude Code intends.

Verified as root: `claude --version` answers, and a tool-using job on
`google/gemini-2.5-flash` — with the key from `/home/kali` — read a file and
answered correctly.

Worth being explicit: this means a **root shell runs a binary the desktop user can
replace**. On a single-user machine where that user already has sudo it grants
nothing new, but on a shared machine do not install the shim —
`CLAUDE_FREE_NO_SHIM=1 ./install.sh` skips it.

## Inside a session

Claude Code's own `/model` picker accepts **one** custom entry — its code pushes a
single option built from `ANTHROPIC_CUSTOM_MODEL_OPTION` — so that entry is the
model in use. Any other is reachable by typing it:

	/model opencode/glm-5-free

which was measured rather than assumed: the proxy logged that one request going to
`opencode/glm-5-free` while the rest of the session stayed on the configured
model.

## Why this works at all

Three facts, each checked on the device before any code was written:

	claude binary        56 references to ANTHROPIC_BASE_URL
	opencode auth.json   the keys, as {"google": {"type": "api", "key": ...}}
	free model, no key   POST to Zen /chat/completions -> HTTP 200

Claude Code talks to exactly one API shape and lets you move it with
`ANTHROPIC_BASE_URL`; most providers are reachable with the OpenAI shape. So the
only missing piece is a translator that knows where each provider lives.

Two things about *where* they live were not obvious:

- The catalog records an `api` URL only for providers opencode drives with its
  generic openai-compatible SDK. `google`, `openai`, `groq`, `mistral`, `xai` and
  `cerebras` have none, because they ship dedicated SDKs — yet they all expose an
  OpenAI-compatible path anyway, so those are in a table in the proxy. Verified
  for google: `/v1beta/openai/chat/completions` answers, tool calls included.
- **Anthropic is not translated at all.** With an Anthropic key the request is
  already the right shape, so it is forwarded untouched: less code, perfect
  fidelity.

`amazon-bedrock`, `azure` and Vertex need signed or deployment-specific requests,
and are refused with that as the reason rather than failing obscurely.

Every Zen free model reports `tool_call: true`, which is the part that matters:
Claude Code is a tool-calling loop, and a model that cannot call tools is useless
to it however well it writes.

## The proxy

`anthropic-proxy.mjs`, Node standard library only — adding a dependency tree to a
phone to translate two JSON shapes would be a poor trade.

The request direction is mostly renaming. The response direction is not: Claude
Code expects Anthropic's event sequence

	message_start
	content_block_start / content_block_delta / content_block_stop
	message_delta (stop_reason, usage)
	message_stop

with tool calls arriving as `input_json_delta` fragments, while OpenAI streams the
same information in a different arrangement. That is a small state machine, not a
field mapping: blocks have to be numbered, opened once and closed once, and a tool
call has to close any open text block first. Providers differ here too — Zen
streams token by token, Gemini's OpenAI endpoint sends the whole answer in one
chunk — and both end up as the same event sequence.

Model names from Claude Code are mapped rather than passed through: it asks for
`haiku` for cheap background work and appends things like `[1m]` to request a
larger window. Names *you* choose are used exactly as given, so an unusable one is
reported instead of quietly swapped:

	openai/gpt-4o-mini               no credential for "openai" - run: opencode auth login
	amazon-bedrock/anthropic.claude  needs signed requests, which this proxy cannot do

## Verified end to end

Not "the proxy returns 200", but Claude Code doing a job, on two providers:

	$ echo 'the secret key is rhodep-4242' > data.txt

	# a Zen free model
	$ claude-free --model opencode/nemotron-3.5-lightning-free
	$ claude-free -p "Read data.txt and reply with only the key" --allowedTools=Read
	rhodep-4242

	# google/gemini-2.5-flash, with your own key
	$ claude-free -p "Read data.txt and reply with only the key" --allowedTools=Read
	The key is rhodep-4242.

Each needed the whole loop: tools sent upstream, a `tool_use` block streamed back,
Claude Code running Read, the `tool_result` translated into an OpenAI `tool`
message, and a second round trip. Text-only and non-streaming paths were checked
separately.

## What survives an update

**opencode updating changes nothing.** Nothing here runs opencode. The proxy talks
HTTP to each provider, and opencode's files are only *read*: `auth.json` for keys
and `models.json` for the catalog. If the catalog's shape ever changes, `--model`
lists nothing and the context hint is skipped; sessions still work.

**Claude Code updating is low risk but not zero.** The variables it is driven with
— `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_MODEL`,
`ANTHROPIC_SMALL_FAST_MODEL` — are the ones every proxy setup uses, so they are
unlikely to move. What could: a new version calling an endpoint the proxy does not
implement, which comes back as an Anthropic-shaped error with the route named in
the log rather than as a hang; or the `~/.claude.json` format changing, which makes
`--trust` a no-op instead of corrupting the file. Note `claude` lives in
`~/.local/bin` and is not an apt package, so the port's holds do not cover it.

**The likely breakage is neither.** Zen's free models are promotional and get
retired without notice, which would fail every request upstream. So the configured
model is checked at launch, and if it is gone the largest free model that still
supports tool calling is used, with a line saying how to make it permanent.

`claude-free --doctor` checks the whole chain at once — node, python3, claude, the
proxy, the catalog, which credentials exist, whether both models are still listed,
whether the proxy is up, and whether this directory is trusted.

## The trust prompt

Claude Code asks once per directory and remembers the answer itself. That works —
measured three ways in a directory that had never been opened:

	answer it, exit with /exit         hasTrustDialogAccepted = true
	answer it, then kill the session   hasTrustDialogAccepted = true
	open it again                      no question asked

So this does **not** touch trust on launch. Two earlier theories of mine were
wrong: it is not that the answer needs a clean exit, and it is not that the write
fails. What was actually wrong was a stored `false` for `/home/kali`, which makes
the question come back every time.

For that case, and for pre-approving a directory deliberately:

	claude-free --trust      # this directory, no question next time
	claude-free --untrust    # ask again

`CLAUDE_FREE_AUTO_TRUST=1` in the config trusts every directory on launch. Off by
default, because trusting a directory unasked is what the question exists for.

## What it does not do

- **Images are not forwarded.** They become a placeholder line, so the model can
  say it cannot see them instead of the attachment vanishing silently.
- **Prompt caching does nothing.** `cache_control` is dropped, so long sessions
  pay full input tokens every turn — worth knowing on a metered key.
- **Anthropic `thinking` blocks are dropped**, and a provider's own `reasoning`
  field is ignored on the way back rather than shown as text.
- **One diagnostic line per session stays.** Claude Code prints
  `[claude-code:unrecognized_model]` because these models are not in its table.
  `CLAUDE_CODE_MAX_CONTEXT_TOKENS`, set from the catalog, removes the paragraph
  about auto-compacting, but the rest needs a `modelOverrides` format only visible
  inside a minified binary, and filtering stderr would hide real errors.
- **Free models are rate limited, and not all of them are actually free.** Zen
  answers a 429 as `FreeUsageLimitError: Rate limit exceeded`, and some models the
  catalog lists as free answer `401 ModelError: Model glm-5-free is not
  supported...` without a key. Both arrive with the provider's own message intact
  rather than as something mysterious — the list comes from the catalog, but Zen
  decides at request time. Your own key has its own quota and is unaffected.
- **A free model is not Claude.** They call tools and follow instructions less
  reliably, which shows up as loops and skipped steps in long agentic runs. Your
  own paid key, routed the same way, behaves like that provider normally does.

## Where things are

	/usr/local/bin/claude-free                     the wrapper
	/usr/local/libexec/rhodep/anthropic-proxy.mjs  the translator
	~/.config/rhodep/claude-free.conf              model choices
	~/.cache/claude-free/proxy.log                 the proxy's log

The proxy listens on `127.0.0.1` only and starts on demand, with the wrapper
waiting on `/health` rather than racing it, since a refused first connection looks
exactly like a broken proxy.
