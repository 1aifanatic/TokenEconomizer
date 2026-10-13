# Environment & requirements

Everything you need on a **fresh clone**, split into two independent layers. The layers do
**not** overlap: the repo layer is tiny and pure-Python; the harness layer (Docker, agent
CLIs, datasets) is external infrastructure you provision only when you want to reproduce the
real cost numbers.

```
┌─ Layer 1: THIS REPO ────────────────────────────────────────────────┐
│  Python >= 3.10.  Core = stdlib-only (zero deps).                    │
│  + pytest (tests).   requirements.txt.                               │
│  Lets you: import the package, run the test suite, run the offline   │
│  smoke test.  NO Docker, NO datasets.                                │
└─────────────────────────────────────────────────────────────────────┘
┌─ Layer 2: THE HARNESS (te_harness/, + what it drives) ──────────────┐
│  Docker + the sealed agent CLIs + dataset checkouts.  The runner    │
│  itself is in the repo; none of what it drives can be.  Lets you:   │
│  run each benchmark for real and measure cost and resolve.          │
│  See docs/HARNESS.md and docs/BENCHMARKS.md.                        │
└─────────────────────────────────────────────────────────────────────┘
```

## Layer 1 — the repo (always required)

| Requirement | Version | Why |
|---|---|---|
| Python | ≥ 3.10 | the package + scripts |
| pip / venv | any recent | install the package |
| pytest | ≥ 7.0 | test suite — optional |

Core `import tokeneconomizer` needs **nothing beyond the standard library**. `pytest` is
only for the tests.

**Install (recommended — editable, with the test extra):**
```bash
python3 -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e '..[test]'                               # the repository root's pyproject.toml
```

**Or with the convenience requirements file** (same pins, no build step):
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -e ..           # still install the package itself (editable)
```

**Verify Layer 1 with zero external infra:**
```bash
python -c "import tokeneconomizer as te; print(te.Economizer)"   # self-contained import
pytest                                                            # offline suite, no Docker/datasets
bash scripts/launch/smoke_test.sh                                # every arm wired end-to-end, auto-cleans
```
If those three pass, the repo layer is healthy. **A fresh user can do all of this with no
Docker, no datasets, and no API keys.**

## Layer 2 — the harness (only for real runs)

> This section is the **what**. For the **how**, in order, with a gate after every step, see
> [`SETUP.md`](SETUP.md) — venv, the data directory, credentials, the runner, then
> per-benchmark data.

A real run launches a **sealed agent inside a benchmark Docker image** and grades it against
a gold patch. The code that does this is in the repo
([`HARNESS.md`](HARNESS.md)); everything it *drives* is not, and cannot be. To reproduce for
real you supply:

1. **Docker** — to run the per-benchmark execution images (see install guide below).
2. **A sealed agent CLI** — Claude Code, GitHub Copilot CLI, or Codex, on its own native
   authentication: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or a Copilot subscription or
   Copilot BYOK. Keys go in `.env` (see [`../.env.example`](../.env.example)); the per-agent
   table is in [`RUNNING.md`](RUNNING.md).
3. **Dataset checkouts** — per [`docs/BENCHMARKS.md`](BENCHMARKS.md) (SWE-bench, Web-Bench,
   CredData, ReposVul, CyberGym).
4. **Nothing else.** The `AgentRunner` implementation — the glue that launches the agent in
   the image, applies the economizer's per-task decisions, grades, and writes `row.json` +
   `transcript.jsonl` — ships in the repo as `te_harness`, so this is a flag rather than a
   deliverable: `RUNNER=te_harness.swebench:make_runner`. Pass
   `RUNNER=your_pkg.harness:make_runner` to substitute your own.

Some dataset **provisioning** steps need extra tools that are **not** repo dependencies
(don't put them in `requirements.txt`):

| Tool | Needed for | Install |
|---|---|---|
| `git` | cloning Web-Bench / CredData / ReposVul | your OS package manager |
| `datasets` (HF) | fetching SWE-bench Verified instances | `pip install datasets` (provisioning only) |
| Node.js + npm | Web-Bench evaluator + Playwright | nodejs.org / `nvm` |

---

## Installing & updating Docker (for beginners)

You only need this for **Layer 2**. Skip it entirely if you're just running the repo layer
(import / tests / smoke test).

### Check what you already have
```bash
docker --version            # e.g. "Docker version 27.x"
docker run --rm hello-world # prints a success message if the daemon is working
```
If `docker` is missing or `hello-world` fails, install/fix it below.

### Linux (Ubuntu/Debian) — Docker Engine
```bash
# 1. Remove any old distro packages
sudo apt-get remove -y docker docker-engine docker.io containerd runc 2>/dev/null || true

# 2. Add Docker's official apt repository
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
  sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# 3. Install
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin

# 4. Run docker without sudo (log out/in afterward)
sudo usermod -aG docker "$USER"
```

### macOS / Windows — Docker Desktop
- **macOS:** install [Docker Desktop](https://docs.docker.com/desktop/install/mac-install/)
  (Apple-Silicon or Intel build), or `brew install --cask docker`, then launch Docker.app.
- **Windows:** install [Docker Desktop](https://docs.docker.com/desktop/install/windows-install/)
  with the **WSL 2** backend, and run the launchers from inside a WSL 2 Ubuntu shell.

### Updating Docker
```bash
# Linux (Docker Engine): just re-pull the packages
sudo apt-get update && sudo apt-get install -y \
  docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# macOS / Windows (Docker Desktop): Docker menu → "Check for Updates…", or reinstall the cask
brew upgrade --cask docker
```

### Common first-run fixes
- **`permission denied … /var/run/docker.sock`** — you're not in the `docker` group yet: run
  the `usermod -aG docker` line above, then log out and back in (or `newgrp docker`).
- **`Cannot connect to the Docker daemon`** — the daemon isn't running:
  `sudo systemctl start docker` (Linux) or launch Docker Desktop (macOS/Windows).
- **Benchmark images are large** — pre-pull the instances you'll run and give Docker enough
  disk (SWE-bench per-instance images are multi-GB each).

---

## What a fresh user can and cannot reproduce

| Goal | Needs | Doable on a bare clone? |
|---|---|---|
| Import the package | Layer 1 | ✅ yes |
| Run the test suite | Layer 1 | ✅ yes |
| Run the offline smoke test (all arms wired) | Layer 1 | ✅ yes — synthetic numbers |
| Measure cost and resolve per arm | Layer 2 (Docker + agent CLI + credentials + datasets) | ⚠️ yes, once Layer 2 is provisioned |

The boundary: **the repo exercises the *method and wiring* out of the box; real numbers need
Docker, an agent CLI with its credentials, and the datasets**, which are machine- and
account-specific and therefore not shipped here.
