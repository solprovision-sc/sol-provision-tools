#!/usr/bin/env python3
"""
sp_contribute.py — Sol Provision Tools Contributor Workflow
------------------------------------------------------------
Guides contributors through the full feature branch cycle:
  1. Pull latest dev
  2. Create a feature branch
  3. Stage all changed files EXCEPT branch-specific files
  4. Commit with a prompted message
  5. Push to origin

Branch-specific files (never synced between dev and main):
  - app/templates/index.html  — prod has Coming Soon cards, dev has full landing
  - app/static/js/common.js   — prod nav shows only live pages, dev shows all

Run from the repo root:
  python tools/sp_contribute.py

Requirements: Git must be installed and on PATH.
"""

import subprocess
import sys
import os
import time

# ── Config ────────────────────────────────────────────────────────────────────

PROTECTED_FILES = [
    "app/templates/index.html",   # prod = Coming Soon cards, dev = full landing
    "app/static/js/common.js",    # prod nav = live pages only, dev nav = all pages
]

BRANCH_PREFIX = "feature/"
REMOTE        = "origin"
BASE_BRANCH   = "dev"

# Delay in seconds between narrated steps (set to 0 to disable)
STEP_DELAY    = 2
ACTION_DELAY  = 1

# ── Colours (ANSI — works in Git Bash and most terminals) ────────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
CYAN   = "\033[96m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
DIM    = "\033[2m"

def c(color, text):
    return f"{color}{text}{RESET}"

# ── Narration helpers ─────────────────────────────────────────────────────────

def narrate(msg, delay=ACTION_DELAY):
    """Print a narration line then pause briefly."""
    print(f"  {c(DIM, '▸')} {msg}")
    time.sleep(delay)

def success(msg):
    print(f"  {c(GREEN, '✓')} {msg}")

def warn(msg):
    print(f"  {c(YELLOW, '⚠')} {msg}")

def fail(msg):
    print(f"  {c(RED, '✗')} {msg}")

def divider():
    print(c(DIM, "  " + "─" * 58))

def step_header(num, title, description):
    """Print a step header with number, title, and what this step does."""
    print(f"\n{c(BOLD + CYAN, f'  ┌─[ Step {num} ]─ {title}')}")
    print(f"  {c(DIM, f'│  {description}')}")
    print(f"{c(CYAN, '  └' + '─' * 58)}")
    time.sleep(STEP_DELAY)

# ── Git runner ────────────────────────────────────────────────────────────────

def run_git(cmd, narration=None, capture=True, check=True):
    """
    Run a git command with optional narration.
    Returns (stdout, stderr, returncode).
    Prints success/failure feedback from actual git output.
    """
    if narration:
        narrate(narration)

    result = subprocess.run(
        cmd, shell=True, text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    code   = result.returncode

    if code == 0:
        # Print relevant git output lines (skip blank lines)
        output_lines = (stdout + "\n" + stderr).strip().splitlines()
        for line in output_lines:
            line = line.strip()
            if line:
                print(f"    {c(DIM, line)}")
    else:
        # Print git's error output so the user knows exactly what went wrong
        fail(f"Git command failed: {c(BOLD, cmd)}")
        error_lines = (stderr + "\n" + stdout).strip().splitlines()
        for line in error_lines:
            line = line.strip()
            if line:
                print(f"    {c(RED, line)}")
        if check:
            print()
            fail("Cannot continue. Please fix the error above and try again.")
            sys.exit(1)

    return stdout, stderr, code

# ── Input helpers ─────────────────────────────────────────────────────────────

def ask(prompt, allow_empty=False):
    """Prompt the user for input."""
    while True:
        value = input(f"\n  {c(CYAN, '▶')} {prompt} ").strip()
        if value or allow_empty:
            return value
        warn("Input cannot be empty. Please try again.")

def confirm(prompt):
    """Ask a yes/no question. Returns True for y/yes."""
    answer = input(f"\n  {c(CYAN, '▶')} {prompt} {c(DIM, '[y/n]')} ").strip().lower()
    return answer in ("y", "yes")

# ── Pre-flight checks ─────────────────────────────────────────────────────────

def check_git_installed():
    narrate("Checking that Git is installed and available on PATH...")
    result = subprocess.run("git --version", shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        fail("Git is not installed or not on PATH.")
        fail("Download from: https://git-scm.com/download/win")
        sys.exit(1)
    version = result.stdout.strip()
    success(f"Git found — {version}")
    time.sleep(ACTION_DELAY)

def check_repo_root():
    narrate("Verifying this is the repository root...")
    if not os.path.exists(".git"):
        fail("No .git folder found here. Are you in the right directory?")
        fail("Run this script from the root of sol-provision-tools/")
        sys.exit(1)
    success("Repository root confirmed.")
    time.sleep(ACTION_DELAY)

def check_clean_state():
    narrate("Checking for any uncommitted changes on the current branch...")
    result = subprocess.run(
        "git status --porcelain", shell=True, capture_output=True, text=True
    )
    # We don't exit here — changes are expected. Just report current branch.
    branch_result = subprocess.run(
        "git branch --show-current", shell=True, capture_output=True, text=True
    )
    current = branch_result.stdout.strip()
    success(f"Currently on branch: {c(CYAN, current)}")
    time.sleep(ACTION_DELAY)
    return current

# ── Steps ─────────────────────────────────────────────────────────────────────

def step_pull_dev(current_branch):
    step_header(
        1, "Sync with Remote",
        "Switch to 'dev' and pull the latest changes from GitHub."
        "\n  │  This ensures your feature branch starts from the most current code."
    )

    if current_branch != BASE_BRANCH:
        narrate(f"You're on '{current_branch}' — switching to '{BASE_BRANCH}' first...")
        stdout, stderr, code = run_git(
            f"git checkout {BASE_BRANCH}",
            narration=None
        )
        if code == 0:
            success(f"Switched to branch '{BASE_BRANCH}'.")
        time.sleep(ACTION_DELAY)
    else:
        narrate(f"Already on '{BASE_BRANCH}' — no branch switch needed.")

    narrate(f"Pulling latest changes from {REMOTE}/{BASE_BRANCH}...")
    narrate("This downloads any commits your teammates have pushed since you last worked.")
    stdout, stderr, code = run_git(f"git pull {REMOTE} {BASE_BRANCH}")

    if code == 0:
        # Detect whether anything was actually pulled
        combined = (stdout + stderr).lower()
        if "already up to date" in combined:
            success("Already up to date — no new changes from remote.")
        else:
            success(f"Pulled latest changes from {REMOTE}/{BASE_BRANCH} successfully.")
    time.sleep(STEP_DELAY)


def step_create_branch():
    step_header(
        2, "Create Feature Branch",
        "Create a new branch for your work off 'dev'."
        "\n  │  Feature branches keep your changes isolated until they're ready to review."
    )

    narrate("Feature branches are named with the 'feature/' prefix automatically.")
    narrate("Use a short descriptive name — e.g. ships-dynamic, weapons-detail, cargo-v2")

    while True:
        name = ask("Feature branch name (feature/...):")
        # Strip prefix if user typed it manually
        name = name.removeprefix(BRANCH_PREFIX).strip()
        # Sanitize — lowercase, spaces to hyphens
        name = name.lower().replace(" ", "-").replace("_", "-")
        branch = f"{BRANCH_PREFIX}{name}"

        print(f"\n  Branch will be created as: {c(CYAN, branch)}")
        if confirm("Looks good?"):
            break
        warn("Let's try again.")

    narrate(f"Creating branch '{branch}' from '{BASE_BRANCH}'...")
    narrate("This branch only exists on your machine until you push it to GitHub.")
    stdout, stderr, code = run_git(f"git checkout -b {branch}")

    if code == 0:
        success(f"Feature branch '{c(CYAN, branch)}' created and checked out.")

    time.sleep(STEP_DELAY)

    # ── Pause for user to make their changes ──────────────────────────────────
    print()
    divider()
    print(f"\n  {c(BOLD + YELLOW, '  ✎  Time to make your changes.')}\n")
    print(f"  {c(DIM, 'Go to your editor and make whatever changes you need.')}")
    print(f"  {c(DIM, 'This terminal will wait here until you are ready.')}")
    print(f"  {c(DIM, 'Remember: index.html and common.js are protected — changes')}")
    print(f"  {c(DIM, 'to those files will be excluded automatically.')}")
    print()
    input(f"  {c(CYAN, '▶')} {c(BOLD, 'Press Enter when you are done editing and ready to stage...')} ")
    print()

    return branch


def step_stage_files():
    step_header(
        3, "Stage Changed Files",
        "Review your changes and stage everything except protected files."
        "\n  │  Protected files (like index.html) are permanently different between"
        "\n  │  dev and main — they must never be synced between branches."
    )

    narrate("Scanning for changed, new, or deleted files in the repository...")
    result = subprocess.run(
        "git status --porcelain", shell=True, capture_output=True, text=True
    )
    status = result.stdout.strip()

    # Debug — show raw git output so path parsing issues are immediately visible
    if status:
        print(f"  {c(DIM, 'Raw git status:')}")
        for line in status.splitlines():
            print(f"    {c(DIM, repr(line))}")

    if not status:
        warn("No changed files detected. Nothing to stage or commit.")
        if confirm("Exit without committing?"):
            sys.exit(0)

    # Parse file list
    all_files = []
    for line in status.splitlines():
        # porcelain format: 'XY filepath' — status code is always first 2 chars
        # split on the space after the status code rather than hardcoding index
        if len(line) < 4:
            continue
        # status code = line[:2], space = line[2], path = line[3:]
        # but strip any leading/trailing whitespace from the path to be safe
        raw_path = line[2:].lstrip()
        if " -> " in raw_path:
            # renamed file: 'old -> new' — we want the new name
            raw_path = raw_path.split(" -> ")[1].strip()
        if raw_path:
            all_files.append(raw_path)

    # Separate protected from stageable
    protected_found = []
    to_stage = []

    for f in all_files:
        normalized = f.replace("\\", "/")
        if any(normalized == p or normalized.endswith("/" + p.split("/")[-1])
               for p in PROTECTED_FILES):
            protected_found.append(f)
        else:
            to_stage.append(f)

    time.sleep(ACTION_DELAY)

    # Report protected files
    if protected_found:
        print()
        warn("The following protected files have changes but will NOT be staged:")
        for f in protected_found:
            print(f"    {c(YELLOW, '⊘')} {f}  {c(DIM, '← branch-specific, skipped automatically')}")
        narrate("These files are intentionally different between dev and main.")
        narrate("They will never be included by this script — no action needed.")
        time.sleep(ACTION_DELAY)

    if not to_stage:
        warn("No stageable files remain after excluding protected files.")
        sys.exit(0)

    # Show stageable files
    print()
    narrate(f"Found {len(to_stage)} file(s) ready to stage:")
    for f in to_stage:
        # Show git status code for context
        for line in status.splitlines():
            if f in line:
                status_code = line[:2].strip()
                label = {
                    "M": "modified", "A": "new file", "D": "deleted",
                    "??": "untracked", "R": "renamed"
                }.get(status_code, status_code)
                print(f"    {c(GREEN, '+')} {f}  {c(DIM, f'({label})')}")
                break

    print()
    if not confirm("Stage all of the above files?"):
        warn("Aborted by user. Nothing was staged.")
        sys.exit(0)

    # Stage each file
    narrate("Staging files one by one...")
    for f in to_stage:
        stdout, stderr, code = run_git(f'git add "{f}"')
        if code == 0:
            print(f"    {c(GREEN, '✓')} Staged: {f}")
        time.sleep(0.3)

    print()
    success(f"Staged {len(to_stage)} file(s) successfully.")
    time.sleep(STEP_DELAY)
    return to_stage


def step_commit(branch):
    step_header(
        4, "Commit",
        "Package your staged changes into a commit with a descriptive message."
        "\n  │  Good commit messages help teammates understand what changed and why."
    )

    narrate("A commit is a saved snapshot of your staged changes.")
    narrate("Use the format:  Component: brief description of what changed")
    print()
    print(f"  {c(DIM, 'Examples:')}")
    print(f"  {c(DIM, '  Ships: add dynamic detail panel with cargo tabs')}")
    print(f"  {c(DIM, '  Server: add /api/weapons/ship endpoint')}")
    print(f"  {c(DIM, '  Patchnotes: fix auto-select ordering by version')}")
    print(f"  {c(DIM, '  CI: update deploy workflow')}")

    message = ask("Commit message:")

    narrate(f"Committing with message: \"{message}\"")
    stdout, stderr, code = run_git(f'git commit -m "{message}"')

    if code == 0:
        success(f"Commit created successfully.")
        # Extract and show the short hash if available
        for line in (stdout + stderr).splitlines():
            if line.strip():
                print(f"    {c(DIM, line.strip())}")
    time.sleep(STEP_DELAY)


def step_push(branch):
    step_header(
        5, "Push to GitHub",
        f"Upload your feature branch to GitHub so it's visible to your team."
        f"\n  │  This does NOT merge anything — it just makes '{branch}'"
        f"\n  │  available for a Pull Request review."
    )

    narrate(f"Pushing '{branch}' to {REMOTE} on GitHub...")
    narrate("This may take a moment depending on your connection.")
    stdout, stderr, code = run_git(f"git push {REMOTE} {branch}")

    if code == 0:
        success(f"Branch '{c(CYAN, branch)}' is now on GitHub.")
    time.sleep(STEP_DELAY)


def step_summary(branch):
    print()
    divider()
    print(f"\n  {c(BOLD + GREEN, '✓ All done! Your feature branch is live on GitHub.')}\n")
    divider()
    print(f"\n  {c(BOLD, 'What just happened:')}")
    print(f"  {c(DIM, '1.')} Pulled latest '{BASE_BRANCH}' from GitHub")
    print(f"  {c(DIM, '2.')} Created feature branch '{c(CYAN, branch)}'")
    print(f"  {c(DIM, '3.')} Staged your changes (excluding protected files)")
    print(f"  {c(DIM, '4.')} Committed with your message")
    print(f"  {c(DIM, '5.')} Pushed the branch to GitHub")
    print(f"\n  {c(BOLD, 'Next steps — open a Pull Request:')}")
    print(f"  {c(DIM, '1.')} Go to:")
    print(f"       {c(CYAN, 'https://github.com/solprovision-sc/sol-provision-tools')}")
    print(f"  {c(DIM, '2.')} Click {c(YELLOW, 'Compare & pull request')} for '{c(CYAN, branch)}'")
    print(f"  {c(DIM, '3.')} Set base branch to {c(CYAN, 'dev')} (not main)")
    print(f"  {c(DIM, '4.')} {c(YELLOW, '⚠ Check the diff — confirm these are NOT included:')}")
    print(f"       {c(YELLOW, '⊘')} app/templates/index.html")
    print(f"       {c(YELLOW, '⊘')} app/static/js/common.js")
    print(f"  {c(DIM, '5.')} Submit the PR — a teammate will review and merge")
    print(f"  {c(DIM, '6.')} Once merged → auto-deploys to {c(CYAN, 'tools-dev.solprovision.com')}")
    print()

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # Header
    print()
    print(c(BOLD + CYAN, "  ╔══════════════════════════════════════════════════╗"))
    print(c(BOLD + CYAN, "  ║      Sol Provision Tools                        ║"))
    print(c(BOLD + CYAN, "  ║      Contributor Feature Branch Workflow         ║"))
    print(c(BOLD + CYAN, "  ╚══════════════════════════════════════════════════╝"))
    print()
    print(f"  This script will guide you through creating a feature branch,")
    print(f"  staging your changes, committing, and pushing to GitHub.")
    print()
    print(f"  {c(YELLOW, 'Branch-specific files — automatically excluded from every commit:')}")
    for f in PROTECTED_FILES:
        label = {
            "app/templates/index.html": "prod = Coming Soon cards  |  dev = full landing",
            "app/static/js/common.js":  "prod nav = live pages only  |  dev nav = all pages",
        }.get(f, "branch-specific")
        print(f"  {c(YELLOW, '  ⊘')} {f}  {c(DIM, f'({label})')}")
    print()
    divider()

    # Pre-flight
    print(f"\n{c(BOLD, '  Pre-flight checks')}")
    check_git_installed()
    check_repo_root()
    current_branch = check_clean_state()
    print()

    if not confirm("Pre-flight passed. Ready to start the workflow?"):
        warn("Aborted. No changes were made.")
        sys.exit(0)

    # Steps
    step_pull_dev(current_branch)
    branch = step_create_branch()
    step_stage_files()
    step_commit(branch)
    step_push(branch)
    step_summary(branch)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(c(YELLOW, "\n\n  Interrupted by user. Any unpushed changes remain local only."))
        sys.exit(0)