"""CredDataAdapter: the economizer's domain surface for the CredData secret-finding eval.

CredData (Samsung, github.com/Samsung/CredData) is 333 repos of obfuscated source, each
shipped as `data/<repoid>/` with a per-repo label CSV `meta/<repoid>.csv`. The task: run
the coding agent as a security auditor over a disposable copy of one repo, have it WRITE a
findings JSON, and grade line-level detection F1 against the labels (that F1 is the
CredData analog of SWE-bench `resolved` -- the HARD quality guardrail).

The inner coding-agent run is the SAME host agent SWE-bench's runner drives, so the
`transcript.jsonl` is byte-compatible with the vendored `reflect` readers; EVERY
trajectory / tool-usage / catalog / waste / spiral-envelope method is INHERITED verbatim.
Only two domain-specific surfaces are overridden:

  problem_statements()  -> {repoid: fixed security-audit prompt}. The CredData task is the
                          SAME audit instruction for every repo, so every repoid maps to
                          the one prompt (what the untool per-task judge keys on).
  classify_domain()     -> the CredData-specific brief for the untool tool classifier: a
                          READ-ONLY scan that only reads/searches files and WRITES one
                          findings file (never edits/tests the audited source).

The CredData checkout is located by TOKENECONOMIZER_CREDDATA_ROOT (a placeholder you set)
or an explicit `creddata_root=`. The task EXECUTOR + GRADER stay ABOVE the economizer at
the runner layer, exactly as the SWE-bench Docker grade does.
"""
import glob
import os

from .swebench import SwebenchAdapter

CREDDATA_ROOT_ENV = "TOKENECONOMIZER_CREDDATA_ROOT"

CREDDATA_TASK_PROMPT = (
    "You are a security auditor scanning a source tree for HARDCODED CREDENTIALS "
    "(secrets) that have been leaked into the code. Scan EVERY file in the current "
    "directory tree (the working directory is the root of the code to audit). Look for "
    "hardcoded credentials of any kind: passwords, API keys, secret keys, tokens, private "
    "keys, nonces, salts, seeds, AWS keys, credentials embedded in URLs, and similar "
    "secret material. For EACH secret, record the file (path RELATIVE to this directory) "
    "and the 1-based line number where the secret value appears, and write the findings as "
    "a JSON array to a findings file. Do NOT modify any source file you are auditing; only "
    "create the findings file. Missing a real secret and flagging a non-secret both hurt "
    "the score, which is the F1 of the findings against a ground-truth label set."
)

CREDDATA_CLASSIFY_DOMAIN = (
    "You configure the toolset for an autonomous SECURITY-AUDIT agent that will SCAN a "
    "source tree to find HARDCODED CREDENTIALS (secrets) and WRITE its findings to a JSON "
    "file. Execution constraints: the agent runs HEADLESS and SINGLE-SHOT (no human is "
    "available to answer questions), in one sealed working directory per task (no "
    "sub-agents, no persistent cross-session state, no scheduling, no external memory). "
    "The agent must READ and SEARCH files (navigate the tree, view file contents, grep "
    "for patterns, run shell commands) and CREATE the findings file; it does NOT need to "
    "modify the audited source. Each loaded tool's full JSON schema is re-sent to the "
    "model on EVERY turn, costing tokens, so tools that cannot possibly help "
    "scan-and-report in this setting should be removed.\n\nFor EACH tool below, classify "
    "it:\n"
    "  KEEP    = could plausibly help read/navigate/search files, run shell commands, or "
    "write the findings file\n"
    "  EXCLUDE = structurally cannot help in this headless single-shot secret-scanning "
    "setting\n"
    "  UNSURE  = ambiguous\n"
    "Be conservative: only EXCLUDE when you are confident the tool is inapplicable BY "
    "CONSTRUCTION. Return ONLY a JSON object {tool_name: {\"class\": "
    "\"KEEP|EXCLUDE|UNSURE\", \"reason\": \"<short>\"}}.\n\nTOOLS:\n"
)


class CredDataAdapter(SwebenchAdapter):
    name = "creddata"

    def __init__(self, creddata_root=None, repo_ids=None):
        """`creddata_root` locates the CredData checkout (defaults to the
        TOKENECONOMIZER_CREDDATA_ROOT env-var placeholder). `repo_ids` optionally restricts
        problem_statements() to a subset; when None, every repo under meta/ is enumerated."""
        self.creddata_root = creddata_root or os.environ.get(CREDDATA_ROOT_ENV)
        self._repo_ids = list(repo_ids) if repo_ids is not None else None

    def meta_dir(self):
        if not self.creddata_root:
            raise RuntimeError(
                f"no CredData root: set ${CREDDATA_ROOT_ENV} or pass creddata_root=.")
        return os.path.join(self.creddata_root, "meta")

    def repo_ids(self):
        if self._repo_ids is not None:
            return list(self._repo_ids)
        return sorted(os.path.basename(p)[:-4]
                      for p in glob.glob(os.path.join(self.meta_dir(), "*.csv")))

    def problem_statements(self):
        """{repoid: audit_prompt} -- every CredData repo gets the SAME audit instruction."""
        return {rid: CREDDATA_TASK_PROMPT for rid in self.repo_ids()}

    def classify_domain(self):
        return CREDDATA_CLASSIFY_DOMAIN
