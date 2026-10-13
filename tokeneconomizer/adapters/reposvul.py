"""ReposVulAdapter: the economizer's domain surface for the ReposVul vulnerability eval.

ReposVul (ICSE'24, github.com/Eshe0922/ReposVul) is a repository-level vulnerability
dataset: each instance is a CVE with a fixing commit, from which the PRE-FIX (vulnerable)
files are reconstructed at their real repo paths. The task is the direct analog of
CredData's: run the coding agent as a security auditor over one instance's files, have it
WRITE a findings JSON of (file, line) locations, and grade line-level F1 against the
fix-removed (vulnerable) lines (the ReposVul analog of SWE-bench `resolved`).

Thin subclass of SwebenchAdapter for the same transfer reason as CredData/WebBench: the
inner coding-agent run is the SAME host agent, so its transcript is byte-compatible with
the vendored `reflect` readers and EVERY reading method is INHERITED verbatim. Only
`problem_statements()` (one fixed vulnerability-audit prompt per instance) and
`classify_domain()` (a read-only-scan classifier brief) are overridden.

The prepared ReposVul data is located by TOKENECONOMIZER_REPOSVUL_ROOT (a placeholder you
set) or an explicit `reposvul_root=`.
"""
import glob
import os

from .swebench import SwebenchAdapter

REPOSVUL_ROOT_ENV = "TOKENECONOMIZER_REPOSVUL_ROOT"

REPOSVUL_TASK_PROMPT = (
    "You are a security auditor scanning a source tree for SOFTWARE VULNERABILITIES "
    "(exploitable security flaws) present in the code. Scan EVERY file in the current "
    "directory tree (the working directory is the root of the code to audit). Look for "
    "vulnerable code of any kind: injection (SQL/command/path), improper authentication or "
    "authorization, missing input validation, unsafe deserialization, buffer/bounds "
    "errors, use of broken/weak cryptography, hardcoded credentials, race conditions, "
    "resource leaks, and similar security defects. For EACH vulnerability, record the file "
    "(path RELATIVE to this directory) and the 1-based line number of the vulnerable line, "
    "and write the findings as a JSON array to a findings file. Do NOT modify any source "
    "file you are auditing; only create the findings file. Missing a real vulnerability and "
    "flagging safe code both hurt the score, which is the F1 of the findings against a "
    "ground-truth label set."
)

REPOSVUL_CLASSIFY_DOMAIN = (
    "You configure the toolset for an autonomous SECURITY-AUDIT agent that will SCAN a "
    "source tree to find SOFTWARE VULNERABILITIES (exploitable security flaws) and WRITE "
    "its findings to a JSON file. Execution constraints: the agent runs HEADLESS and "
    "SINGLE-SHOT (no human is available to answer questions), in one sealed working "
    "directory per task (no sub-agents, no persistent cross-session state, no scheduling, "
    "no external memory). The agent must READ and SEARCH files (navigate the tree, view "
    "file contents, grep for patterns, run shell commands) and CREATE the findings file; it "
    "does NOT need to modify, build, or test the audited source. Each loaded tool's full "
    "JSON schema is re-sent to the model on EVERY turn, costing tokens, so tools that "
    "cannot possibly help scan-and-report in this setting should be removed.\n\nFor EACH "
    "tool below, classify it:\n"
    "  KEEP    = could plausibly help read/navigate/search files, run shell commands, or "
    "write the findings file\n"
    "  EXCLUDE = structurally cannot help in this headless single-shot vulnerability-"
    "scanning setting\n"
    "  UNSURE  = ambiguous\n"
    "Be conservative: only EXCLUDE when you are confident the tool is inapplicable BY "
    "CONSTRUCTION. Return ONLY a JSON object {tool_name: {\"class\": "
    "\"KEEP|EXCLUDE|UNSURE\", \"reason\": \"<short>\"}}.\n\nTOOLS:\n"
)


class ReposVulAdapter(SwebenchAdapter):
    name = "reposvul"

    def __init__(self, reposvul_root=None, instance_ids=None):
        """`reposvul_root` locates the prepared ReposVul data (defaults to the
        TOKENECONOMIZER_REPOSVUL_ROOT env-var placeholder). `instance_ids` optionally
        restricts problem_statements() to a subset; when None, every instance is used."""
        self.reposvul_root = reposvul_root or os.environ.get(REPOSVUL_ROOT_ENV)
        self._instance_ids = list(instance_ids) if instance_ids is not None else None

    def instances_dir(self):
        if not self.reposvul_root:
            raise RuntimeError(
                f"no ReposVul root: set ${REPOSVUL_ROOT_ENV} or pass reposvul_root=.")
        return os.path.join(self.reposvul_root, "instances")

    def instance_ids(self):
        if self._instance_ids is not None:
            return list(self._instance_ids)
        return sorted(os.path.basename(p)[:-5]
                      for p in glob.glob(os.path.join(self.instances_dir(), "*.json")))

    def problem_statements(self):
        """{iid: audit_prompt} -- every instance gets the SAME vulnerability-audit prompt."""
        return {iid: REPOSVUL_TASK_PROMPT for iid in self.instance_ids()}

    def classify_domain(self):
        return REPOSVUL_CLASSIFY_DOMAIN
