# awrtifact — Aither World Artifact

<!-- aither-header:start GENERATED from the ecosystem registry. Edits here are overwritten; change the registry instead. -->

**[Docs](https://aitherium.github.io/awrtifact/)**  ·  [Source](https://github.com/Aitherium/awrtifact)  ·  `pip install awrtifact`  ·  [The Aither World](https://aitherium.github.io/)

> **The Aither World** is an operating system for agents — a Linux you can hand to one, the runtimes it works in, and the tools it works with. [awnix](https://github.com/Aitherium/awnix) is the Linux underneath it; **awrtifact** is one of its 41 bricks — each installs on its own, runs offline, and needs no account.
>
> **Start here:** Store one artifact as a versioned GitHub release and fetch it back byte-verified — `awrtifact mirror <URL|FILE> --release TAG`.

<!-- aither-header:end -->

Deliberately chunk artifacts into GitHub release assets and fetch them back
byte-verified. The productized aitherkvcache mirror lane: any artifact (model
weights, datasets, builds, backups) → `.partN` slices under GitHub's 2 GiB
per-asset cap → versioned release → served by a generated Cloudflare Worker
(CORS + HTTP Range + stitching) → fetched back with size and sha256 checks.

## The loop

```bash
# 1. Split a local artifact into .partN slices + manifest.json
awrtifact split DeepSeek-V4-Flash.gguf --out parts/

# 2. What is missing from the release? (resumable — re-runs upload only gaps)
awrtifact plan parts/manifest.json --repo Aitherium/aitherkvcache --release fleet-v1

# 3. Upload the missing parts (size-checked before upload, --create makes the release)
awrtifact upload parts/manifest.json --repo Aitherium/aitherkvcache \
    --release fleet-v1 --dir parts --parallel 8 --create

# 4. Prove the local parts match the manifest byte-for-byte
awrtifact verify parts/manifest.json --dir parts

# 5. Fetch it back anywhere (resume + size check + sha256 TOFU lockfile)
awrtifact fetch DeepSeek-V4-Flash.gguf --url https://weights.example.com/ \
    --out /srv/models --expected 49999999999

# 6. Or let the spec drive everything: generate the serving worker from it
awrtifact serve-spec awrtifact.yaml --check   # drift gate
awrtifact serve-spec awrtifact.yaml           # write the generated worker

# 7. Bring the backup up deliberately (dispatch mirror runs for gaps)
awrtifact backup-catalog awrtifact.yaml --dry-run
```

## One command to a working artifact store repo

`provision-repo` creates the repo if missing, seeds it with an init README
plus the share gate page, enables Pages, and prints the shareable URL:

```bash
awrtifact provision-repo --repo you/store                # private
awrtifact provision-repo --repo you/store --public       # Pages always works
```

The store is then ready for every lane — `awrtifact upload`, `awrtifact
mirror`, the GobboNet backup mod, any aw* client that speaks the chunk
contract. A brand-new repo is the ordinary FIRST use, and GitHub refuses to
publish releases in an empty repo — the seeding is what makes it real.

The repo's GitHub Pages can carry a share gate for **passphrase-encrypted**
backups: `https://you.github.io/store/backup-gate.html` shows a tokenless
manifest preview of an encrypted backup release and decrypts in the browser
for anyone with the passphrase. Encryption is a property of the CLIENT that
writes a backup, not of the store — the CLI's parts are plaintext (verified
by sha256), the GobboNet backup mod writes AES-GCM ciphertext, and both are
releases in the same store, fetchable byte-verified by either side. Private
repos work immediately (Pages needs a paid plan there for the gate half —
the command warns, it never fails).

**See the gate live** (the GobboNet mod's own Pages deployment):
[wizzense.github.io/GobboNet/backup-gate.html](https://wizzense.github.io/GobboNet/backup-gate.html)
— pick any encrypted backup release from the drop-down, enter the
passphrase, and watch it verify + decrypt in the browser.

## Clients of the contract

The chunk contract (`.partN` slices + per-part and whole sha256, under
GitHub's 2 GiB asset cap) is the interoperability boundary — anything that
speaks it can read anything that writes it:

- **the CLI** (`split`/`upload`/`mirror`/`fetch`/`verify`) — programmatic,
  plaintext, resumable
- **the workers** (`serve-spec` → artifact.aitherium.com / weights.aitherium.com)
  — Range-stitched serving of the same releases
- **the GobboNet backup mod** (`gobbonet-backup.js`) — browser-side,
  passphrase-encrypted backups into your own store, sharing via the gate page

One store, three clients, one manifest contract.

## First mirror into a repo that is not aitherkvcache

The mirror lane dispatches a GitHub Actions workflow, and the workflow must
live in the TARGET repo first. For your own repo (public or private):

1. Copy `mirror-to-release.yml` and `hash-release-object.yml` (from the
   awrtifact source tree, `.DEPLOYMENT/workers/awrtifact/`) into your repo's
   `.github/workflows/` and push.
2. Then `awrtifact mirror <URL|FILE> --repo you/your-repo --release <tag>`
   works as usual.

Without the workflow the mirror refuses loudly (it never silently does
nothing) — the refusal names the two files to copy.

## Why the checks exist

- **2 GiB cap.** GitHub release assets top out at 2 GiB; larger artifacts are
  split at 1.9 GB. The worker stitches `.partN` slices behind the original
  filename, so clients ask for the name, never the parts.
- **Size is the truncation detector.** A short file is not a download error —
  the loader reports a corrupt artifact. Every step verifies sizes; the
  manifest's `total` is the number the client checks.
- **sha256 per part and whole.** Split records both; verify proves each slice
  and that the slices stitch back into the original.
- **Resume everywhere.** Uploads skip present parts; fetches resume from the
  byte count on disk via Range.

## Spec (the declarative store)

`awrtifact.yaml` names the store's repo, releases, allowlist and artifacts
(see `awrtifact/spec.py` for the full shape). The worker's data sections are
GENERATED from it — adding an artifact is a spec edit + regenerate, never a
worker code edit. `serve-spec --check` is the drift gate.

## Install

```bash
pip install awrtifact                   # core (stdlib-only)
pip install 'awrtifact[spec]'           # + spec commands (PyYAML)
```

Requires the `gh` CLI for plan/upload/backup commands (your existing auth).

## License

Apache-2.0

<!-- aither-ecosystem:start GENERATED from the ecosystem registry. Edits here are overwritten; change the registry instead. -->

## The aw family

Standalone tools that share one idea: **replace something you would otherwise have to _trust_ with something you can _check_.**

Each installs on its own, works offline, and needs no account.

| | instead of trusting | you check |
|---|---|---|
| [awdk](https://github.com/Aitherium/awdk) | a framework's idea of how your agents should run | one loop you can read, pointed at a backend you already pay for |
| [awskills](https://github.com/Aitherium/awskills) | that an agent knows your procedure | the procedure written down, versioned, and loadable by any agent |
| [awpack](https://github.com/Aitherium/awpack) | that the pack you want shipped inside somebody's SDK, under whatever licence that SDK happens to carry | the pack as its own versioned artifact, with its own licence, that any agent runtime can install |
| [awm](https://github.com/Aitherium/awm) | that memory stayed in its lane | tenant:user:project scopes, so a write cannot cross a boundary |
| [awnode](https://github.com/Aitherium/awnode) | a vendor's cloud with every prompt | a local gateway routing to backends you chose |
| [awgraph](https://github.com/Aitherium/awgraph) | that grep found everything | an AST + tree-sitter call graph an agent can traverse |
| [awgit](https://github.com/Aitherium/awgit) | that no one else is editing this file | a lease, refused at commit time if you do not hold it |
| [awdelphi](https://github.com/Aitherium/awdelphi) | one agent's confident take on a decision | the round trace, the anonymity, and who dissents |
| [awtoll](https://github.com/Aitherium/awtoll) | that your tooling is saving you context | the measured token cost of each tool call, and what the alternative cost |
| [awseal](https://github.com/Aitherium/awseal) | that the artifact came from who you think | an Ed25519 seal — the key that verifies is not the key that forges |
| [awshare](https://github.com/Aitherium/awshare) | that the download is intact | content-addressed bundles, verified on fetch |
| [awnest](https://github.com/Aitherium/awnest) | that there is a person on the other end | a verdict with evidence, where "we could not tell" is not "yes" |
| [awnboard](https://github.com/Aitherium/awnboard) | a share link anyone who sees it can use | an invitation addressed to one person, for one gate, revocable |
| [awnix](https://github.com/Aitherium/awnix) | that the box is what you left it as | an immutable image you built, with atomic rollback |
| [awrecover](https://github.com/Aitherium/awrecover) | that the restore worked | a restore that fully lands or does not land at all |
| [awrelay](https://github.com/Aitherium/awrelay) | a SaaS in the middle of your agents | findings, alerts and coordination over your own transport |
| [awmail](https://github.com/Aitherium/awmail) | a mailbox somebody else can read | mail your agents send and receive over your own server |
| [awfind](https://github.com/Aitherium/awfind) | one vendor's idea of the web | results from whichever providers you configured |
| [awbrowse](https://github.com/Aitherium/awbrowse) | that the page said what you were told | the render, the DOM and the requests it made |
| [gobbonet-agentic](https://github.com/Aitherium/gobbonet-agentic) | the model to keep a 300-message campaign coherent by itself | campaign facts recalled from scoped memory you can list and edit |
| [aitherkvcache](https://github.com/Aitherium/aitherkvcache) | a vendor's quantisation defaults | sub-byte KV cache kernels you can benchmark yourself |
| **awrtifact** _(you are here)_ | a hand-rolled split script and a hand-edited worker manifest | byte-verified parts in a release, served with Range + CORS, sizes asserted by a live gate |
| [AitherZero](https://github.com/Aitherium/AitherZero) | a pile of scripts nobody has numbered | numbered, discoverable automation with declarative playbooks |
| [AitherConnect](https://github.com/Aitherium/AitherConnect) | what a page tells your browser to do | a federated search and desktop bridge you host |
| [awreason](https://github.com/Aitherium/awreason) | a confident paragraph | the phases it went through, and every tool call it made to get there |
| [awrecurse](https://github.com/Aitherium/awrecurse) | that everything you pasted in was actually read | which slices it opened, and what it concluded from each |
| [awprism](https://github.com/Aitherium/awprism) | the first explanation that fits | the ranked alternatives, and the observation that separates them |
| [awrepl](https://github.com/Aitherium/awrepl) | what the agent believes the value is | the value, printed from the live session |
| [awresearch](https://github.com/Aitherium/awresearch) | a summary of pages nobody opened | every claim against the source it came from |
| [awfocus](https://github.com/Aitherium/awfocus) | twelve terminal tabs and a bad memory | one command that names every session, finds any transcript, and opens or steers the one you want |
| [awgym](https://github.com/Aitherium/awgym) | that a world model learned anything from the games it saw | transitions captured from real play, fed back, and the retrodiction score falling on grids it never saw |
| [awpredict](https://github.com/Aitherium/awpredict) | a model because it trained without erroring | its prediction against a self-updating lookup, on the rows that are actually novel |
| [awsh](https://github.com/Aitherium/awsh) | that you already know the name of the command | what it decided your line meant, before it acts on it |
| [awkno](https://github.com/Aitherium/awkno) | that the docs site is up, or that you remember the family | the whole ecosystem in your terminal, with no network at all |

[**awnix**](https://github.com/Aitherium/awnix) is the ground floor — A Linux you can hand to an agent — immutable base, capabilities included.

## The Aitherium ecosystem

Every repository here is public. Each publishes an `aither-manifest.json` beside its page, so any surface can read every sibling's — the network is browsable from any node in it.

| repo | what it is | pages |
|---|---|---|
| [awdk](https://github.com/Aitherium/awdk) | Build AI agent fleets — 3 lines, any backend, local or cloud | [docs](https://aitherium.github.io/awdk/) |
| [awskills](https://github.com/Aitherium/awskills) | Portable agent skills — self-contained procedures an agent loads on demand | [docs](https://aitherium.github.io/awskills/) |
| [awpack](https://github.com/Aitherium/awpack) | First-party agent packs — the ones we build, versioned and installable on their own | [docs](https://aitherium.github.io/awpack/) |
| [awm](https://github.com/Aitherium/awm) | A portable, scoped agent memory | [docs](https://aitherium.github.io/awm/) |
| [awnode](https://github.com/Aitherium/awnode) | A lightweight local gateway — bridges your apps to the AI backends you chose | [docs](https://aitherium.github.io/awnode/) |
| [awrun](https://github.com/Aitherium/awrun) | A priority-aware queue and dispatcher for agentic runs and ad-hoc CI builds. It also judges whether the runner pool is big enough for the queue it is draining, and can ask a host to grow it -- reserving capacity is zero-sum, so a saturated pool needs more of it, not a different share of it | [docs](https://aitherium.github.io/awrun/) |
| [awgraph](https://github.com/Aitherium/awgraph) | A semantic code graph for agents — AST + tree-sitter, call graphs | [docs](https://aitherium.github.io/awgraph/) |
| [awgit](https://github.com/Aitherium/awgit) | Semantic version control on top of git — edit-ops and leases | [docs](https://aitherium.github.io/awgit/) |
| [awdelphi](https://github.com/Aitherium/awdelphi) | Anonymous multi-round expert panels — a converged answer with a trace | [docs](https://aitherium.github.io/awdelphi/) |
| [awtoll](https://github.com/Aitherium/awtoll) | What every tool call costs you in context, measured from your own transcripts | [docs](https://aitherium.github.io/awtoll/) |
| [awseal](https://github.com/Aitherium/awseal) | Sign an artifact so a stranger can verify it | [docs](https://aitherium.github.io/awseal/) |
| [awshare](https://github.com/Aitherium/awshare) | Publish an artifact and fetch it back verified | [docs](https://aitherium.github.io/awshare/) |
| [awdit](https://github.com/Aitherium/awdit) | An append-only audit trail whose gaps are DETECTABLE | [docs](https://aitherium.github.io/awdit/) |
| [awbac](https://github.com/Aitherium/awbac) | Role-based access control that fails closed and explains itself | [docs](https://aitherium.github.io/awbac/) |
| [awiam](https://github.com/Aitherium/awiam) | Who is this caller? A directory and session store that fails honestly | [docs](https://aitherium.github.io/awiam/) |
| [awtunnel](https://github.com/Aitherium/awtunnel) | Reach a service that has no public address | [docs](https://aitherium.github.io/awtunnel/) |
| [awnest](https://github.com/Aitherium/awnest) | Prove there is a human before you let them into the nest | [docs](https://aitherium.github.io/awnest/) |
| [awnboard](https://github.com/Aitherium/awnboard) | A front gate you can put in front of anything, and hand someone the key to | [docs](https://aitherium.github.io/awnboard/) |
| [awnix](https://github.com/Aitherium/awnix) | A Linux you can hand to an agent — immutable base, capabilities included | [docs](https://aitherium.github.io/awnix/) |
| [awrecover](https://github.com/Aitherium/awrecover) | Labelled snapshots with an all-or-nothing restore | [docs](https://aitherium.github.io/awrecover/) |
| [awrelay](https://github.com/Aitherium/awrelay) | Portable agent messaging — findings, alerts, coordination | [docs](https://aitherium.github.io/awrelay/) |
| [awmail](https://github.com/Aitherium/awmail) | Give an agent an email address — send, and actually receive | [docs](https://aitherium.github.io/awmail/) |
| [awnet](https://github.com/Aitherium/awnet) | The agentic web — agents host a mesh, and agents join one | [docs](https://aitherium.github.io/awnet/) |
| [awfind](https://github.com/Aitherium/awfind) | A portable search client — query, results, ranking | [docs](https://aitherium.github.io/awfind/) |
| [awbrowse](https://github.com/Aitherium/awbrowse) | A portable browser client — navigate, console, network, DOM, screenshot | [docs](https://aitherium.github.io/awbrowse/) |
| [awknowledge](https://github.com/Aitherium/awknowledge) | How to run a coding agent so the result survives — the laws, with evidence | [docs](https://aitherium.github.io/awknowledge/) |
| [gobbonet-agentic](https://github.com/Aitherium/gobbonet-agentic) | GobboNet campaigns with a real agent brain — scoped memory, graph recall | [docs](https://aitherium.github.io/gobbonet-agentic/) |
| [aitherkvcache](https://github.com/Aitherium/aitherkvcache) | Near-optimal KV cache quantization for LLM inference — sub-byte compression | [docs](https://aitherium.github.io/aitherkvcache/) |
| **awrtifact** _(you are here)_ | Deliberately chunk artifacts into GitHub release assets — the productized aitherkvcache mirror lane | [docs](https://aitherium.github.io/awrtifact/) |
| [AitherZero](https://github.com/Aitherium/AitherZero) | PowerShell 7+ automation framework — numbered, self-describing scripts | [docs](https://aitherium.github.io/AitherZero/) |
| [AitherConnect](https://github.com/Aitherium/AitherConnect) | Browser extension — federated AI search, page context, and the Living OS overlay | [docs](https://aitherium.github.io/AitherConnect/) |
| [awreason](https://github.com/Aitherium/awreason) | A portable reasoning client — sessions, phases, thoughts, and the chain that produced the answer | [docs](https://aitherium.github.io/awreason/) |
| [awrecurse](https://github.com/Aitherium/awrecurse) | Answer a question over a context far larger than the window — recursively, with the trace kept | [docs](https://aitherium.github.io/awrecurse/) |
| [awprism](https://github.com/Aitherium/awprism) | Turn a failure into ranked hypotheses — and say what would confirm each one | [docs](https://aitherium.github.io/awprism/) |
| [awrepl](https://github.com/Aitherium/awrepl) | A REPL an agent can actually use — state that survives between turns | [docs](https://aitherium.github.io/awrepl/) |
| [awresearch](https://github.com/Aitherium/awresearch) | Ask a research question, get a cited report you can check | [docs](https://aitherium.github.io/awresearch/) |
| [awfocus](https://github.com/Aitherium/awfocus) | See, search and steer every Claude session from one command | [docs](https://aitherium.github.io/awfocus/) |
| [awgym](https://github.com/Aitherium/awgym) | An ARC training gym — a game a world model can watch, and six roles that play through it | [docs](https://aitherium.github.io/awgym/) |
| [awpredict](https://github.com/Aitherium/awpredict) | Predict what your environment does next, and how surprised you were | [docs](https://aitherium.github.io/awpredict/) |
| [awsh](https://github.com/Aitherium/awsh) | Your terminal answers you -- type a question where a command would go | [docs](https://aitherium.github.io/awsh/) |
| [awkno](https://github.com/Aitherium/awkno) | The man page for the Aither World — every brick, stack and law, offline | [docs](https://aitherium.github.io/awkno/) |

<div id="aither-constellation" data-self="awrtifact"></div>
<script src="aither-constellation.js"></script>

<!-- aither-ecosystem:end -->
