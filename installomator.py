from __future__ import annotations

"""
Installomator label discovery and name resolution.

Supports three sources:
  - "github"  : official Installomator/Installomator repo (default)
  - "fork"    : custom GitHub fork  (user provides owner/repo + branch)
  - "local"   : local clone or script file  (user provides filesystem path)
"""

import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

log = logging.getLogger(__name__)

# Official defaults
_GITHUB_OWNER = "Installomator"
_GITHUB_REPO  = "Installomator"
_GITHUB_BRANCH = "main"

CACHE_PATH = "/tmp/installomator_labels.txt"
CACHE_MAX_AGE_SECONDS = 3600  # 1 hour


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def default_source() -> dict:
    return {"type": "github"}


def fetch_labels(source: dict | None = None, force_refresh: bool = False) -> list[str]:
    """Return a sorted list of all Installomator label names."""
    source = source or default_source()
    src_type = source.get("type", "github")

    if src_type == "local":
        return _local_fetch_labels(source["path"])

    # github or fork — both hit GitHub raw URLs
    owner, repo, branch = _github_coords(source)

    if not force_refresh and _cache_is_fresh():
        with open(CACHE_PATH) as f:
            return [line.strip() for line in f if line.strip()]

    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/Labels.txt"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()

    labels = [line.strip() for line in resp.text.splitlines()
              if line.strip() and not line.strip().startswith("#")]
    labels.sort()

    with open(CACHE_PATH, "w") as f:
        f.write("\n".join(labels) + "\n")

    return labels


def fetch_script_contents(source: dict | None = None) -> str:
    """Return the full Installomator.sh script body for upload to Jamf."""
    source = source or default_source()
    src_type = source.get("type", "github")

    if src_type == "local":
        script_path = _resolve_script_path(source["path"])
        with open(script_path) as f:
            return f.read()

    owner, repo, branch = _github_coords(source)
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/Installomator.sh"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.text


def resolve_label_info(label: str, source: dict | None = None) -> dict:
    """
    Extract name, appName, and confirmation flag for a single label.

    Returns {"name": str, "app_name": str, "app_name_confirmed": bool}
    """
    source = source or default_source()
    src_type = source.get("type", "github")
    fallback = label.replace("-", " ").replace("_", " ").title()

    try:
        if src_type == "local":
            text = _local_read_fragment(source["path"], label)
        else:
            owner, repo, branch = _github_coords(source)
            url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/fragments/labels/{label}.sh"
            resp = requests.get(url, timeout=10)
            text = resp.text if resp.status_code == 200 else None

        if text:
            return _parse_fragment(text, fallback)
    except Exception as exc:
        log.warning("resolve_label_info(%s) failed: %s", label, exc)

    return {"name": fallback, "app_name": fallback, "app_name_confirmed": False}


def resolve_display_names(labels: list[str], source: dict | None = None,
                          progress_callback=None) -> dict[str, dict]:
    """
    Resolve label info for a list of labels (concurrent for GitHub sources).
    Returns {label: {"name": str, "app_name": str, "app_name_confirmed": bool}}.
    """
    source = source or default_source()
    results = {}
    total = len(labels)

    if source.get("type") == "local" or len(labels) <= 3:
        for i, label in enumerate(labels):
            results[label] = resolve_label_info(label, source)
            if progress_callback:
                progress_callback(label, i + 1, total)
        return results

    # Concurrent resolution for GitHub/fork sources
    done_count = 0
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(resolve_label_info, label, source): label for label in labels}
        for future in as_completed(futures):
            label = futures[future]
            done_count += 1
            try:
                results[label] = future.result()
            except Exception:
                fallback = label.replace("-", " ").replace("_", " ").title()
                results[label] = {"name": fallback, "app_name": fallback, "app_name_confirmed": False}
            if progress_callback:
                progress_callback(label, done_count, total)
    return results


def describe_source(source: dict | None = None) -> str:
    """Human-readable description of the source for log messages."""
    source = source or default_source()
    src_type = source.get("type", "github")
    if src_type == "local":
        return f"local: {source.get('path', '?')}"
    owner, repo, branch = _github_coords(source)
    return f"{owner}/{repo} @ {branch}"


# ---------------------------------------------------------------------------
# GitHub coord helpers
# ---------------------------------------------------------------------------

def _github_coords(source: dict) -> tuple[str, str, str]:
    if source.get("type") == "fork":
        repo_str = source.get("repo", f"{_GITHUB_OWNER}/{_GITHUB_REPO}")
        parts = repo_str.strip("/").split("/")
        owner = parts[0] if len(parts) >= 2 else _GITHUB_OWNER
        repo  = parts[1] if len(parts) >= 2 else parts[0]
        branch = source.get("branch", _GITHUB_BRANCH) or _GITHUB_BRANCH
        return owner, repo, branch
    return _GITHUB_OWNER, _GITHUB_REPO, _GITHUB_BRANCH


# ---------------------------------------------------------------------------
# Local filesystem helpers
# ---------------------------------------------------------------------------

def _resolve_script_path(path: str) -> str:
    """Given a path to a file or directory, return the path to Installomator.sh."""
    path = os.path.expanduser(path)
    if os.path.isfile(path):
        return path
    candidate = os.path.join(path, "Installomator.sh")
    if os.path.isfile(candidate):
        return candidate
    raise FileNotFoundError(f"No Installomator.sh found at {path}")


def _local_repo_root(path: str) -> str:
    """Guess the repo root from a script path or directory."""
    path = os.path.expanduser(path)
    if os.path.isfile(path):
        return os.path.dirname(path)
    return path


def _local_fetch_labels(path: str) -> list[str]:
    """
    Discover labels from a local Installomator clone.
    Priority: Labels.txt > fragments/labels/ directory listing > parse case statements from script
    """
    root = _local_repo_root(path)

    # Try Labels.txt
    labels_file = os.path.join(root, "Labels.txt")
    if os.path.isfile(labels_file):
        with open(labels_file) as f:
            labels = [line.strip() for line in f
                      if line.strip() and not line.strip().startswith("#")]
        labels.sort()
        return labels

    # Try fragments/labels/ directory
    frags_dir = os.path.join(root, "fragments", "labels")
    if os.path.isdir(frags_dir):
        labels = [f.removesuffix(".sh") for f in os.listdir(frags_dir)
                  if f.endswith(".sh") and not f.startswith("_")]
        labels.sort()
        return labels

    # Fall back: parse case labels from the monolithic script
    script_path = _resolve_script_path(path)
    return _parse_labels_from_script(script_path)


def _parse_labels_from_script(script_path: str) -> list[str]:
    """Extract label names from case statements in a monolithic Installomator.sh."""
    with open(script_path) as f:
        text = f.read()

    # Labels appear as:  labelname)  at the start of a line (inside case $labelserial in ... esac)
    # Filter out common non-label case arms
    skip = {"*", "esac", "in", "then", "else", "fi", "do", "done", ";;"}
    labels = []
    for match in re.finditer(r'^(\w[\w-]*)\)\s*$', text, re.MULTILINE):
        candidate = match.group(1)
        if candidate.lower() not in skip and len(candidate) > 1:
            labels.append(candidate)

    labels = sorted(set(labels))
    return labels


def _local_read_fragment(path: str, label: str) -> str | None:
    """Try to read a label fragment from the local filesystem."""
    root = _local_repo_root(path)
    frag = os.path.join(root, "fragments", "labels", f"{label}.sh")
    if os.path.isfile(frag):
        with open(frag) as f:
            return f.read()

    # Fallback: extract the case block from the monolithic script
    try:
        script_path = _resolve_script_path(path)
        with open(script_path) as f:
            text = f.read()
        # Find the case block: "label)\n ... ;;"
        pattern = rf'^{re.escape(label)}\)\s*\n(.*?)\n\s*;;'
        match = re.search(pattern, text, re.MULTILINE | re.DOTALL)
        if match:
            return match.group(1)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Fragment parsing
# ---------------------------------------------------------------------------

def _parse_fragment(text: str, fallback: str) -> dict:
    """Extract name and appName from a fragment's shell variable assignments."""
    name_match = re.search(r'^name\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not name_match:
        name_match = re.search(r"^name\s*=\s*'([^']+)'", text, re.MULTILINE)
    if not name_match:
        name_match = re.search(r'^name\s*=\s*(\S+)', text, re.MULTILINE)
    display_name = name_match.group(1).strip() if name_match else fallback

    app_name_match = re.search(r'^appName\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not app_name_match:
        app_name_match = re.search(r"^appName\s*=\s*'([^']+)'", text, re.MULTILINE)
    if not app_name_match:
        app_name_match = re.search(r'^appName\s*=\s*(\S+)', text, re.MULTILINE)
    app_name_confirmed = app_name_match is not None
    app_name = app_name_match.group(1).strip() if app_name_match else display_name
    app_name = app_name.removesuffix(".app")

    return {
        "name": display_name,
        "app_name": app_name,
        "app_name_confirmed": app_name_confirmed,
    }


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _cache_is_fresh() -> bool:
    if not os.path.exists(CACHE_PATH):
        return False
    age = time.time() - os.path.getmtime(CACHE_PATH)
    return age < CACHE_MAX_AGE_SECONDS
