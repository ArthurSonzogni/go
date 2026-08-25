#!/usr/bin/env python3
"""Updates README.md with links to all hosted static projects in this repository."""

import argparse
import html
import os
from pathlib import Path
import re
import subprocess
import sys


def get_base_url() -> str:
    """Determine the GitHub Pages base URL (canonical repo: 'go')."""
    try:
        remotes_out = subprocess.check_output(
            ["git", "remote", "-v"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        for line in remotes_out.splitlines():
            match = re.search(r"github\.com[:/]([^/\s]+)/([^/\s.]+?)(?:\.git)?(?:\s|$)", line)
            if match:
                owner, repo = match.group(1), match.group(2)
                if repo == "go":
                    return f"https://{owner}.github.io/{repo}"
    except Exception:
        pass
    return "https://ArthurSonzogni.github.io/go"


def extract_title(html_path: Path, fallback_title: str) -> str:
    """Extract <title> or <h1> from an HTML file, falling back to directory name."""
    try:
        content = html_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return fallback_title

    # Try matching <title>...</title>
    title_match = re.search(r"<title[^>]*>(.*?)</title>", content, re.IGNORECASE | re.DOTALL)
    if title_match:
        extracted = html.unescape(title_match.group(1)).strip()
        extracted = re.sub(r"\s+", " ", extracted)
        if extracted:
            return extracted

    # Try matching <h1>...</h1>
    h1_match = re.search(r"<h1[^>]*>(.*?)</h1>", content, re.IGNORECASE | re.DOTALL)
    if h1_match:
        raw_h1 = re.sub(r"<[^>]+>", "", h1_match.group(1))
        extracted = html.unescape(raw_h1).strip()
        extracted = re.sub(r"\s+", " ", extracted)
        if extracted:
            return extracted

    return fallback_title


def find_projects(repo_root: Path, base_url: str):
    """Find all subdirectories containing an index.html and return sorted list of projects."""
    projects = []
    base_url = base_url.rstrip("/")

    for entry in sorted(repo_root.iterdir()):
        if not entry.is_dir():
            continue
        # Skip hidden directories (.git, .github, etc.)
        if entry.name.startswith("."):
            continue

        index_file = entry / "index.html"
        if index_file.is_file():
            title = extract_title(index_file, entry.name)
            url = f"{base_url}/{entry.name}/"
            projects.append({
                "name": entry.name,
                "title": title,
                "url": url,
            })

    # Sort projects case-insensitively by title
    projects.sort(key=lambda p: p["title"].lower())
    return projects


def generate_projects_markdown(projects) -> str:
    """Generate Markdown bullet points for the projects."""
    lines = [f"- [{p['title']}]({p['url']})" for p in projects]
    return "\n".join(lines)


def update_readme_content(content: str, projects_md: str) -> str:
    """Insert or replace the ## Projects section in README content."""
    projects_header = "## Projects"

    if projects_header in content:
        # Match '## Projects' and everything until next '##' header or end of string
        pattern = r"(## Projects\s*\n)(?:.*?)((?=\n## |\Z))"
        replacement = r"## Projects\n\n" + projects_md + r"\n"
        new_content = re.sub(pattern, replacement, content, flags=re.DOTALL)
    else:
        new_content = content.rstrip() + f"\n\n{projects_header}\n\n{projects_md}\n"

    return new_content


def main():
    parser = argparse.ArgumentParser(description="Update README.md with hosted project links.")
    parser.add_argument("--base-url", default=None, help="Base URL for hosted pages")
    parser.add_argument("--readme", default="README.md", help="Path to README.md")
    parser.add_argument("--dry-run", action="store_true", help="Print changes without writing to file")
    parser.add_argument("--check", action="store_true", help="Check if README.md is up to date (CI mode)")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent
    readme_path = repo_root / args.readme
    base_url = args.base_url or get_base_url()

    projects = find_projects(repo_root, base_url)
    projects_md = generate_projects_markdown(projects)

    if not readme_path.exists():
        new_content = f"# Static Pages\n\n## Projects\n\n{projects_md}\n"
        old_content = ""
    else:
        old_content = readme_path.read_text(encoding="utf-8")
        new_content = update_readme_content(old_content, projects_md)

    if args.check:
        if not readme_path.exists() or old_content != new_content:
            print("README.md is outdated. Please run update_readme.py.")
            sys.exit(1)
        print("README.md is up to date.")
        return

    if args.dry_run:
        print(new_content)
        return

    readme_path.write_text(new_content, encoding="utf-8")
    print(f"Successfully updated {readme_path.name} with {len(projects)} projects:")
    for p in projects:
        print(f"  - {p['title']} -> {p['url']}")


if __name__ == "__main__":
    main()
